"""The reader names every unread behaviour and runnable item.

The reader holds one **closed** table per parent (``SWC-INTERNAL-BEHAVIOR``, ``RUNNABLE-ENTITY``): a child is
*read*, *compared against what the writer regenerates* or *owned elsewhere*, and every other non-empty child is one
skip entry per item. This module holds:

* **one entry per table row**, per item, tagged with the item's tag, at the item's path when it is named and the
  parent's when it is a reference, with a reason naming the container;
* **the closed-table property**: an unknown container is reported by construction, an empty one not;
* **the compared group**: ``SUPPORTS-MULTIPLE-INSTANTIATION`` is reported where the source differs from the writer's
  shape, absence included; ``SYMBOL`` and ``PORT-API-OPTION`` are the mapper's, and only an absent ``SYMBOL`` is a
  reader line;
* **what is read**: ``HANDLE-TERMINATION-AND-RESTART`` is never reported by the table, nor its unknown literal
  twice;
* **the documentation group**: a ``LONG-NAME``, ``CATEGORY``, ``INTRODUCTION`` or ``ANNOTATIONS`` of either parent,
  and a behaviour's own ``DESC``, is one entry per child with text; a runnable's ``DESC`` is read;
* **one cause, one line**: a foreign ``ADMIN-DATA`` and a duplicate AUTOSAR path keep the index's single entry;
* **the live round trip**: a component SWCraft exports re-imports byte for byte with no skip entry.

What this module does **not** reach: what the reader does not select below a read item (inside an event, an access
point or a mapping reference); the report and ``import component``'s abort (``test_behaviour_losses_import.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from behaviour_losses_support import (
    BEHAVIOUR,
    CONTAINER_ROWS,
    MINIMAL_SWC,
    NS,
    RUNNABLE,
    SWC_PATH,
    add_xml,
    items_xml,
    option_xml,
    snippet,
    unread_reason,
)
from conftest import ACCESS_POINTS_MODEL_ROOT
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_reader import SkipCategory, SkipEntry, read_arxml
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.reader import read_swc_model
from swcraft_tools.writer.yaml_documents import swc_to_dict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(tmp_path: Path, mutate) -> list[SkipEntry]:
    """Read ``minimal_swc.arxml`` after *mutate(behaviour, runnable)*; its skip entries."""
    tree = etree.parse(str(MINIMAL_SWC))
    behaviour = next(tree.getroot().iter(f"{NS}SWC-INTERNAL-BEHAVIOR"))
    runnable = next(behaviour.iter(f"{NS}RUNNABLE-ENTITY"))
    mutate(behaviour, runnable)
    path = tmp_path / "source.arxml"
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return list(read_arxml(str(path)).skip_log)


def _remove(parent: etree._Element, tag: str) -> None:
    for element in parent.findall(f"{NS}{tag}"):
        parent.remove(element)


def _set_text(parent: etree._Element, tag: str, text: str) -> None:
    node = parent.find(f"{NS}{tag}")
    assert node is not None, tag
    node.text = text


def _replace_options(behaviour: etree._Element, *options: str) -> None:
    container = behaviour.find(f"{NS}PORT-API-OPTIONS")
    assert container is not None
    for child in list(container):
        container.remove(child)
    add_xml(container, "".join(options))


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_the_writer_shaped_fixture_reads_with_no_entry(tmp_path: Path) -> None:
    """Every case below starts from a behaviour the table is silent on."""
    assert _read(tmp_path, lambda behaviour, runnable: None) == []


# ---------------------------------------------------------------------------
# One entry per item, per row of the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("owner", "container", "item", "named"), CONTAINER_ROWS, ids=lambda value: str(value))
def test_each_item_of_an_unread_container_is_one_entry(
    owner: str, container: str, item: str, named: bool, tmp_path: Path
) -> None:
    parent_path = BEHAVIOUR if owner == "behaviour" else RUNNABLE
    xml = f"<{container}>{items_xml(item, named, 'item')}</{container}>"

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour if owner == "behaviour" else runnable, xml))

    assert len(entries) == 2, entries
    assert {entry.tag for entry in entries} == {item}
    expected_paths = [f"{parent_path}/item1", f"{parent_path}/item2"] if named else [parent_path, parent_path]
    assert [entry.path for entry in entries] == expected_paths
    parent_tag = "SWC-INTERNAL-BEHAVIOR" if owner == "behaviour" else "RUNNABLE-ENTITY"
    assert {entry.reason for entry in entries} == {unread_reason(container, parent_tag)}


def test_two_containers_holding_one_item_tag_keep_apart_in_the_reason(tmp_path: Path) -> None:
    """Two unread containers both holding ``PARAMETER-ACCESS``-like items keep apart in the reason.

    ``PARAMETER-ACCESSS`` and ``MODE-SWITCH-POINTS`` are two named rows the table does not read.
    """
    parameter = "<PARAMETER-ACCESSS><PARAMETER-ACCESS><SHORT-NAME>a</SHORT-NAME></PARAMETER-ACCESS></PARAMETER-ACCESSS>"
    switch = (
        "<MODE-SWITCH-POINTS><MODE-SWITCH-POINT><SHORT-NAME>b</SHORT-NAME></MODE-SWITCH-POINT></MODE-SWITCH-POINTS>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(runnable, parameter + switch))

    assert [(entry.tag, entry.reason.split(" ")[0]) for entry in entries] == [
        ("PARAMETER-ACCESS", "PARAMETER-ACCESSS"),
        ("MODE-SWITCH-POINT", "MODE-SWITCH-POINTS"),
    ]


def test_an_unrecognized_call_point_kind_keeps_its_one_entry(tmp_path: Path) -> None:
    """The one precedent row: the access containers keep their own path, and it is not doubled."""
    xml = (
        "<SERVER-CALL-POINTS><FUTURE-CALL-POINT><SHORT-NAME>call</SHORT-NAME></FUTURE-CALL-POINT></SERVER-CALL-POINTS>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(runnable, xml))

    assert [(entry.path, entry.tag) for entry in entries] == [(f"{RUNNABLE}/call", "FUTURE-CALL-POINT")]
    assert entries[0].reason == (
        "unrecognized access kind inside SERVER-CALL-POINTS (only SYNCHRONOUS-SERVER-CALL-POINT or "
        "ASYNCHRONOUS-SERVER-CALL-POINT is read)"
    )


# ---------------------------------------------------------------------------
# The closed table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("owner", ["behaviour", "runnable"])
def test_an_empty_container_reports_nothing(owner: str, tmp_path: Path) -> None:
    xml = "<EXCLUSIVE-AREAS/><WAIT-POINTS></WAIT-POINTS><DATA-READ-ACCESSS>\n</DATA-READ-ACCESSS>"

    assert (
        _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour if owner == "behaviour" else runnable, xml)) == []
    )


@pytest.mark.parametrize("owner", ["behaviour", "runnable"])
def test_a_container_no_table_names_is_reported(owner: str, tmp_path: Path) -> None:
    """The closed-table property: the next tag an export brings is reported by construction."""
    xml = "<FUTURE-CONTAINERS><FUTURE-ITEM><SHORT-NAME>next</SHORT-NAME></FUTURE-ITEM></FUTURE-CONTAINERS>"
    parent_path = BEHAVIOUR if owner == "behaviour" else RUNNABLE

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour if owner == "behaviour" else runnable, xml))

    assert [(entry.path, entry.tag) for entry in entries] == [(f"{parent_path}/next", "FUTURE-ITEM")]
    assert entries[0].reason.startswith("FUTURE-CONTAINERS of a ")


@pytest.mark.parametrize("owner", ["behaviour", "runnable"])
def test_an_unrecognized_scalar_child_is_reported(owner: str, tmp_path: Path) -> None:
    parent_path = BEHAVIOUR if owner == "behaviour" else RUNNABLE
    parent_tag = "SWC-INTERNAL-BEHAVIOR" if owner == "behaviour" else "RUNNABLE-ENTITY"

    entries = _read(
        tmp_path,
        lambda behaviour, runnable: add_xml(
            behaviour if owner == "behaviour" else runnable, '<FUTURE-FLAG>true</FUTURE-FLAG><FUTURE-MARK KIND="x"/>'
        ),
    )

    assert [(entry.path, entry.tag) for entry in entries] == [
        (parent_path, "FUTURE-FLAG"),
        (parent_path, "FUTURE-MARK"),
    ]
    assert entries[0].reason == unread_reason("FUTURE-FLAG", parent_tag)


def test_a_foreign_child_inside_runnables_is_reported(tmp_path: Path) -> None:
    entries = _read(
        tmp_path,
        lambda behaviour, runnable: add_xml(
            behaviour.find(f"{NS}RUNNABLES"), "<BSW-MODULE-ENTITY><SHORT-NAME>odd</SHORT-NAME></BSW-MODULE-ENTITY>"
        ),
    )

    assert [(entry.path, entry.tag, entry.reason) for entry in entries] == [
        (f"{BEHAVIOUR}/odd", "BSW-MODULE-ENTITY", "unrecognized child inside RUNNABLES (only RUNNABLE-ENTITY is read)")
    ]


def test_a_foreign_child_inside_data_type_mapping_refs_is_reported(tmp_path: Path) -> None:
    xml = (
        "<DATA-TYPE-MAPPING-REFS>"
        '<DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Acme/Maps</DATA-TYPE-MAPPING-REF>'
        '<MODE-REQUEST-TYPE-MAP-REF DEST="X">/Acme/Other</MODE-REQUEST-TYPE-MAP-REF>'
        "</DATA-TYPE-MAPPING-REFS>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour, xml))

    assert [(entry.path, entry.tag, entry.reason) for entry in entries] == [
        (
            BEHAVIOUR,
            "MODE-REQUEST-TYPE-MAP-REF",
            "unrecognized child inside DATA-TYPE-MAPPING-REFS (only DATA-TYPE-MAPPING-REF is read)",
        )
    ]


# --- The documentation group is reported ---
# Each child below holds TWO items, so a per-item report would give two where one is expected.
_DOCUMENTATION_WITH_TEXT = {
    "LONG-NAME": '<LONG-NAME><L-4 L="EN">Wheel speed</L-4><L-4 L="DE">Raddrehzahl</L-4></LONG-NAME>',
    "CATEGORY": "<CATEGORY>VENDOR_KIND</CATEGORY>",
    "INTRODUCTION": '<INTRODUCTION><P><L-1 L="EN">First.</L-1></P><P><L-1 L="EN">Second.</L-1></P></INTRODUCTION>',
    "ANNOTATIONS": (
        '<ANNOTATIONS><ANNOTATION><LABEL><L-4 L="EN">one</L-4></LABEL></ANNOTATION>'
        '<ANNOTATION><LABEL><L-4 L="EN">two</L-4></LABEL></ANNOTATION></ANNOTATIONS>'
    ),
    "DESC": '<DESC><L-2 L="EN">First.</L-2><L-2 L="DE">Erste.</L-2></DESC>',
}
_DOCUMENTATION_EMPTY = {
    "LONG-NAME": '<LONG-NAME><L-4 L="FOR-ALL"/></LONG-NAME>',
    "CATEGORY": "<CATEGORY>  </CATEGORY>",
    "INTRODUCTION": '<INTRODUCTION><P><L-1 L="EN"/></P></INTRODUCTION>',
    "ANNOTATIONS": "<ANNOTATIONS><ANNOTATION/></ANNOTATIONS>",
    "DESC": '<DESC>\n  <L-2 L="FOR-ALL"/>\n  <L-2 L="EN"></L-2>\n</DESC>',
}
_REPORTED_DOCUMENTATION = [
    *(("behaviour", tag) for tag in _DOCUMENTATION_WITH_TEXT),
    *(("runnable", tag) for tag in _DOCUMENTATION_WITH_TEXT if tag != "DESC"),
]


def _add_to(owner: str, xml: str):
    return lambda behaviour, runnable: add_xml(behaviour if owner == "behaviour" else runnable, xml)


@pytest.mark.parametrize(("owner", "tag"), _REPORTED_DOCUMENTATION)
def test_a_documentation_child_with_text_is_one_entry(owner: str, tag: str, tmp_path: Path) -> None:
    """One ``NOT_SUPPORTED`` entry per child, whatever its item count."""
    parent_tag = "SWC-INTERNAL-BEHAVIOR" if owner == "behaviour" else "RUNNABLE-ENTITY"

    entries = _read(tmp_path, _add_to(owner, _DOCUMENTATION_WITH_TEXT[tag]))

    assert [(entry.path, entry.tag, entry.reason, entry.category) for entry in entries] == [
        (
            BEHAVIOUR if owner == "behaviour" else RUNNABLE,
            tag,
            f"{tag} of a {parent_tag} is documentation SWCraft does not carry -- not imported or preserved",
            SkipCategory.NOT_SUPPORTED,
        )
    ]


@pytest.mark.parametrize(("owner", "tag"), _REPORTED_DOCUMENTATION)
def test_an_empty_documentation_child_is_no_entry(owner: str, tag: str, tmp_path: Path) -> None:
    """An element with no text -- an empty ``<L-2 L="FOR-ALL"/>`` included -- loses nothing.

    Empty is a paragraph with no text node, whatever layout surrounds it; a paragraph of white space
    is text (``test_uncarried_losses.py``'s ``TestAWhitespaceOnlyParagraphIsText``).
    """
    assert _read(tmp_path, _add_to(owner, _DOCUMENTATION_EMPTY[tag])) == []


def test_a_runnable_desc_is_read_not_reported(tmp_path: Path) -> None:
    """The runnable's ``DESC`` is in the table's *read* group: it is carried.

    What the first ``L-2``'s text does not carry -- here a second ``L-2`` -- is one descriptive
    line of its own, never the table's.
    """
    entries = _read(tmp_path, _add_to("runnable", _DOCUMENTATION_WITH_TEXT["DESC"]))
    assert [(entry.path, entry.tag, entry.effect) for entry in entries] == [(RUNNABLE, "DESC", "report")]
    assert "a second L-2" in entries[0].reason


def test_every_documentation_child_together_is_one_entry_each(tmp_path: Path) -> None:
    both = "".join(_DOCUMENTATION_WITH_TEXT.values())
    runnable_only = "".join(xml for tag, xml in _DOCUMENTATION_WITH_TEXT.items() if tag != "DESC")

    def mutate(behaviour: etree._Element, runnable: etree._Element) -> None:
        add_xml(behaviour, both)
        add_xml(runnable, runnable_only)

    assert sorted((entry.path, entry.tag) for entry in _read(tmp_path, mutate)) == sorted(
        [(BEHAVIOUR, tag) for tag in _DOCUMENTATION_WITH_TEXT]
        + [(RUNNABLE, tag) for tag in _DOCUMENTATION_WITH_TEXT if tag != "DESC"]
    )


@pytest.mark.parametrize("owner", ["behaviour", "runnable"])
def test_a_foreign_admin_data_is_reported_once(owner: str, tmp_path: Path) -> None:
    """``ADMIN-DATA`` stays *owned elsewhere*: the index's one entry, no documentation line beside it."""
    xml = '<ADMIN-DATA><SDGS><SDG GID="VENDOR"><SD GID="k">v</SD></SDG></SDGS></ADMIN-DATA>'

    entries = _read(tmp_path, _add_to(owner, xml))

    parent = BEHAVIOUR if owner == "behaviour" else RUNNABLE
    assert [(entry.path, entry.tag) for entry in entries] == [(parent, "ADMIN-DATA")]
    assert "documentation" not in entries[0].reason


def test_an_item_with_a_duplicate_path_is_reported_once(tmp_path: Path) -> None:
    """The index already reported the second declaration; the table does not report it again."""
    xml = (
        "<AR-TYPED-PER-INSTANCE-MEMORYS><VARIABLE-DATA-PROTOTYPE><SHORT-NAME>Area</SHORT-NAME>"
        "</VARIABLE-DATA-PROTOTYPE></AR-TYPED-PER-INSTANCE-MEMORYS>"
        "<PER-INSTANCE-PARAMETERS><PARAMETER-DATA-PROTOTYPE><SHORT-NAME>Area</SHORT-NAME>"
        "</PARAMETER-DATA-PROTOTYPE></PER-INSTANCE-PARAMETERS>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour, xml))

    by_tag = {entry.tag: entry for entry in entries}
    assert len(entries) == 2 and set(by_tag) == {"VARIABLE-DATA-PROTOTYPE", "PARAMETER-DATA-PROTOTYPE"}, entries
    assert {entry.path for entry in entries} == {f"{BEHAVIOUR}/Area"}
    assert by_tag["VARIABLE-DATA-PROTOTYPE"].reason.startswith(
        "AR-TYPED-PER-INSTANCE-MEMORYS of a SWC-INTERNAL-BEHAVIOR"
    )
    assert by_tag["PARAMETER-DATA-PROTOTYPE"].reason.startswith("duplicate full AUTOSAR path")


def test_a_runnable_the_index_dropped_is_not_scanned(tmp_path: Path) -> None:
    """A second runnable of one name is the index's one line; what it holds is not reported beside it."""
    xml = (
        "<RUNNABLE-ENTITY><SHORT-NAME>WheelSpeedSensorRun</SHORT-NAME>"
        "<EXCLUSIVE-AREA-FLAGS>true</EXCLUSIVE-AREA-FLAGS></RUNNABLE-ENTITY>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour.find(f"{NS}RUNNABLES"), xml))

    assert [(entry.path, entry.tag) for entry in entries] == [(RUNNABLE, "RUNNABLE-ENTITY")]
    assert entries[0].reason.startswith("duplicate full AUTOSAR path")


def test_a_runnable_named_like_an_earlier_event_is_not_read(tmp_path: Path) -> None:
    """EVENTS precedes RUNNABLES: the index keeps the event and drops the runnable, and so does the reader."""
    taken = "TMT_WheelSpeedSensorRun"
    xml = f"<RUNNABLE-ENTITY><SHORT-NAME>{taken}</SHORT-NAME><SYMBOL>Other</SYMBOL></RUNNABLE-ENTITY>"

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour.find(f"{NS}RUNNABLES"), xml))

    assert [(entry.path, entry.tag) for entry in entries] == [(f"{BEHAVIOUR}/{taken}", "RUNNABLE-ENTITY")]
    assert entries[0].reason.startswith("duplicate full AUTOSAR path")
    (swc,) = read_arxml(str(tmp_path / "source.arxml")).swcs
    assert [runnable.name for runnable in swc.runnables] == ["WheelSpeedSensorRun"]


def test_a_further_behaviour_is_one_entry(tmp_path: Path) -> None:
    """The container is unbounded in both schemas; SWCraft reads the first behaviour and reports each other."""
    xml = (
        "<SWC-INTERNAL-BEHAVIOR><SHORT-NAME>Variant</SHORT-NAME>"
        "<EXCLUSIVE-AREAS><EXCLUSIVE-AREA><SHORT-NAME>Area</SHORT-NAME></EXCLUSIVE-AREA></EXCLUSIVE-AREAS>"
        "</SWC-INTERNAL-BEHAVIOR>"
    )

    entries = _read(tmp_path, lambda behaviour, runnable: add_xml(behaviour.getparent(), xml))

    assert [(entry.path, entry.tag) for entry in entries] == [(f"{SWC_PATH}/Variant", "SWC-INTERNAL-BEHAVIOR")]
    assert "only the first is read" in entries[0].reason


def test_a_behaviour_child_outside_the_autosar_namespace_is_the_indexs_one_entry(tmp_path: Path) -> None:
    def mutate(behaviour: etree._Element, runnable: etree._Element) -> None:
        behaviour.append(etree.fromstring(b'<V:EXTRA xmlns:V="urn:vendor"><V:ITEM>x</V:ITEM></V:EXTRA>'))

    entries = _read(tmp_path, mutate)

    assert len(entries) == 1, entries


def test_a_runnable_child_outside_the_autosar_namespace_is_the_indexs_one_entry(tmp_path: Path) -> None:
    """The runnable's table skips a foreign child, so the index's own entry is the only one."""

    def mutate(behaviour: etree._Element, runnable: etree._Element) -> None:
        runnable.append(etree.fromstring(b'<V:EXTRA xmlns:V="urn:vendor"><V:ITEM>x</V:ITEM></V:EXTRA>'))

    entries = _read(tmp_path, mutate)

    assert len(entries) == 1, entries
    assert not any(entry.tag == "EXTRA" and "RUNNABLE-ENTITY" in entry.reason for entry in entries), entries


# ---------------------------------------------------------------------------
# The compared group
# ---------------------------------------------------------------------------


class TestSupportsMultipleInstantiation:
    def test_false_is_not_reported(self, tmp_path: Path) -> None:
        assert (
            _read(tmp_path, lambda behaviour, runnable: _set_text(behaviour, "SUPPORTS-MULTIPLE-INSTANTIATION", "0"))
            == []
        )

    def test_true_is_reported(self, tmp_path: Path) -> None:
        entries = _read(
            tmp_path, lambda behaviour, runnable: _set_text(behaviour, "SUPPORTS-MULTIPLE-INSTANTIATION", "true")
        )

        assert [(entry.path, entry.tag) for entry in entries] == [(BEHAVIOUR, "SUPPORTS-MULTIPLE-INSTANTIATION")]
        assert "is not false" in entries[0].reason and "the writer emits false" in entries[0].reason

    def test_absent_is_reported(self, tmp_path: Path) -> None:
        entries = _read(tmp_path, lambda behaviour, runnable: _remove(behaviour, "SUPPORTS-MULTIPLE-INSTANTIATION"))

        assert [(entry.path, entry.tag) for entry in entries] == [(BEHAVIOUR, "SUPPORTS-MULTIPLE-INSTANTIATION")]
        assert "is absent" in entries[0].reason and "the writer emits false" in entries[0].reason


class TestSymbol:
    """``SYMBOL`` is read as written; the reader reports only its absence."""

    def test_equal_to_the_name_is_not_reported(self, tmp_path: Path) -> None:
        assert _read(tmp_path, lambda behaviour, runnable: None) == []

    def test_a_different_symbol_is_read_and_not_reported(self, tmp_path: Path) -> None:
        assert _read(tmp_path, lambda behaviour, runnable: _set_text(runnable, "SYMBOL", "Rte_Other")) == []

    def test_a_padded_symbol_is_no_reader_line(self, tmp_path: Path) -> None:
        """The reader judges nothing about the text: the mapper drops and names it (``test_runnable_symbol_import``)."""
        assert (
            _read(tmp_path, lambda behaviour, runnable: _set_text(runnable, "SYMBOL", " WheelSpeedSensorRun\n")) == []
        )

    def test_an_absent_symbol_is_reported(self, tmp_path: Path) -> None:
        entries = _read(tmp_path, lambda behaviour, runnable: _remove(runnable, "SYMBOL"))

        assert [(entry.path, entry.tag) for entry in entries] == [(RUNNABLE, "SYMBOL")]
        assert "is absent" in entries[0].reason


class TestPortApiOptionsAreReadNotJudged:
    """The reader reads each option's raw facts and records no line for one.

    Which line an option gets is the mapper's decision, once it has decided the component's
    ports (``test_port_api_option_import.py``). A foreign child of the container stays the
    closed table's line.
    """

    @pytest.mark.parametrize(
        "options",
        [
            (option_xml(), option_xml("rWheelSpeedEcho", "R-PORT-PROTOTYPE")),
            (),
            (option_xml(extra="<PORT-ARG-VALUES><PORT-DEFINED-ARGUMENT-VALUE/></PORT-ARG-VALUES>"),),
            (option_xml(flags=False),),
            (option_xml().replace("<INDIRECT-API>false", "<INDIRECT-API>true"),),
            (option_xml().replace(f"{SWC_PATH}/pWheelSpeed", "/Acme/Chassis/Components/OtherSwc/pWheelSpeed"),),
            (option_xml(dest="R-PORT-PROTOTYPE"),),
            (option_xml(), option_xml()),
            (option_xml().replace(f">{SWC_PATH}/pWheelSpeed<", ">  <"),),
            (option_xml().replace(f">{SWC_PATH}/pWheelSpeed<", f">\n  {SWC_PATH}/pWheelSpeed\n<"),),
        ],
        ids=[
            "the-old-writers-shape",
            "none",
            "port-arg-values",
            "no-flag",
            "a-flag-true",
            "another-components-port",
            "wrong-dest",
            "a-second-option",
            "blank-port-ref",
            "padded-port-ref",
        ],
    )
    def test_no_option_is_a_reader_line(self, options: tuple[str, ...], tmp_path: Path) -> None:
        assert _read(tmp_path, lambda behaviour, runnable: _replace_options(behaviour, *options)) == []

    def test_an_option_naming_the_components_own_pr_port_is_chained_to_the_port(self, tmp_path: Path) -> None:
        """The PR port is ``map_swcs``' skip, and its option's line is chained to it, keyed at the option's port."""
        pr_port = (
            "<PR-PORT-PROTOTYPE><SHORT-NAME>prBoth</SHORT-NAME>"
            '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Chassis/Interfaces/IWheelSpeed'
            "</PROVIDED-INTERFACE-TREF></PR-PORT-PROTOTYPE>"
        )

        def mutate(behaviour: etree._Element, runnable: etree._Element) -> None:
            swc = behaviour.getparent().getparent()
            add_xml(swc.find(f"{NS}PORTS"), pr_port)
            _replace_options(behaviour, option_xml(), option_xml("prBoth", "PR-PORT-PROTOTYPE"))

        assert _read(tmp_path, mutate) == []
        source = tmp_path / "source.arxml"
        project = init_project(tmp_path / "proj")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(source), str(project / "model"))
        port_line = f"PR-PORT-PROTOTYPE {SWC_PATH}/prBoth: "
        option_line = (
            f"PORT-API-OPTION {SWC_PATH}/prBoth: PORT-API-OPTION of port {SWC_PATH}/prBoth, which is not imported ("
        )
        lines = [line for line in caught.value.skip_lines if "prBoth" in line]
        assert [line.split(": ")[0] + ": " for line in lines] == [port_line, f"PORT-API-OPTION {SWC_PATH}/prBoth: "]
        assert lines[1].startswith(option_line)

    def test_a_foreign_child_of_the_container_is_reported(self, tmp_path: Path) -> None:
        entries = _read(
            tmp_path,
            lambda behaviour, runnable: add_xml(behaviour.find(f"{NS}PORT-API-OPTIONS"), "<ODD-OPTION>x</ODD-OPTION>"),
        )

        assert [(entry.path, entry.tag, entry.reason) for entry in entries] == [
            (BEHAVIOUR, "ODD-OPTION", "unrecognized child inside PORT-API-OPTIONS (only PORT-API-OPTION is read)")
        ]


