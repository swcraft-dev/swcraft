"""Tests for arxml_reader.py's SWCraft markers: the ADMIN-DATA groups read as its own, and what is reported."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import ACCESS_POINTS_MODEL_ROOT
from swcraft_tools.arxml_reader import ImportModel, SwcInfo, read_arxml
from swcraft_tools.writer import generate_arxml

# The SWCraft capability marker


def _admin_data_document(tmp_path: Path, name: str, admin_data: str, *, on_behavior: bool = False) -> ImportModel:
    """One APPLICATION-SW-COMPONENT-TYPE carrying *admin_data*, read back.

    Hand-built so that no writer output can be mistaken for a control. *on_behavior* puts the block
    on the component's ``SWC-INTERNAL-BEHAVIOR``, where the writer puts the marker and the only
    element ``_read_swc`` reads markers off; the default puts it on the component, where a vendor
    block may stand.
    """
    body = (
        "          <INTERNAL-BEHAVIORS>\n"
        "            <SWC-INTERNAL-BEHAVIOR>\n"
        f"              <SHORT-NAME>{name}_InternalBehavior</SHORT-NAME>\n"
        f"{admin_data}"
        # Writer-shaped: an absent SUPPORTS-MULTIPLE-INSTANTIATION is itself a
        # reported difference, and not these tests' subject.
        "              <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>\n"
        "            </SWC-INTERNAL-BEHAVIOR>\n"
        "          </INTERNAL-BEHAVIORS>\n"
        if on_behavior
        else admin_data
    )
    document = tmp_path / f"{name}.arxml"
    document.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        f"          <SHORT-NAME>{name}</SHORT-NAME>\n"
        f"{body}"
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
        newline="\n",
    )
    return read_arxml(str(document))


#: A vendor block on an ``APPLICATION-SW-COMPONENT-TYPE``, an ``SDG GID="ResourceBudget"``.
_RESOURCE_BUDGET_SDG = (
    "          <ADMIN-DATA>\n"
    "            <SDGS>\n"
    '              <SDG GID="ResourceBudget">\n'
    '                <SD GID="RAMBudget">1024</SD>\n'
    "              </SDG>\n"
    "            </SDGS>\n"
    "          </ADMIN-DATA>\n"
)

_SWCRAFT_SDG = (
    "          <ADMIN-DATA>\n"
    "            <SDGS>\n"
    '              <SDG GID="SWCRAFT">\n'
    '                <SD GID="ACCESS-POINTS">explicit</SD>\n'
    "              </SDG>\n"
    "            </SDGS>\n"
    "          </ADMIN-DATA>\n"
)

_MIXED_SDGS = (
    "          <ADMIN-DATA>\n"
    "            <SDGS>\n"
    '              <SDG GID="SWCRAFT">\n'
    '                <SD GID="ACCESS-POINTS">explicit</SD>\n'
    "              </SDG>\n"
    '              <SDG GID="ResourceBudget">\n'
    '                <SD GID="RAMBudget">1024</SD>\n'
    "              </SDG>\n"
    "            </SDGS>\n"
    "          </ADMIN-DATA>\n"
)


class TestTheSwcraftSdgIsReadAndNotSkipped:
    """The reader reads SWCraft's own ``SDG`` and does not report it; a foreign group is still reported.

    ``ADMIN-DATA`` is not in ``DEFERRED_ELEMENT_TAGS``, so a skip line for it aborts
    ``import component`` as lossy: a marked SWCraft export would abort its own import.
    """

    def test_a_swcraft_only_admin_data_is_read_and_not_skip_logged(self, tmp_path: Path) -> None:
        model = _admin_data_document(tmp_path, "MarkedSwc", _SWCRAFT_SDG, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == {"ACCESS-POINTS": "explicit"}

    def test_a_foreign_admin_data_is_still_skip_logged_with_todays_line(self, tmp_path: Path) -> None:
        """A foreign block's lines keep their text, word for word: the skip was narrowed, not removed."""
        model = _admin_data_document(tmp_path, "BudgetSwc", _RESOURCE_BUDGET_SDG)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert entries[0].reason == (
            "ADMIN-DATA/SDG vendor metadata on APPLICATION-SW-COMPONENT-TYPE "
            "'BudgetSwc' has no SWCraft representation -- not imported or preserved"
        )
        assert model.swcs[0].swcraft_markers == {}

    def test_a_mixed_admin_data_is_reported_for_its_foreign_group_only(self, tmp_path: Path) -> None:
        """One entry, naming what was **not** read.

        The SWCraft group beside it still lands in ``swcraft_markers``."""
        model = _admin_data_document(tmp_path, "MixedSwc", _MIXED_SDGS, on_behavior=True)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert "ResourceBudget" in entries[0].reason
        assert "the SWCRAFT group beside it is read" in entries[0].reason
        assert model.swcs[0].swcraft_markers == {"ACCESS-POINTS": "explicit"}

    def test_an_admin_data_with_no_sdg_at_all_is_still_skip_logged(self, tmp_path: Path) -> None:
        """An admin data block with no SDG at all is still skip-logged.

        The narrowing is "every group is SWCraft's", not "no group is
        foreign": a block carrying other admin content (a ``DOC-REVISIONS``,
        say) has nothing SWCraft read and is reported as before."""
        other = "          <ADMIN-DATA>\n            <LANGUAGE>EN</LANGUAGE>\n          </ADMIN-DATA>\n"
        model = _admin_data_document(tmp_path, "OtherSwc", other)

        assert [e.tag for e in model.skip_log] == ["ADMIN-DATA"]
        assert model.swcs[0].swcraft_markers == {}

    def test_a_hand_built_swc_info_has_no_markers(self) -> None:
        """The field has a default, so every ``SwcInfo(...)`` built without it still constructs."""
        info = SwcInfo(path="/P/S", name="S", description="", ports=[], runnables=[], events=[])

        assert info.swcraft_markers == {}


