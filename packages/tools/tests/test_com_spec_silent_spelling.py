"""A com-spec value the model states at the writer's silent value survives the project's own export.

``PortComSpec`` accepts an attribute equal to the writer's silent value (``alive_timeout: 0``,
``enable_update: false``, ``handle_out_of_range: none``, ...), an entry holding only ``data_element``, and the
entries in any order. The writer writes each as silence and the importer records only what differs from it, so
the file cannot say which spelling wrote it. Held at both ARXML versions: the spellings are one ARXML, which
makes ``import component``'s projection exact; the project's own unchanged export changes nothing;
``--update`` keeps the target's spelling beside a real change, and its preview names no com-spec it did not
change; a value that is not silent is still a difference, its removal named before the write.

What this module does **not** reach: ``import model`` builds a model from the file alone and returns
the unstated form (``test_behavioural_import.py``'s
``test_a_swcraft_written_file_carries_no_com_specs_and_no_line``), which is an equivalence and no
loss; a queued element's ``queue_length`` has no silent value, so only the projection's unit case
names it; and the MCP draft of the same import is ``packages/mcp/tests/test_import_change_set.py``'s.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import ComponentImportConflictError, export_component, import_component
from swcraft_tools.component_exchange.divergence import _projected_com_specs
from swcraft_tools.component_exchange.target_carry import keep_target_com_spec_spelling
from swcraft_tools.schema import Port, PortComSpec, SWComponent
from swcraft_tools.writer.arxml_generation import COM_SPEC_SILENT_VALUES, generate_arxml
from test_behavioural_emission import EXCHANGE_MODEL_ROOT, _change, _dump, exchange_model, set_port

NS = f"{{{NAMESPACE}}}"
VERSIONS = ("4.2.2", "4.3.0")

RECEIVER = ("ActuatorSwc", "rSpeed")
SENDER = ("SensorSwc", "pSpeed")

#: (case id, component and port, the entries as the user wrote them), each the unstated form's ARXML.
SILENT_SPELLINGS: list[tuple[str, tuple[str, str], list[dict[str, Any]]]] = [
    (
        "receiver-all-silent",
        RECEIVER,
        [
            {
                "data_element": "speed",
                "enable_update": False,
                "alive_timeout": 0.0,
                "handle_never_received": False,
                "handle_timeout_type": "none",
                "handle_out_of_range": "none",
                "uses_end_to_end_protection": False,
            }
        ],
    ),
    ("receiver-attribute-less", RECEIVER, [{"data_element": "speed"}]),
    ("receiver-single", RECEIVER, [{"data_element": "speed", "alive_timeout": 0.0}]),
    (
        "sender-all-silent",
        SENDER,
        [{"data_element": "speed", "handle_out_of_range": "none", "uses_end_to_end_protection": False}],
    ),
    ("sender-attribute-less", SENDER, [{"data_element": "speed"}]),
    ("sender-single", SENDER, [{"data_element": "speed", "uses_end_to_end_protection": False}]),
]

spellings = pytest.mark.parametrize(
    ("owner", "entries"), [pytest.param(owner, entries, id=case) for case, owner, entries in SILENT_SPELLINGS]
)
versions = pytest.mark.parametrize("version", VERSIONS)


def _project(tmp_path: Path, *patches: Callable[[Path], None]) -> Path:
    """A scaffolded project holding the ``component_exchange`` model with *patches* applied; its model root."""
    project = init_project(tmp_path / "target")
    shutil.rmtree(project / "model")
    return exchange_model(project / "model", *patches)


def _stating(owner: tuple[str, str], entries: list[dict[str, Any]]) -> Callable[[Path], None]:
    return lambda root: set_port(root, *owner, com_specs=entries)


def _two_elements(order: list[str]) -> Callable[[Path], None]:
    """A two-element interface on a new ``SensorSwc`` provide port, its com-spec entries in *order*."""

    def patch(model_root: Path) -> None:
        _dump(
            model_root / "interfaces" / "IPair.yaml",
            {
                "name": "IPair",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "a", "implementation_type": "uint8"},
                    {"name": "b", "implementation_type": "uint8"},
                ],
            },
        )
        _change(
            model_root,
            "swc/sensing/SensorSwc.yaml",
            lambda d: d["ports"].insert(
                2,
                {
                    "name": "pPair",
                    "direction": "provide",
                    "interface": "IPair",
                    "com_specs": [{"data_element": name, "uses_end_to_end_protection": True} for name in order],
                },
            ),
        )

    return patch


def _port(model_root: Path, swc: str, port: str) -> dict[str, Any]:
    (path,) = list((model_root / "swc").rglob(f"{swc}.yaml"))
    (found,) = [p for p in yaml.safe_load(path.read_text(encoding="utf-8"))["ports"] if p["name"] == port]
    return found


def _own_export(model_root: Path, swc: str, target: Path, version: str) -> Path:
    export_component(str(model_root), swc, str(target), version=version)
    return target


def _patched(export: Path, target: Path, patch: Callable[[etree._ElementTree], None]) -> Path:
    tree = etree.parse(str(export))
    patch(tree)
    target.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return target


def _another_period(tree: etree._ElementTree) -> None:
    """A change of the component's own that is no com-spec: its timing event's period."""
    (period,) = list(tree.iter(f"{NS}PERIOD"))
    period.text = "0.5"


def _state(port: str, tag: str, text: str) -> Callable[[etree._ElementTree], None]:
    """State *tag* as *text* in the one sender/receiver com-spec of *port*."""

    def patch(tree: etree._ElementTree) -> None:
        (prototype,) = [
            node
            for node in tree.iter(f"{NS}P-PORT-PROTOTYPE", f"{NS}R-PORT-PROTOTYPE")
            if node.findtext(f"{NS}SHORT-NAME") == port
        ]
        (leaf,) = list(prototype.iter(f"{NS}{tag}"))
        leaf.text = text

    return patch


class TestTheSpellingsAreOneArxml:
    """The exactness of the projection: each spelling writes the unstated form's bytes."""

    @spellings
    @versions
    def test_a_spelling_generates_the_bytes_of_silence(
        self, tmp_path: Path, owner: tuple[str, str], entries: list[dict[str, Any]], version: str
    ) -> None:
        model_root = exchange_model(tmp_path / "model", _stating(owner, entries))
        generate_arxml(str(model_root), str(tmp_path / "stated.arxml"), version=version)
        generate_arxml(str(EXCHANGE_MODEL_ROOT), str(tmp_path / "silent.arxml"), version=version)

        assert (tmp_path / "stated.arxml").read_bytes() == (tmp_path / "silent.arxml").read_bytes()

    @spellings
    @versions
    def test_a_spelling_exports_the_bytes_of_silence(
        self, tmp_path: Path, owner: tuple[str, str], entries: list[dict[str, Any]], version: str
    ) -> None:
        model_root = exchange_model(tmp_path / "model", _stating(owner, entries))
        stated = _own_export(model_root, owner[0], tmp_path / "stated.arxml", version)
        silent = _own_export(EXCHANGE_MODEL_ROOT, owner[0], tmp_path / "silent.arxml", version)

        assert stated.read_bytes() == silent.read_bytes()

    @versions
    def test_the_order_of_the_entries_is_no_byte(self, tmp_path: Path, version: str) -> None:
        first = exchange_model(tmp_path / "first", _two_elements(["a", "b"]))
        second = exchange_model(tmp_path / "second", _two_elements(["b", "a"]))
        generate_arxml(str(first), str(tmp_path / "first.arxml"), version=version)
        generate_arxml(str(second), str(tmp_path / "second.arxml"), version=version)

        assert (tmp_path / "first.arxml").read_bytes() == (tmp_path / "second.arxml").read_bytes()

    def test_the_all_silent_case_states_every_attribute_of_the_writers_table(self) -> None:
        """A seventh silent attribute fails here until the cases state it."""
        (_case, _owner, (entry,)) = SILENT_SPELLINGS[0]
        assert {attribute.field for attribute in COM_SPEC_SILENT_VALUES.values()} == set(entry) - {"data_element"}
        assert all(entry[attribute.field] == attribute.silent for attribute in COM_SPEC_SILENT_VALUES.values())