# ---------------------------------------------------------------------------
# HANDLE-TERMINATION-AND-RESTART is read
# ---------------------------------------------------------------------------


def _insert_termination(behaviour: etree._Element, literal: str) -> None:
    (element,) = snippet(f"<HANDLE-TERMINATION-AND-RESTART>{literal}</HANDLE-TERMINATION-AND-RESTART>")
    behaviour.find(f"{NS}PORT-API-OPTIONS").addprevious(element)


@pytest.mark.parametrize("literal", ["NO-SUPPORT", "CAN-BE-TERMINATED", "CAN-BE-TERMINATED-AND-RESTARTED"])
def test_a_termination_literal_is_not_reported_by_the_table(literal: str, tmp_path: Path) -> None:
    assert _read(tmp_path, lambda behaviour, runnable: _insert_termination(behaviour, literal)) == []


def test_an_unknown_termination_literal_is_task_ones_one_entry(tmp_path: Path) -> None:
    entries = _read(tmp_path, lambda behaviour, runnable: _insert_termination(behaviour, "SOMETIMES"))

    assert [(entry.path, entry.tag) for entry in entries] == [(BEHAVIOUR, "HANDLE-TERMINATION-AND-RESTART")]
    assert entries[0].reason.startswith("unrecognized HANDLE-TERMINATION-AND-RESTART value 'SOMETIMES'")