#: A ``SWCRAFT`` group that is **not** the writer's marker: the right ``SDG``
#: ``GID`` and an ``SD`` the writer never mints. Nothing SWCraft reads today.
_SWCRAFT_SDG_OTHER_SHAPE = (
    "          <ADMIN-DATA>\n"
    "            <SDGS>\n"
    '              <SDG GID="SWCRAFT">\n'
    '                <SD GID="RAM-BUDGET">1024</SD>\n'
    "              </SDG>\n"
    "            </SDGS>\n"
    "          </ADMIN-DATA>\n"
)


def _interface_admin_data_document(tmp_path: Path, name: str, admin_data: str) -> ImportModel:
    """One SENDER-RECEIVER-INTERFACE carrying *admin_data*, read back.

    The third placement, beside the component and the behaviour: an element the
    writer never puts a marker on at all.
    """
    document = tmp_path / f"{name}.arxml"
    document.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>PortInterfaces</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <SENDER-RECEIVER-INTERFACE>\n"
        f"          <SHORT-NAME>{name}</SHORT-NAME>\n"
        f"{admin_data}"
        "        </SENDER-RECEIVER-INTERFACE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
        newline="\n",
    )
    return read_arxml(str(document))


class TestTheExemptionIsScopedToTheBehavioursOwnMarker:
    """What the narrowing to SWCraft's own marker may **not** swallow.

    It is scoped to the element (``SWC-INTERNAL-BEHAVIOR``, the only place ``_read_swc`` reads a
    marker off) and the shape (``SDG GID="SWCRAFT"`` holding ``SD GID="ACCESS-POINTS"``). A
    ``SWCRAFT`` group anywhere else, or of another shape, keeps its skip line, which aborts a
    component import instead of losing the payload in silence.
    """

    _TODAYS_LINE = (
        "ADMIN-DATA/SDG vendor metadata on {tag} '{name}' has no SWCraft representation -- not imported or preserved"
    )

    def test_the_marker_shape_on_the_component_is_still_vendor_metadata(self, tmp_path: Path) -> None:
        """The marker shape on the component is reported as vendor metadata.

        ``plan_component_import`` never succeeds with the payload gone unread and unreported."""
        model = _admin_data_document(tmp_path, "ComponentMarkedSwc", _SWCRAFT_SDG)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert entries[0].reason == self._TODAYS_LINE.format(
            tag="APPLICATION-SW-COMPONENT-TYPE", name="ComponentMarkedSwc"
        )
        assert model.swcs[0].swcraft_markers == {}

    def test_the_marker_shape_on_an_interface_is_still_vendor_metadata(self, tmp_path: Path) -> None:
        model = _interface_admin_data_document(tmp_path, "IMarked", _SWCRAFT_SDG)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert entries[0].reason == self._TODAYS_LINE.format(tag="SENDER-RECEIVER-INTERFACE", name="IMarked")

    def test_a_swcraft_group_of_another_shape_on_the_behaviour_is_still_vendor_metadata(self, tmp_path: Path) -> None:
        """The second axis: a SWCraft group of another shape is still vendor metadata.

        The right element, the right ``SDG GID``, an ``SD``
        the writer never mints. The group is foreign today, so it is reported and
        its data reaches ``swcraft_markers`` not at all."""
        model = _admin_data_document(tmp_path, "OtherShapeSwc", _SWCRAFT_SDG_OTHER_SHAPE, on_behavior=True)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert entries[0].reason == self._TODAYS_LINE.format(
            tag="SWC-INTERNAL-BEHAVIOR", name="OtherShapeSwc_InternalBehavior"
        )
        assert model.swcs[0].swcraft_markers == {}

    def test_the_is_read_clause_is_gated_on_the_same_condition(self, tmp_path: Path) -> None:
        """The mixed block on the COMPONENT: both groups are foreign there.

        The line names both and never claims a SWCraft group was read."""
        model = _admin_data_document(tmp_path, "MixedComponentSwc", _MIXED_SDGS)

        entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
        assert len(entries) == 1
        assert "the SWCRAFT group beside it is read" not in entries[0].reason
        assert model.swcs[0].swcraft_markers == {}

    def test_a_marker_carrying_an_unexpected_value_is_still_the_writers_shape(self, tmp_path: Path) -> None:
        """The shape is the two ``GID``s and never the text.

        A marker whose value is not ``explicit`` is read here and distrusted one layer up, which keeps a typo out of the
        vendor-metadata abort."""
        wrong = _SWCRAFT_SDG.replace(">explicit<", ">wrong<")
        model = _admin_data_document(tmp_path, "WrongValueSwc", wrong, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == {"ACCESS-POINTS": "wrong"}

    def test_the_reader_and_the_writer_read_one_copy_of_the_gids(self) -> None:
        """The reader and the writer both take the ``GID``s from the leaf ``capability_markers``.

        ``arxml_reader`` may not import the writer, and neither side holds a copy of its own."""
        from swcraft_tools import arxml_reader, capability_markers
        from swcraft_tools.writer import arxml_generation

        assert arxml_reader.SDG_GID is capability_markers.SDG_GID
        assert arxml_generation.SDG_GID is capability_markers.SDG_GID
        assert arxml_reader._OWN_MARKER_GIDS == {marker.sd_gid for marker in capability_markers.OWN_MARKERS}
        assert arxml_generation.COMPONENT_EXPORT_MARKERS is capability_markers.COMPONENT_EXPORT_MARKERS


# The reader over the WRITER's own access points


@pytest.fixture(scope="module")
def swcraft_written_access_points(tmp_path_factory) -> ImportModel:
    """``fixtures/access_points/model`` generated once, and read back in the exporter's own spelling."""
    output = tmp_path_factory.mktemp("access-points") / "model.arxml"
    generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output))
    return read_arxml(str(output))