class TestTheOwnExportIsUnchanged:
    @spellings
    @versions
    @pytest.mark.parametrize("update", [False, True], ids=["plain", "update"])
    def test_the_unchanged_export_is_unchanged_and_writes_nothing(
        self, tmp_path: Path, owner: tuple[str, str], entries: list[dict[str, Any]], version: str, update: bool
    ) -> None:
        model_root = _project(tmp_path, _stating(owner, entries))
        export = _own_export(model_root, owner[0], tmp_path / "own.arxml", version)
        before = files_of(model_root)

        report = import_component(str(export), str(model_root), update=update)

        assert report.swc_action == "unchanged"
        assert files_of(model_root) == before

    @versions
    def test_a_silent_value_beside_a_stated_one_is_unchanged(self, tmp_path: Path, version: str) -> None:
        entries = [{"data_element": "speed", "enable_update": True, "alive_timeout": 0.0}]
        model_root = _project(tmp_path, _stating(RECEIVER, entries))
        export = _own_export(model_root, "ActuatorSwc", tmp_path / "own.arxml", version)

        assert import_component(str(export), str(model_root)).swc_action == "unchanged"

    @versions
    def test_the_entries_in_another_order_are_unchanged_and_keep_their_order(
        self, tmp_path: Path, version: str
    ) -> None:
        model_root = _project(tmp_path, _two_elements(["b", "a"]))
        export = _own_export(model_root, "SensorSwc", tmp_path / "own.arxml", version)

        assert import_component(str(export), str(model_root)).swc_action == "unchanged"

        source = _patched(export, tmp_path / "period.arxml", _another_period)
        assert import_component(str(source), str(model_root), update=True).swc_action == "updated"
        assert [entry["data_element"] for entry in _port(model_root, "SensorSwc", "pPair")["com_specs"]] == ["b", "a"]