# ---------------------------------------------------------------------------
# The live round trip
# ---------------------------------------------------------------------------


def _rich_model(tmp_path: Path) -> Path:
    """``access_points`` with one component carrying ports, every access list, every event kind and the field."""
    root = tmp_path / "source_model"
    shutil.copytree(ACCESS_POINTS_MODEL_ROOT, root)
    path = root / "swc" / "access" / "AccessSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["ports"].append({"name": "pControl", "direction": "provide", "interface": "IControl"})
    document["runnables"].append({"name": "StartRun", "events": [{"type": "init"}]})
    document["runnables"].append(
        {"name": "ServeRun", "events": [{"type": "operation", "port": "pControl", "operation": "Apply"}]}
    )
    document["handle_termination_and_restart"] = "can_be_terminated_and_restarted"
    # What the exchange does not carry is left out, so the comparison below is about
    # what it does: a component's DESC is not exported, and the writer emits the
    # provided ports first.
    document.pop("description", None)
    document["ports"].sort(key=lambda port: port["direction"] != "provide")
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def test_a_component_swcraft_exports_reimports_byte_identically_with_no_entry(tmp_path: Path) -> None:
    source_root = _rich_model(tmp_path)
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(source_root), "AccessSwc", str(arxml))

    tags = {etree.QName(node).localname for node in etree.parse(str(arxml)).iter()}
    assert {"TIMING-EVENT", "INIT-EVENT", "OPERATION-INVOKED-EVENT"} <= tags
    assert "PORT-API-OPTIONS" not in tags  # the writer writes no port API option
    assert {"DATA-RECEIVE-POINT-BY-ARGUMENTS", "DATA-SEND-POINTS", "SERVER-CALL-POINTS", "ADMIN-DATA"} <= tags
    assert {"SYMBOL", "SUPPORTS-MULTIPLE-INSTANTIATION", "HANDLE-TERMINATION-AND-RESTART"} <= tags
    assert list(read_arxml(str(arxml)).skip_log) == []

    project = init_project(tmp_path / "proj")
    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    # The one report-only line every component export carries: its implementation
    # element, a deferred tag the report adds itself and no reader entry.
    assert report.skip_lines == [
        "SWC-IMPLEMENTATION /ComponentTypes/AccessSwc_Implementation: SWC-IMPLEMENTATION has no SWCraft representation"
    ]
    (source,) = [swc for swc in read_swc_model(str(source_root)) if swc.name == "AccessSwc"]
    # The export carries no trace link, so a fresh import writes the component
    # without the source's `satisfies` and nothing else differs.
    source = source.model_copy(update={"satisfies": []})
    expected = yaml.safe_dump(swc_to_dict(source), default_flow_style=False, sort_keys=False)
    (written,) = list((project / "model").glob("swc/**/AccessSwc.yaml"))
    assert written.read_text(encoding="utf-8") == expected