class TestReadsTheWritersOwnAccessPoints:
    def test_the_reader_skip_logs_nothing_for_a_document_it_wrote_itself(self, swcraft_written_access_points) -> None:
        """The marker is read, so a marked SWCraft export does not abort its own re-import."""
        assert [f"{e.tag} at {e.path}: {e.reason}" for e in swcraft_written_access_points.skip_log] == []

    def test_every_container_is_read_with_its_kind_and_the_writers_own_refs(
        self, swcraft_written_access_points
    ) -> None:
        swc = next(s for s in swcraft_written_access_points.swcs if s.name == "AccessSwc")
        runnable = next(r for r in swc.runnables if r.name == "AccessMain")

        assert [(a.name, a.kind, a.tag) for a in runnable.accesses] == [
            ("R_rSignals_speed", "DATA-RECEIVE-POINT-BY-ARGUMENTS", "VARIABLE-ACCESS"),
            ("R_rSignals_level", "DATA-RECEIVE-POINT-BY-VALUES", "VARIABLE-ACCESS"),
            ("W_pSignals_speed", "DATA-SEND-POINTS", "VARIABLE-ACCESS"),
            ("Call_rControl_Apply", "SERVER-CALL-POINTS", "SYNCHRONOUS-SERVER-CALL-POINT"),
        ]
        assert runnable.accesses[0].port_ref == "/ComponentTypes/AccessSwc/rSignals"
        assert runnable.accesses[0].target_ref == "/PortInterfaces/ISignals/speed"
        assert runnable.accesses[3].port_ref == "/ComponentTypes/AccessSwc/rControl"
        assert runnable.accesses[3].target_ref == "/PortInterfaces/IControl/Apply"

    def test_the_runnable_that_declares_nothing_reads_back_with_no_access(self, swcraft_written_access_points) -> None:
        """The non-vacuity control on the emitter.

        A writer emitting a container per runnable would satisfy the assertion above."""
        swc = next(s for s in swcraft_written_access_points.swcs if s.name == "AccessSwc")

        assert next(r for r in swc.runnables if r.name == "QuietRun").accesses == []

    def test_the_marker_is_read_off_the_component_that_carries_a_point_and_no_other(
        self, swcraft_written_access_points
    ) -> None:
        """The per-component rule, read back: a component that declares no access reads back unmarked.

        ``ServerSwc`` declares no access and carries no ``ADMIN-DATA``."""
        assert {s.name: s.swcraft_markers for s in swcraft_written_access_points.swcs} == {
            "AccessSwc": {"ACCESS-POINTS": "explicit"},
            "ServerSwc": {},
        }


# The component export's data-types marker

_DATA_TYPES_SDG = (
    "          <ADMIN-DATA>\n"
    "            <SDGS>\n"
    '              <SDG GID="SWCRAFT">\n'
    '                <SD GID="DATA-TYPES">base and implementation types declared, no invented twin</SD>\n'
    "              </SDG>\n"
    "            </SDGS>\n"
    "          </ADMIN-DATA>\n"
)

_BOTH_SWCRAFT_SDS = _SWCRAFT_SDG.replace(
    '                <SD GID="ACCESS-POINTS">explicit</SD>\n',
    '                <SD GID="ACCESS-POINTS">explicit</SD>\n'
    '                <SD GID="DATA-TYPES">base and implementation types declared, no invented twin</SD>\n',
)