class TestUpdateKeepsTheTargetsSpelling:
    @spellings
    @versions
    def test_a_change_elsewhere_leaves_the_entries_in_place(
        self, tmp_path: Path, owner: tuple[str, str], entries: list[dict[str, Any]], version: str
    ) -> None:
        model_root = _project(tmp_path, _stating(owner, entries))
        export = _own_export(model_root, owner[0], tmp_path / "own.arxml", version)
        source = _patched(export, tmp_path / "period.arxml", _another_period)

        with pytest.raises(ComponentImportConflictError):
            import_component(str(source), str(model_root))
        preview = import_component(str(source), str(model_root), update=True, dry_run=True)
        report = import_component(str(source), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _port(model_root, *owner)["com_specs"] == entries
        assert "(timing, 0.5)" in preview.swc_diff_preview
        assert "com_specs" not in preview.swc_diff_preview

    @versions
    def test_a_value_the_file_states_joins_the_silent_one_the_target_states(self, tmp_path: Path, version: str) -> None:
        model_root = _project(tmp_path, _stating(RECEIVER, [{"data_element": "speed", "alive_timeout": 0.0}]))
        export = _own_export(model_root, "ActuatorSwc", tmp_path / "own.arxml", version)
        source = _patched(export, tmp_path / "stated.arxml", _state("rSpeed", "ENABLE-UPDATE", "true"))

        report = import_component(str(source), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _port(model_root, *RECEIVER)["com_specs"] == [
            {"data_element": "speed", "enable_update": True, "alive_timeout": 0.0}
        ]

    @versions
    def test_a_value_the_file_states_replaces_the_silent_one_of_its_own_attribute(
        self, tmp_path: Path, version: str
    ) -> None:
        entries = [{"data_element": "speed", "handle_out_of_range": "none", "uses_end_to_end_protection": False}]
        model_root = _project(tmp_path, _stating(SENDER, entries))
        export = _own_export(model_root, "SensorSwc", tmp_path / "own.arxml", version)
        source = _patched(export, tmp_path / "stated.arxml", _state("pSpeed", "HANDLE-OUT-OF-RANGE", "IGNORE"))

        import_component(str(source), str(model_root), update=True)

        assert _port(model_root, *SENDER)["com_specs"] == [
            {"data_element": "speed", "handle_out_of_range": "ignore", "uses_end_to_end_protection": False}
        ]


class TestAStatedValueIsStillADifference:
    @versions
    def test_a_value_the_target_states_and_the_file_does_not(self, tmp_path: Path, version: str) -> None:
        model_root = _project(tmp_path, _stating(RECEIVER, [{"data_element": "speed", "alive_timeout": 0.0}]))
        export = _own_export(model_root, "ActuatorSwc", tmp_path / "silent.arxml", version)
        set_port(
            model_root, *RECEIVER, com_specs=[{"data_element": "speed", "alive_timeout": 0.0, "enable_update": True}]
        )

        with pytest.raises(ComponentImportConflictError):
            import_component(str(export), str(model_root))
        preview = import_component(str(export), str(model_root), update=True, dry_run=True)
        import_component(str(export), str(model_root), update=True)

        # The removal is named before the write, and the silent spelling beside it stays.
        assert "enable_update" in preview.swc_diff_preview
        assert _port(model_root, *RECEIVER)["com_specs"] == [{"data_element": "speed", "alive_timeout": 0.0}]

    def test_a_queue_length_is_never_projected_away(self) -> None:
        entry = PortComSpec(data_element="event", queue_length=1, handle_out_of_range="none")
        assert _projected_com_specs([entry]) == [PortComSpec(data_element="event", queue_length=1)]


class TestTheProjectionAndTheKeptSpelling:
    """The two helpers on model objects: the branches no file-level case above reaches."""

    @staticmethod
    def _swc(direction: str, interface: str, entries: list[PortComSpec]) -> SWComponent:
        port = Port(name="p", direction=direction, interface=interface, com_specs=entries)
        return SWComponent(name="C", type="application", ports=[port])

    def test_the_projection_drops_the_three_spellings(self) -> None:
        entries = [
            PortComSpec(data_element="c", enable_update=True, alive_timeout=0),
            PortComSpec(data_element="a", handle_timeout_type="none"),
            PortComSpec(data_element="d"),
            PortComSpec(data_element="b", handle_never_received=True),
        ]
        assert _projected_com_specs(entries) == [
            PortComSpec(data_element="b", handle_never_received=True),
            PortComSpec(data_element="c", enable_update=True),
        ]
        assert _projected_com_specs([]) == []

    def test_a_port_the_file_flips_or_re_points_keeps_nothing(self) -> None:
        target = self._swc("require", "I", [PortComSpec(data_element="d", alive_timeout=0.0)])
        for incoming in (self._swc("provide", "I", []), self._swc("require", "J", [])):
            assert keep_target_com_spec_spelling(target, incoming).ports[0].com_specs == []

    def test_an_entry_the_file_removes_keeps_only_its_silent_spelling(self) -> None:
        target = self._swc(
            "require",
            "I",
            [
                PortComSpec(data_element="gone", enable_update=True),
                PortComSpec(data_element="half", enable_update=True, alive_timeout=0.0),
                PortComSpec(data_element="bare"),
            ],
        )
        incoming = self._swc("require", "I", [PortComSpec(data_element="new", enable_update=True)])

        assert keep_target_com_spec_spelling(target, incoming).ports[0].com_specs == [
            PortComSpec(data_element="half", alive_timeout=0.0),
            PortComSpec(data_element="bare"),
            PortComSpec(data_element="new", enable_update=True),
        ]

    def test_the_kept_component_projects_as_the_file_does(self) -> None:
        """Keeping the spelling changes nothing the comparison sees."""
        target = self._swc("require", "I", [PortComSpec(data_element="d", alive_timeout=0.0, enable_update=False)])
        incoming = self._swc("require", "I", [PortComSpec(data_element="d", handle_never_received=True)])

        kept = keep_target_com_spec_spelling(target, incoming)

        assert kept.ports[0].com_specs == [
            PortComSpec(data_element="d", handle_never_received=True, alive_timeout=0.0, enable_update=False)
        ]
        assert _projected_com_specs(kept.ports[0].com_specs) == _projected_com_specs(incoming.ports[0].com_specs)
