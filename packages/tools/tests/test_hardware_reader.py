"""Tests for the hardware layer reader ``read_hardware`` / ``find_hardware_file``.

An absent ``hardware/`` reads as ``[]``, the contract of every layer's reader, although the layer is mandatory:
``check-model`` reports it (``hardware.layer-absent``), so both halves are asserted below.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from swcraft_tools.reader import find_hardware_file, read_hardware
from swcraft_tools.schema import Core, Memory, Partition, Processor
from swcraft_tools.validator import check_hardware_topology

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEMO_MODEL = REPO_ROOT / "examples" / "basic" / "model"

VALID_PROCESSOR = {
    "kind": "processor",
    "name": "CPU",
    "description": "Concept document, table 4.1",
}

VALID_CORE = {
    "kind": "core",
    "name": "CORE2",
    "threshold": 0.7,
    "enforcement": "strict",
    "description": "Concept document, table 4.2",
}

VALID_MEMORY = {
    "kind": "memory",
    "name": "RAM0",
    "type": "RAM",
    "size": 262144,
    "accessible": ["CORE2"],
    "threshold": 0.8,
}


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


# read_hardware


class TestReadHardware:
    def test_reads_all_three_kinds(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "CPU.yaml", VALID_PROCESSOR)
        _write_yaml(tmp_path / "hardware" / "CORE2.yaml", VALID_CORE)
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        elements = read_hardware(str(tmp_path))
        assert len(elements) == 3
        by_name = {e.name: e for e in elements}
        assert isinstance(by_name["CPU"], Processor)
        assert isinstance(by_name["CORE2"], Core)
        assert isinstance(by_name["RAM0"], Memory)
        assert by_name["CORE2"].threshold == 0.7
        assert by_name["CORE2"].enforcement == "strict"
        assert by_name["CORE2"].hw_id is None
        assert by_name["CORE2"].description == "Concept document, table 4.2"
        assert by_name["RAM0"].type == "RAM"
        assert by_name["RAM0"].size == 262144
        assert by_name["RAM0"].accessible == ["CORE2"]
        # Default enforcement survives the YAML round trip.
        assert by_name["RAM0"].enforcement == "strict"

    def test_ordering_is_deterministic_by_filename(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "CORE2.yaml", VALID_CORE)
        _write_yaml(tmp_path / "hardware" / "CORE0.yaml", {**VALID_CORE, "name": "CORE0"})
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        names = [e.name for e in read_hardware(str(tmp_path))]
        assert names == ["CORE0", "CORE2", "RAM0"]
        assert names == [e.name for e in read_hardware(str(tmp_path))]

    def test_the_shipped_demo_model_reads_in_filename_order(self):
        """The committed ``examples/basic`` hardware reads in filename order."""
        elements = read_hardware(str(DEMO_MODEL))
        # A task runs in a partition.
        assert [e.name for e in elements] == ["CORE0", "CPU", "PARTITION0", "RAM0"]
        by_name = {e.name: e for e in elements}
        assert isinstance(by_name["CPU"], Processor)
        assert isinstance(by_name["CORE0"], Core)
        assert isinstance(by_name["RAM0"], Memory)
        assert isinstance(by_name["PARTITION0"], Partition)
        assert by_name["PARTITION0"].core == "CORE0"
        assert by_name["CORE0"].threshold == 0.7
        assert by_name["RAM0"].size == 262144
        assert by_name["RAM0"].accessible == ["CORE0"]

    def test_missing_hardware_dir_returns_empty_list(self, tmp_path):
        assert read_hardware(str(tmp_path)) == []

    def test_empty_hardware_dir_returns_empty_list(self, tmp_path):
        (tmp_path / "hardware").mkdir()
        assert read_hardware(str(tmp_path)) == []

    def test_a_missing_layer_is_the_validators_refusal_not_the_readers(self, tmp_path):
        """An absent ``hardware/`` is reported once by ``check-model``, the other half of the reader's silence."""
        assert read_hardware(str(tmp_path)) == []
        result = check_hardware_topology(str(tmp_path))
        assert not result.consistent
        assert [record.rule for record in result.records] == ["hardware.layer-absent"]

    def test_non_yaml_files_are_ignored(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        (tmp_path / "hardware" / "notes.txt").write_text("not a hardware element", encoding="utf-8")
        elements = read_hardware(str(tmp_path))
        assert [e.name for e in elements] == ["RAM0"]

    def test_invalid_hardware_yaml_raises_with_offending_filename(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "Bad.yaml", {**VALID_MEMORY, "size": 0})
        with pytest.raises(ValueError, match="Bad.yaml"):
            read_hardware(str(tmp_path))

    def test_unknown_kind_raises_with_offending_filename(self, tmp_path):
        _write_yaml(
            tmp_path / "hardware" / "Weird.yaml",
            {"kind": "flash_cycles", "name": "Weird", "threshold": 0.5},
        )
        with pytest.raises(ValueError, match="Weird.yaml"):
            read_hardware(str(tmp_path))

    def test_the_retired_kind_raises_with_offending_filename(self, tmp_path):
        """A retired ``cpu_core`` file is refused, and the message names the file to fix."""
        _write_yaml(
            tmp_path / "hardware" / "Core0.yaml",
            {"kind": "cpu_core", "name": "Core0", "core": 0, "threshold": 0.7},
        )
        with pytest.raises(ValueError, match="Core0.yaml"):
            read_hardware(str(tmp_path))

    def test_malformed_yaml_syntax_raises_with_offending_filename(self, tmp_path):
        broken_path = tmp_path / "hardware" / "Broken.yaml"
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("kind: memory\nname: [RAM0\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Broken.yaml"):
            read_hardware(str(tmp_path))


# find_hardware_file


class TestFindHardwareFile:
    def test_finds_existing_element_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        result = find_hardware_file(str(tmp_path), "RAM0")
        assert result is not None
        assert "RAM0.yaml" in result

    def test_matches_content_name_not_filename(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "renamed.yaml", VALID_MEMORY)
        result = find_hardware_file(str(tmp_path), "RAM0")
        assert result is not None
        assert "renamed.yaml" in result
        assert find_hardware_file(str(tmp_path), "renamed") is None

    def test_one_namespace_spans_the_three_kinds(self, tmp_path):
        """A processor, a core and a memory are found by the same lookup: ``hardware/`` is one namespace."""
        _write_yaml(tmp_path / "hardware" / "CPU.yaml", VALID_PROCESSOR)
        _write_yaml(tmp_path / "hardware" / "CORE2.yaml", VALID_CORE)
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        for name in ("CPU", "CORE2", "RAM0"):
            assert find_hardware_file(str(tmp_path), name) is not None

    def test_nonexistent_element_returns_none(self, tmp_path):
        _write_yaml(tmp_path / "hardware" / "RAM0.yaml", VALID_MEMORY)
        assert find_hardware_file(str(tmp_path), "ROM") is None

    def test_missing_hardware_dir_returns_none(self, tmp_path):
        assert find_hardware_file(str(tmp_path), "RAM0") is None