class TestTheDataTypesMarkerIsReadAndNotSkipped:
    """A ``SWCRAFT`` group holding the data-types ``SD`` is the writer's own, alone or beside ``ACCESS-POINTS``.

    Every component export writes it, and a component with no access point carries it alone;
    reported as vendor metadata, which is not deferred, it would abort the export's own import.
    """

    def test_a_data_types_only_group_on_the_behaviour_is_read_and_not_skip_logged(self, tmp_path: Path) -> None:
        model = _admin_data_document(tmp_path, "DataTypesSwc", _DATA_TYPES_SDG, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == {
            "DATA-TYPES": "base and implementation types declared, no invented twin"
        }

    def test_both_sds_in_one_group_are_read(self, tmp_path: Path) -> None:
        model = _admin_data_document(tmp_path, "BothSwc", _BOTH_SWCRAFT_SDS, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == {
            "ACCESS-POINTS": "explicit",
            "DATA-TYPES": "base and implementation types declared, no invented twin",
        }

    def test_the_group_on_the_component_is_still_foreign(self, tmp_path: Path) -> None:
        model = _admin_data_document(tmp_path, "DataTypesOnComponentSwc", _DATA_TYPES_SDG)

        assert [e.tag for e in model.skip_log] == ["ADMIN-DATA"]
        assert model.swcs[0].swcraft_markers == {}


# The marker group is the writer's own only as a whole

_ACCESS_SD = '<SD GID="ACCESS-POINTS">explicit</SD>'
_DATA_TYPES_SD = '<SD GID="DATA-TYPES">base and implementation types declared, no invented twin</SD>'


def _swcraft_groups(*groups: str, group_attributes: str = "", sdgs_extra: str = "") -> str:
    """An ``ADMIN-DATA`` holding one ``SDG GID="SWCRAFT"`` per entry of *groups*, each entry its children."""
    body = "".join(f'              <SDG GID="SWCRAFT"{group_attributes}>{children}</SDG>\n' for children in groups)
    return (
        f"          <ADMIN-DATA>\n            <SDGS>\n{body}{sdgs_extra}            </SDGS>\n          </ADMIN-DATA>\n"
    )


class TestTheMarkerGroupIsTheWritersOwnOnlyAsAWhole:
    """A ``SWCRAFT`` group is read only when it holds what the writer mints and nothing beside it.

    Otherwise an unknown ``SD`` or a nested ``SDG`` would be lost where a component import must abort
    on content it cannot represent; every case below expects the vendor-metadata line.
    """

    _LINE = (
        "ADMIN-DATA/SDG vendor metadata on SWC-INTERNAL-BEHAVIOR '{name}_InternalBehavior' "
        "has no SWCraft representation -- not imported or preserved"
    )

    @pytest.mark.parametrize(
        "admin_data",
        [
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD + '<SD GID="VENDOR-FLAG">1</SD>'), id="data-types-and-unknown-sd"
            ),
            pytest.param(
                _swcraft_groups(_ACCESS_SD + _DATA_TYPES_SD + '<SD GID="VENDOR-FLAG">1</SD>'),
                id="both-known-and-unknown-sd",
            ),
            pytest.param(
                _swcraft_groups(_ACCESS_SD + '<SD GID="VENDOR-FLAG">1</SD>'), id="access-points-and-unknown-sd"
            ),
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD + '<SDG GID="VENDOR"><SD GID="RAMBudget">1024</SD></SDG>'),
                id="data-types-and-nested-sdg",
            ),
            pytest.param(
                _swcraft_groups(_ACCESS_SD + '<SDG GID="VENDOR"><SD GID="RAMBudget">1024</SD></SDG>'),
                id="access-points-and-nested-sdg",
            ),
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD + '<SDX-REF DEST="SW-BASE-TYPE">/Types/B</SDX-REF>'),
                id="data-types-and-sdx-ref",
            ),
            pytest.param(_swcraft_groups(_DATA_TYPES_SD + '<SDF GID="RATIO">0.5</SDF>'), id="data-types-and-sdf"),
        ],
    )
    def test_a_known_sd_beside_any_other_child_makes_the_group_foreign(self, tmp_path: Path, admin_data: str) -> None:
        model = _admin_data_document(tmp_path, "BesideSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [self._LINE.format(name="BesideSwc")]
        assert model.swcs[0].swcraft_markers == {}

    @pytest.mark.parametrize(
        "admin_data",
        [
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD + _DATA_TYPES_SD.replace("base and", "other")), id="data-types"
            ),
            pytest.param(_swcraft_groups(_ACCESS_SD + _ACCESS_SD.replace("explicit", "other")), id="access-points"),
            pytest.param(_swcraft_groups(_DATA_TYPES_SD, _DATA_TYPES_SD), id="across-two-groups"),
            pytest.param(_swcraft_groups(_ACCESS_SD, _DATA_TYPES_SD), id="two-groups-disjoint"),
        ],
    )
    def test_a_repeated_known_sd_or_a_second_group_is_foreign_and_never_collapsed(
        self, tmp_path: Path, admin_data: str
    ) -> None:
        """A repeated known ``SD`` or a second group is foreign and never collapsed.

        The markers are keyed by ``GID``, so a second ``SD`` of one ``GID`` would keep one value and drop the other (two
        ``ACCESS-POINTS`` would read back as the second one's value, unreported). The writer mints one group per
        behaviour, so two groups are foreign even when their ``GID``s are disjoint."""
        model = _admin_data_document(tmp_path, "RepeatSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [self._LINE.format(name="RepeatSwc")]
        assert model.swcs[0].swcraft_markers == {}

    @pytest.mark.parametrize(
        "admin_data",
        [
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD.replace('GID="DATA-TYPES"', 'GID="DATA-TYPES" S="1"')), id="sd"
            ),
            pytest.param(_swcraft_groups(_DATA_TYPES_SD, group_attributes=' T="2026-01-01"'), id="sdg"),
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD.replace("base and", "<VT>base</VT> and")),
                id="an-element-inside-the-sd",
            ),
        ],
    )
    def test_an_attribute_or_an_element_the_writer_never_sets_makes_the_group_foreign(
        self, tmp_path: Path, admin_data: str
    ) -> None:
        """The writer sets ``GID`` and nothing else on both elements.

        An ``S`` checksum or a ``T`` timestamp is content this reader does not carry."""
        model = _admin_data_document(tmp_path, "AttributeSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [self._LINE.format(name="AttributeSwc")]
        assert model.swcs[0].swcraft_markers == {}

    def test_an_sdgs_holding_a_non_sdg_child_is_reported_beside_the_marker(self, tmp_path: Path) -> None:
        """The same hole one level up: a non-SDG child of ``SDGS`` is reported.

        The groups are read through ``SDGS/SDG``, so a sibling of them went unread and unreported."""
        admin_data = _swcraft_groups(_DATA_TYPES_SD, sdgs_extra='              <SDF GID="RATIO">0.5</SDF>\n')
        model = _admin_data_document(tmp_path, "SiblingSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [
            self._LINE.format(name="SiblingSwc")
            + " (foreign SDG <non-SDG content>; the SWCRAFT group beside it is read)"
        ]

    @pytest.mark.parametrize(
        ("admin_data", "markers"),
        [
            pytest.param(
                _swcraft_groups(_DATA_TYPES_SD),
                {"DATA-TYPES": "base and implementation types declared, no invented twin"},
                id="data-types-alone",
            ),
            pytest.param(
                _swcraft_groups(_ACCESS_SD + _DATA_TYPES_SD),
                {"ACCESS-POINTS": "explicit", "DATA-TYPES": "base and implementation types declared, no invented twin"},
                id="both-known-sds",
            ),
            pytest.param(_swcraft_groups(_ACCESS_SD), {"ACCESS-POINTS": "explicit"}, id="access-points-alone"),
        ],
    )
    def test_the_writers_own_shapes_are_still_read_and_not_reported(
        self, tmp_path: Path, admin_data: str, markers: dict[str, str]
    ) -> None:
        """The control: the three groups the writer emits, in the spelling ``_swcraft_groups`` gives them."""
        model = _admin_data_document(tmp_path, "OwnSwc", admin_data, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == markers


def _marker_block(
    *,
    children: str = _DATA_TYPES_SD,
    admin_text: str = "",
    sdgs_text: str = "",
    group_text: str = "",
    last_sd_tail: str = "",
    sdg_tail: str = "",
    sdgs_tail: str = "",
) -> str:
    """One ``ADMIN-DATA`` holding one ``SWCRAFT`` group, with character data spliced in at each level.

    Written without indentation, so that every ``.text`` and ``.tail`` the test does not set
    is ``None`` and the spliced string is the only character data in the block.
    """
    return (
        f"<ADMIN-DATA>{admin_text}<SDGS>{sdgs_text}"
        f'<SDG GID="SWCRAFT">{group_text}{children}{last_sd_tail}</SDG>{sdg_tail}'
        f"</SDGS>{sdgs_tail}</ADMIN-DATA>\n"
    )


class TestTheMarkerBlockCarriesNoCharacterDataBesideItsElements:
    """Character data in the marker block is judged as well as its children.

    The writer puts only XML whitespace in ``.text`` and ``.tail``, so anything else is content the
    exporter did not write: in the group it makes the group foreign; in ``SDGS`` or ``ADMIN-DATA`` it
    is reported beside the group, which is still read.
    """

    _LINE = TestTheMarkerGroupIsTheWritersOwnOnlyAsAWhole._LINE

    @pytest.mark.parametrize(
        "admin_data",
        [
            pytest.param(_marker_block(group_text="payload"), id="text-inside-the-group"),
            pytest.param(_marker_block(last_sd_tail="tailpayload"), id="a-tail-after-the-last-sd"),
            pytest.param(_marker_block(children=_ACCESS_SD + "between" + _DATA_TYPES_SD), id="a-tail-between-two-sds"),
            pytest.param(_marker_block(group_text="<![CDATA[payload]]>"), id="cdata-inside-the-group"),
            pytest.param(_marker_block(group_text="&#160;"), id="a-no-break-space-is-not-xml-whitespace"),
        ],
    )
    def test_character_data_inside_the_group_makes_it_foreign(self, tmp_path: Path, admin_data: str) -> None:
        model = _admin_data_document(tmp_path, "TextSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [self._LINE.format(name="TextSwc")]
        assert model.swcs[0].swcraft_markers == {}

    @pytest.mark.parametrize(
        ("admin_data", "token"),
        [
            pytest.param(_marker_block(sdgs_text="payload"), "<non-SDG content>", id="text-inside-sdgs"),
            pytest.param(_marker_block(sdg_tail="tailpayload"), "<non-SDG content>", id="a-tail-after-the-group"),
            pytest.param(_marker_block(admin_text="payload"), "<non-SDGS content>", id="text-inside-admin-data"),
            pytest.param(_marker_block(sdgs_tail="tailpayload"), "<non-SDGS content>", id="a-tail-after-sdgs"),
        ],
    )
    def test_character_data_beside_the_group_is_reported_beside_it(
        self, tmp_path: Path, admin_data: str, token: str
    ) -> None:
        """Character data beside the group: the group is read and the block is reported.

        The level-by-level rule, the same reading `<non-SDG content>` and `<non-SDGS content>`
        already give a stray element there."""
        model = _admin_data_document(tmp_path, "BesideTextSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [
            self._LINE.format(name="BesideTextSwc") + f" (foreign SDG {token}; the SWCRAFT group beside it is read)"
        ]

    @pytest.mark.parametrize(
        "admin_data",
        [
            pytest.param(_marker_block(), id="no-character-data"),
            pytest.param(
                _marker_block(
                    children=_ACCESS_SD + "\n \t" + _DATA_TYPES_SD,
                    admin_text="\n  ",
                    sdgs_text="\t",
                    group_text=" &#13;\n",
                    last_sd_tail="\n",
                    sdg_tail="  ",
                    sdgs_tail="\r\n",
                ),
                id="xml-whitespace-at-every-level",
            ),
        ],
    )
    def test_whitespace_is_still_read_and_not_reported(self, tmp_path: Path, admin_data: str) -> None:
        """The control: XML white space is still read and not reported.

        Space, tab, carriage return and line feed are what XML calls white
        space, and the only character data the writer's pretty-printer emits there."""
        model = _admin_data_document(tmp_path, "WhitespaceSwc", admin_data, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert "DATA-TYPES" in model.swcs[0].swcraft_markers

    def test_a_comment_or_processing_instruction_never_reaches_the_reader(self, tmp_path: Path) -> None:
        """The measured limit: a comment or processing instruction never reaches the reader.

        It is pinned so that a parser change is noticed: ``ET.parse`` with its
        default ``TreeBuilder`` discards comments and processing instructions before the reader
        sees the tree, as a child and as text alike, so neither can be judged and the group is
        read. Neither is content an AUTOSAR schema gives a meaning to."""
        admin_data = _marker_block(group_text="<!-- vendor note --><?vendor flag?>")
        model = _admin_data_document(tmp_path, "CommentSwc", admin_data, on_behavior=True)

        assert [e.tag for e in model.skip_log] == []
        assert model.swcs[0].swcraft_markers == {
            "DATA-TYPES": "base and implementation types declared, no invented twin"
        }


# An element's ADMIN-DATA is admitted only as the writer's one block

_OWN_SDGS = f'<SDGS><SDG GID="SWCRAFT">{_ACCESS_SD}{_DATA_TYPES_SD}</SDG></SDGS>'
_VENDOR_SDGS = '<SDGS><SDG GID="VENDOR"><SD GID="X">payload</SD></SDG></SDGS>'
_BOTH_MARKERS = {"ACCESS-POINTS": "explicit", "DATA-TYPES": "base and implementation types declared, no invented twin"}


def _admin(inner: str, attributes: str = "") -> str:
    """One ``ADMIN-DATA`` holding *inner*, unindented, so the only character data is what a test spells."""
    return f"<ADMIN-DATA{attributes}>{inner}</ADMIN-DATA>"


#: Every shape, measured through ``main``'s reader (before the positive rule) and the current one.
#: Columns: id, block, on the behaviour (else on the component), reported by ``main``, reported now,
#: markers read now. ``main`` is recorded as a measured literal, not imported.
_SHAPE_TABLE = [
    ("no-admin-data", "", True, False, False, {}),
    (
        "own-access-points",
        _admin(f'<SDGS><SDG GID="SWCRAFT">{_ACCESS_SD}</SDG></SDGS>'),
        True,
        False,
        False,
        {"ACCESS-POINTS": "explicit"},
    ),
    ("own-both-sds", _admin(_OWN_SDGS), True, False, False, _BOTH_MARKERS),
    (
        "own-data-types-alone",
        _admin(f'<SDGS><SDG GID="SWCRAFT">{_DATA_TYPES_SD}</SDG></SDGS>'),
        True,
        True,
        False,
        {"DATA-TYPES": "base and implementation types declared, no invented twin"},
    ),
    (
        "own-both-pretty-whitespace",
        "\n  " + _admin("\n   " + _OWN_SDGS.replace("<SDG ", "\n    <SDG ") + "\n  ") + "\n ",
        True,
        False,
        False,
        _BOTH_MARKERS,
    ),
    ("attribute-on-admin-data", _admin(_OWN_SDGS, ' T="1"'), True, False, True, _BOTH_MARKERS),
    ("attribute-on-sdgs", _admin(_OWN_SDGS.replace("<SDGS>", '<SDGS T="1">')), True, False, True, _BOTH_MARKERS),
    ("extra-empty-sdgs", _admin(_OWN_SDGS + "<SDGS/>"), True, False, True, _BOTH_MARKERS),
    ("second-sdgs-with-foreign-sdg", _admin(_OWN_SDGS + _VENDOR_SDGS), True, True, True, _BOTH_MARKERS),
    ("second-admin-data-with-foreign-sdg", _admin(_OWN_SDGS) + _admin(_VENDOR_SDGS), True, False, True, _BOTH_MARKERS),
    ("second-admin-data-empty", _admin(_OWN_SDGS) + "<ADMIN-DATA/>", True, False, True, _BOTH_MARKERS),
    ("second-admin-data-with-own-marker", _admin(_OWN_SDGS) + _admin(_OWN_SDGS), True, False, True, {}),
    ("foreign-admin-data-then-own", _admin(_VENDOR_SDGS) + _admin(_OWN_SDGS), True, True, True, _BOTH_MARKERS),
    ("text-after-admin-data", _admin(_OWN_SDGS) + "tailpayload", True, False, True, _BOTH_MARKERS),
    (
        "foreign-sdg-beside-marker",
        _admin(_OWN_SDGS.replace("</SDGS>", _VENDOR_SDGS[len("<SDGS>") :])),
        True,
        True,
        True,
        _BOTH_MARKERS,
    ),
    ("language-beside-marker", _admin("<LANGUAGE>EN</LANGUAGE>" + _OWN_SDGS), True, True, True, _BOTH_MARKERS),
    ("doc-revisions-alone", _admin("<DOC-REVISIONS/>"), True, True, True, {}),
    ("used-languages-beside-marker", _admin("<USED-LANGUAGES/>" + _OWN_SDGS), True, True, True, _BOTH_MARKERS),
    ("empty-admin-data", "<ADMIN-DATA/>", True, True, True, {}),
    ("empty-sdgs-alone", _admin("<SDGS/>"), True, True, True, {}),
    ("unknown-sd-in-group", _admin(_OWN_SDGS.replace("</SDG>", '<SD GID="V">1</SD></SDG>')), True, False, True, {}),
    ("text-inside-group", _admin(_OWN_SDGS.replace('GID="SWCRAFT">', 'GID="SWCRAFT">payload')), True, False, True, {}),
    ("text-inside-sdgs", _admin(_OWN_SDGS.replace("<SDGS>", "<SDGS>payload")), True, False, True, _BOTH_MARKERS),
    ("text-inside-admin-data", _admin("payload" + _OWN_SDGS), True, False, True, _BOTH_MARKERS),
    (
        "two-swcraft-groups-in-one-sdgs",
        _admin(f'<SDGS><SDG GID="SWCRAFT">{_ACCESS_SD}</SDG><SDG GID="SWCRAFT">{_DATA_TYPES_SD}</SDG></SDGS>'),
        True,
        True,
        True,
        {},
    ),
    ("vendor-sdg-on-component", _admin(_VENDOR_SDGS), False, True, True, {}),
    ("own-marker-on-component", _admin(_OWN_SDGS), False, True, True, {}),
    ("two-foreign-admin-data-on-component", _admin(_VENDOR_SDGS) + _admin(_VENDOR_SDGS), False, True, True, {}),
    ("empty-then-foreign-admin-data-on-component", "<ADMIN-DATA/>" + _admin(_VENDOR_SDGS), False, True, True, {}),
]


class TestAnElementsAdminDataIsAdmittedOnlyAsTheWritersOneBlock:
    """One positive rule decides whether an element's ``ADMIN-DATA`` goes unreported.

    Only the writer's exact block -- one ``ADMIN-DATA``, one ``SDGS``, one ``SWCRAFT`` group -- is
    admitted, and every other shape is reported by name (per-shape checks read past an attribute on
    ``ADMIN-DATA`` or ``SDGS``, a second ``SDGS`` and a second ``ADMIN-DATA``).
    """

    _LINE = TestTheMarkerGroupIsTheWritersOwnOnlyAsAWhole._LINE
    _READ = " (foreign SDG {tokens}; the SWCRAFT group beside it is read)"

    @pytest.mark.parametrize(
        ("admin_data", "tokens"),
        [
            pytest.param(
                _admin(_OWN_SDGS, ' T="2026-01-01"'), "<ADMIN-DATA attribute>", id="an-attribute-on-admin-data"
            ),
            pytest.param(
                _admin(_OWN_SDGS.replace("<SDGS>", '<SDGS T="2026-01-01">')),
                "<SDGS attribute>",
                id="an-attribute-on-sdgs",
            ),
            pytest.param(_admin(_OWN_SDGS + "<SDGS/>"), "<second SDGS>", id="an-extra-empty-sdgs"),
            pytest.param(
                _admin(_OWN_SDGS + _VENDOR_SDGS), "<second SDGS>, VENDOR", id="a-second-sdgs-holding-a-foreign-sdg"
            ),
            pytest.param(
                _admin(_OWN_SDGS) + _admin(_VENDOR_SDGS),
                "<second ADMIN-DATA>, VENDOR",
                id="a-second-admin-data-holding-a-foreign-sdg",
            ),
            pytest.param(
                _admin(_OWN_SDGS) + "<ADMIN-DATA/>", "<second ADMIN-DATA>", id="a-second-admin-data-that-is-empty"
            ),
            pytest.param(
                _admin(_VENDOR_SDGS) + _admin(_OWN_SDGS),
                "<second ADMIN-DATA>, VENDOR",
                id="the-marker-in-the-second-admin-data",
            ),
            pytest.param(_admin(_OWN_SDGS) + "tailpayload", "<content after ADMIN-DATA>", id="text-after-admin-data"),
        ],
    )
    def test_a_wrapper_the_writer_never_mints_is_reported_beside_the_marker(
        self, tmp_path: Path, admin_data: str, tokens: str
    ) -> None:
        model = _admin_data_document(tmp_path, "WrapperSwc", admin_data, on_behavior=True)

        assert [e.reason for e in model.skip_log] == [
            self._LINE.format(name="WrapperSwc") + self._READ.format(tokens=tokens)
        ]
        assert model.swcs[0].swcraft_markers == _BOTH_MARKERS

    def test_a_second_admin_data_holding_another_own_marker_is_foreign_and_nothing_is_read(
        self, tmp_path: Path
    ) -> None:
        """The writer mints one ``ADMIN-DATA`` block, so a second one is foreign.

        Two groups keyed by the same ``GID``s would keep one value and drop the other -- the two-groups rule, one level
        up."""
        model = _admin_data_document(tmp_path, "TwiceSwc", _admin(_OWN_SDGS) + _admin(_OWN_SDGS), on_behavior=True)

        assert [e.reason for e in model.skip_log] == [self._LINE.format(name="TwiceSwc")]
        assert model.swcs[0].swcraft_markers == {}

    @pytest.mark.parametrize(
        ("admin_data", "on_behavior", "reported", "markers"),
        [pytest.param(block, behaviour, now, read, id=shape) for shape, block, behaviour, _, now, read in _SHAPE_TABLE],
    )
    def test_the_shape_table(
        self, tmp_path: Path, admin_data: str, on_behavior: bool, reported: bool, markers: dict[str, str]
    ) -> None:
        model = _admin_data_document(tmp_path, "ShapeSwc", admin_data, on_behavior=on_behavior)

        assert [e.tag for e in model.skip_log] == (["ADMIN-DATA"] if reported else [])
        assert model.swcs[0].swcraft_markers == markers

    def test_no_shape_main_reported_is_admitted_but_the_data_types_marker(self) -> None:
        """Nothing ``main`` reported is narrowed, bar the data-types marker's one admission.

        That admission is a group holding ``DATA-TYPES`` alone, which ``main``'s reader did not know."""
        narrowed = [shape for shape, _, _, main_reported, now, _ in _SHAPE_TABLE if main_reported and not now]

        assert narrowed == ["own-data-types-alone"]

    @pytest.mark.parametrize(
        ("admin_data", "on_behavior"),
        [pytest.param(block, behaviour, id=shape) for shape, block, behaviour, _, now, _ in _SHAPE_TABLE if now],
    )
    def test_every_reported_shape_is_named(self, admin_data: str, on_behavior: bool) -> None:
        """Every reported shape is named.

        The naming helper's docstring claims a token for every way the rule refuses; this holds it
        to that over the table, so a suffix can never read ``(foreign SDG ; ...)``."""
        from swcraft_tools.arxml_reader import AR_NS, _foreign_admin_data, _is_the_writers_own_admin_data

        tag = "SWC-INTERNAL-BEHAVIOR" if on_behavior else "APPLICATION-SW-COMPONENT-TYPE"
        element = ET.fromstring(f'<{tag} xmlns="{AR_NS}"><SHORT-NAME>E</SHORT-NAME>{admin_data}</{tag}>')

        assert not _is_the_writers_own_admin_data(element)
        assert _foreign_admin_data(element)


# An element outside the AUTOSAR namespace is reported and not walked

_VENDOR_NS = "urn:vendor"
#: A foreign element holding an AUTOSAR-namespaced ``SHORT-NAME``: walked, it would be indexed as ``Hidden``.
_FOREIGN_EXTRA = (
    f'<x:EXTRA xmlns:x="{_VENDOR_NS}"><SHORT-NAME xmlns="http://autosar.org/schema/r4.0">Hidden</SHORT-NAME>'
    "payload</x:EXTRA>"
)


def _outside_line(local: str, namespace: str) -> str:
    return (
        f"element {local} outside the AUTOSAR namespace ({namespace or 'no namespace'}) "
        "has no SWCraft representation -- not imported or preserved"
    )


class TestAnElementOutsideTheAutosarNamespaceIsReportedAndNotWalked:
    """One check, first in the walk, for every element whose tag is not in the AUTOSAR namespace.

    The ``ADMIN-DATA`` rule looks in the AUTOSAR namespace only, so an ``<ADMIN-DATA xmlns="">`` or
    one in a vendor's namespace, walked through, would vanish from a component import. Such an
    element gets one skip entry tagged ``{namespace}LOCAL``, which no ``DEFERRED_ELEMENT_TAGS``
    member can equal, and nothing under it is walked.
    """

    @pytest.mark.parametrize("namespace", ["", _VENDOR_NS], ids=["no-namespace", "foreign-namespace"])
    @pytest.mark.parametrize("beside_the_marker", [True, False], ids=["beside-the-writers-marker", "alone"])
    def test_an_admin_data_outside_the_namespace_on_a_behaviour_is_reported(
        self, tmp_path: Path, namespace: str, beside_the_marker: bool
    ) -> None:
        outside = f'<ADMIN-DATA xmlns="{namespace}">{_VENDOR_SDGS}</ADMIN-DATA>'
        admin_data = _admin(_OWN_SDGS) + outside if beside_the_marker else outside
        model = _admin_data_document(tmp_path, "OutsideSwc", admin_data, on_behavior=True)

        assert [(e.tag, e.path, e.reason) for e in model.skip_log] == [
            (
                f"{{{namespace}}}ADMIN-DATA",
                "/Pkg/OutsideSwc/OutsideSwc_InternalBehavior/ADMIN-DATA",
                _outside_line("ADMIN-DATA", namespace),
            )
        ]
        assert model.swcs[0].swcraft_markers == (_BOTH_MARKERS if beside_the_marker else {})

    @pytest.mark.parametrize(
        ("build", "path"),
        [
            pytest.param(
                lambda tmp_path: _admin_data_document(tmp_path, "ExtraSwc", _FOREIGN_EXTRA),
                "/Pkg/ExtraSwc/EXTRA",
                id="on-a-component",
            ),
            pytest.param(
                lambda tmp_path: _admin_data_document(tmp_path, "ExtraSwc", _FOREIGN_EXTRA, on_behavior=True),
                "/Pkg/ExtraSwc/ExtraSwc_InternalBehavior/EXTRA",
                id="on-a-behaviour",
            ),
            pytest.param(
                lambda tmp_path: _admin_data_document(
                    tmp_path, "ExtraSwc", f"<RUNNABLES>{_FOREIGN_EXTRA}</RUNNABLES>", on_behavior=True
                ),
                "/Pkg/ExtraSwc/ExtraSwc_InternalBehavior/EXTRA",
                id="inside-runnables",
            ),
            pytest.param(
                lambda tmp_path: _interface_admin_data_document(
                    tmp_path, "IExtra", f"<DATA-ELEMENTS>{_FOREIGN_EXTRA}</DATA-ELEMENTS>"
                ),
                "/PortInterfaces/IExtra/EXTRA",
                id="inside-data-elements",
            ),
        ],
    )
    def test_a_foreign_element_is_reported_and_nothing_under_it_is_indexed(
        self, tmp_path: Path, build, path: str
    ) -> None:
        model = build(tmp_path)

        assert [(e.tag, e.path, e.reason) for e in model.skip_log] == [
            (f"{{{_VENDOR_NS}}}EXTRA", path, _outside_line("EXTRA", _VENDOR_NS))
        ]
        assert not [indexed for indexed in model.path_index if indexed.endswith("/Hidden")]

    @pytest.mark.parametrize("namespace", ["", _VENDOR_NS], ids=["no-namespace", "foreign-namespace"])
    def test_a_known_kind_outside_the_namespace_directly_in_elements_is_reported_and_never_deferred(
        self, tmp_path: Path, namespace: str
    ) -> None:
        """A known kind in another namespace directly in ``ELEMENTS`` is reported, never deferred.

        The ``ELEMENTS`` filter compares local names, so a known kind in another namespace passes it
        and reaches the check (measured). ``SWC-IMPLEMENTATION`` is a deferred tag: a bare local name
        in the skip entry would have turned the abort into a report-only line."""
        from swcraft_tools.arxml_import.report import DEFERRED_ELEMENT_TAGS

        document = tmp_path / "Known.arxml"
        document.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
            "  <AR-PACKAGES>\n"
            "    <AR-PACKAGE>\n"
            "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
            "      <ELEMENTS>\n"
            f'        <SWC-IMPLEMENTATION xmlns="{namespace}"><SHORT-NAME>Impl</SHORT-NAME></SWC-IMPLEMENTATION>\n'
            "      </ELEMENTS>\n"
            "    </AR-PACKAGE>\n"
            "  </AR-PACKAGES>\n"
            "</AUTOSAR>\n",
            encoding="utf-8",
            newline="\n",
        )

        model = read_arxml(str(document))

        assert [(e.tag, e.path) for e in model.skip_log] == [
            (f"{{{namespace}}}SWC-IMPLEMENTATION", "/Pkg/SWC-IMPLEMENTATION")
        ]
        assert not {entry.tag for entry in model.skip_log} & DEFERRED_ELEMENT_TAGS
        assert "/Pkg/Impl" not in model.path_index
