"""``ModelState`` and the generic model loader.

**These equality tests are delegation tests, not oracle tests.** The ``read_*`` wrappers are
one-line delegations to ``load_layer``, so comparing ``load_model_state`` with them catches a
wrapper that stopped delegating or names the wrong layer, nothing more. The behavioral oracle for
the loader is ``packages/tools/tests/test_reader.py`` and :data:`READER_LABELS`, a frozen copy of
the labels the readers used. Do not read a green run of the equality tests as evidence that the
loader reads a model correctly.

**What these tests do not reach.** A layer whose storage record is *not* one directory of one
suffix -- a nesting depth beyond one, a second placement field, a suffix that is not ``.yaml`` --
because the contract declares none. Nor ``check-model``'s complaint about a missing required
layer: the loader records the absence and the gate owns what it means (``hardware.layer-absent``).

**The import-graph assertion.** ``model_state`` sits *below* ``reader``, ``snapshot`` and
``differ``, so an import in the wrong direction is a cycle. It is asserted over the AST, so a
conditional or function-local import is caught too.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, write_model_element
from pydantic import TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from swcraft_tools import model_state as ms
from swcraft_tools import reader, schema, snapshot
from swcraft_tools.editing import registry as editing_registry
from swcraft_tools.editing.read import publish_element
from swcraft_tools.model_registry import ModelRegistry, current_registry, production_registry, use_registry
from swcraft_tools.model_state import ModelState, load_model_state, materialize_revision, parse_element
from synthetic_support import SyntheticElement, merged_registry

BASIC_MODEL = REPO_ROOT / "examples" / "basic" / "model"

TOOLS_SRC = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"

#: layer name -> the reader whose output this layer's elements must equal. Spelled out rather
#: than derived from the registry, so the pairing is not recomputed by the code under test.
READERS = {
    "swc": reader.read_swc_model,
    "interfaces": reader.read_interfaces,
    "connectors": reader.read_connectors,
    "implementation_types": reader.read_data_types,
    "requirements": reader.read_requirements,
    "compositions": reader.read_compositions,
    "tasks": reader.read_tasks,
    "sequences": reader.read_sequences,
    "hardware": reader.read_hardware,
    "mapping_sets": reader.read_mapping_sets,
}

#: layer name -> the word a refusal names it by, transcribed from the readers' former call
#: sites: the oracle that the loader's derived label reproduces each.
READER_LABELS = {
    "swc": "SWC",
    "interfaces": "interface",
    "connectors": "connector",
    "implementation_types": "implementation type",
    "requirements": "requirement",
    "compositions": "composition",
    "tasks": "task",
    "sequences": "sequence",
    "hardware": "hardware",
}


# ---------------------------------------------------------------------------
# A model tree covering all fifteen layers
# ---------------------------------------------------------------------------


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def full_model(root: Path) -> Path:
    """A model with an element in every layer, and a grouped, a flat and a nested SWC."""
    _write(
        root / "compositions" / "Powertrain.yaml",
        {"name": "Powertrain", "title": "Powertrain", "root": True},
    )
    _write(
        root / "swc" / "sensors" / "SpeedSensor.yaml",
        {
            "name": "SpeedSensor",
            "type": "application",
            "ports": [{"name": "pOut", "direction": "provide", "interface": "ISpeed"}],
            "runnables": [{"name": "SenseRun", "events": [{"type": "timing", "period": 0.01}]}],
            "memory": {"RAM": 512},
            "satisfies": ["REQ_001@1"],
        },
    )
    _write(root / "swc" / "Flat.yaml", {"name": "Flat", "type": "service"})
    _write(root / "swc" / "a" / "b" / "Deep.yaml", {"name": "Deep", "type": "application"})
    _write(
        root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        },
    )
    _write(
        root / "interfaces" / "IDiag.yaml",
        {"name": "IDiag", "kind": "client_server", "operations": [{"name": "Read", "arguments": []}]},
    )
    _write(
        root / "connectors" / "Speed_to_Flat.yaml",
        {"name": "Speed_to_Flat", "from": {"swc": "SpeedSensor", "port": "pOut"}, "to": {"swc": "Flat", "port": "rIn"}},
    )
    write_model_element(root, {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
    write_model_element(root, {"name": "Speed", "kind": "value_type", "base": "float32"})
    # A second `implementation_types` kind: the multi-kind loader test needs two classes here.
    write_model_element(root, {"name": "SpeedRef", "kind": "reference_type", "base": "Speed"})
    write_model_element(root, {"name": "Gear", "kind": "enum", "literals": [{"name": "Park", "value": 0}]})
    _write(
        root / "requirements" / "REQ_001.yaml",
        {
            "id": "REQ_001",
            "title": "Speed is sensed",
            "description": "The system shall sense speed.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )
    _write(
        root / "tasks" / "Task_10ms.yaml",
        {
            "name": "Task_10ms",
            "period": 0.01,
            "priority": 5,
            "partition": "PARTITION0",
            "wcet": "1200us",
            "runnables": [{"swc": "SpeedSensor", "runnable": "SenseRun"}],
        },
    )
    _write(
        root / "sequences" / "speed_cycle.yaml",
        {
            "name": "speed_cycle",
            "task": "Task_10ms",
            "title": "Speed cycle",
            "steps": [{"swc": "SpeedSensor", "runnable": "SenseRun"}],
        },
    )
    _write(root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    _write(
        root / "hardware" / "CORE0.yaml",
        {"name": "CORE0", "kind": "core", "threshold": 0.7},
    )
    _write(
        root / "hardware" / "RAM0.yaml",
        {
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0"],
            "threshold": 0.8,
        },
    )
    write_model_element(
        root, {"name": "Physical", "maps": [{"application": "Kph", "implementation": "Speed"}]}, layer="mapping_sets"
    )
    # The forms a type names by reference, one element each (the compu methods' two kinds, for
    # the multi-kind loader's reason).
    write_model_element(root, {"name": "Kmh", "display_name": "km/h"}, layer="units")
    write_model_element(root, {"name": "ProbeLinearScaling", "kind": "linear", "unit": "Kmh", "factor": 0.1})
    write_model_element(root, {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "Park", "value": 0}]})
    write_model_element(root, {"name": "Kph_Limits", "min": 0, "max": 300}, layer="data_constraints")
    return root


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return full_model(tmp_path / "model")


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


# ---------------------------------------------------------------------------
# The state is the registry's layers, and the readers' elements
# ---------------------------------------------------------------------------


def test_the_state_carries_every_layer_in_registry_order(model: Path) -> None:
    state = load_model_state(model)
    assert list(state) == list(production_registry().layer_names)
    assert tuple(state.layer_names) == production_registry().layer_names


@pytest.mark.parametrize("layer", sorted(READERS))
def test_every_layer_equals_todays_reader_on_a_full_model(model: Path, layer: str) -> None:
    elements = load_model_state(model)[layer]
    assert elements == READERS[layer](str(model))
    assert elements, f"{layer}: the fixture model left this layer empty, so the comparison is vacuous"


@pytest.mark.parametrize("layer", sorted(READERS))
def test_every_layer_equals_todays_reader_on_the_shipped_example(layer: str) -> None:
    """The same equality against ``examples/basic``, a model not written for this test."""
    assert load_model_state(BASIC_MODEL)[layer] == READERS[layer](str(BASIC_MODEL))


def test_the_placement_map_equals_read_swc_model_with_placement(model: Path) -> None:
    state = load_model_state(model)
    expected = {element.name: placement for element, placement in reader.read_swc_model_with_placement(str(model))}
    assert state.placement["swc"] == expected
    assert expected["SpeedSensor"] == {"group": "sensors"}
    assert expected["Flat"] == {"group": ""}
    assert expected["Deep"] == {"group": "a/b"}, "a nested file reports its relative directory verbatim"


def test_the_state_and_the_snapshot_both_carry_the_identity_order(tmp_path: Path) -> None:
    """Elements are in identity order, not file order, in the state and in the snapshot.

    ``_read_state`` bypasses ``load_layer``, so a sort in ``load_layer`` alone would leave it
    in file order; the snapshot is what the protocol's reads and ``differ.diff_snapshots`` see.
    """
    model = tmp_path / "model"
    for filename, identity in (("b.yaml", "A"), ("a.yaml", "B")):
        _write(model / "tasks" / filename, {"name": identity, "period": 0.01, "priority": 1, "partition": "PARTITION0"})
    _write(model / "swc" / "g" / "b.yaml", {"name": "Alpha", "type": "application"})
    _write(model / "swc" / "g" / "a.yaml", {"name": "Bravo", "type": "application"})

    state = load_model_state(model)
    assert [task.name for task in state["tasks"]] == ["A", "B"]
    assert [swc.name for swc in state["swc"]] == ["Alpha", "Bravo"]
    assert list(state.placement_of("swc")) == ["Alpha", "Bravo"]

    loaded = snapshot.load_snapshot(str(model))
    assert [task.name for task in loaded.tasks] == ["A", "B"]
    assert [swc.name for swc in loaded.swcs] == ["Alpha", "Bravo"]


def test_a_layer_with_no_placement_record_has_no_placement_entry(model: Path) -> None:
    state = load_model_state(model)
    assert set(state.placement) == {"swc"}
    assert state.placement_of("tasks") == {}


def test_an_unknown_layer_is_a_key_error(model: Path) -> None:
    state = load_model_state(model)
    with pytest.raises(KeyError):
        state["nonesuch"]
    assert "nonesuch" not in state
    assert "tasks" in state


# ---------------------------------------------------------------------------
# Absence, emptiness and foreign files
# ---------------------------------------------------------------------------


def test_a_missing_layer_is_empty_and_recorded_absent(model: Path) -> None:
    """A missing layer reads as ``[]`` and is recorded absent; ``check-model`` owns the complaint."""
    for path in sorted((model / "hardware").iterdir(), key=lambda path: path.as_posix()):
        path.unlink()
    (model / "hardware").rmdir()

    state = load_model_state(model)
    assert state["hardware"] == []
    assert state["hardware"] == reader.read_hardware(str(model))
    assert "hardware" in state.absent
    assert state.absent == frozenset({"hardware"})


def test_a_present_but_empty_layer_is_not_absent(model: Path) -> None:
    for path in sorted((model / "hardware").iterdir(), key=lambda path: path.as_posix()):
        path.unlink()
    state = load_model_state(model)
    assert state["hardware"] == []
    assert state.absent == frozenset()


def test_a_foreign_suffix_is_read_by_no_layer(model: Path) -> None:
    """A file the storage record's suffix does not name is neither read nor a failure."""
    (model / "swc" / "Ignored.yml").write_text("name: Ignored\n", encoding="utf-8")
    (model / "interfaces" / "notes.txt").write_text("not a model file\n", encoding="utf-8")
    (model / "tasks" / "Task_10ms.yaml.bak").write_text("{{ not yaml\n", encoding="utf-8")

    state = load_model_state(model)
    assert [element.name for element in state["swc"]] == ["Flat", "Deep", "SpeedSensor"]
    assert [element.name for element in state["interfaces"]] == ["IDiag", "ISpeed"]
    assert [element.name for element in state["tasks"]] == ["Task_10ms"]
    for layer in READERS:
        assert state[layer] == READERS[layer](str(model))


def test_a_missing_model_root_is_todays_file_not_found(tmp_path: Path) -> None:
    """``read_swc_model_with_placement``'s refusal, type and message both.

    ``load_snapshot`` catches this exact exception type and re-raises it as a ``ValueError``.
    """
    missing = tmp_path / "absent"
    with pytest.raises(FileNotFoundError, match="Model root not found"):
        load_model_state(missing)
    with pytest.raises(FileNotFoundError, match="Model root not found"):
        reader.read_swc_model_with_placement(str(missing))


# ---------------------------------------------------------------------------
# Refusals: the moved parser names the file and carries the target
# ---------------------------------------------------------------------------


def test_a_malformed_file_is_refused_with_the_readers_message_and_target(model: Path) -> None:
    (model / "tasks" / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(reader.ElementParseError) as raised:
        load_model_state(model)
    assert raised.value.target == "tasks/Broken.yaml"
    assert str(model / "tasks" / "Broken.yaml") in raised.value.render(full_path=True)
    assert isinstance(raised.value, ValueError)


def test_an_invalid_element_is_refused_the_same_way(model: Path) -> None:
    _write(model / "tasks" / "NoPeriod.yaml", {"name": "NoPeriod", "priority": 5})
    with pytest.raises(reader.ElementParseError) as raised:
        load_model_state(model)
    assert raised.value.target == "tasks/NoPeriod.yaml"


# ---------------------------------------------------------------------------
# One line per field
# ---------------------------------------------------------------------------

#: A task with two field errors, the file every multi-line case below reads.
TWO_ERRORS_TASK = "name: T\nperiod: .nan\npriority: x\npartition: P\n"


def _refusal(tmp_path: Path, layer: str, text: str) -> ms.ElementParseError:
    """The refusal of a model whose one file, ``Bad`` in *layer*, holds *text*."""
    root = tmp_path / "model"
    facts = production_registry().layer(layer)
    path = root / facts.storage.directory / f"Bad{facts.storage.file_suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(ms.ElementParseError) as raised:
        reader.read_layer(str(root), layer)
    return raised.value


class TestOneLinePerField:
    """``details`` is one sentence per field; ``str()`` and ``render`` stay one line."""

    def test_a_missing_id_on_a_requirement_is_the_field_and_pydantics_message(self, tmp_path: Path) -> None:
        refusal = _refusal(
            tmp_path, "requirements", "title: t\ndescription: d\nversion: 1\nsource: external\ntype: functional\n"
        )
        assert refusal.details == ("id: Field required",)
        assert refusal.render_lines(full_path=False) == [
            "Invalid requirement YAML in requirements/Bad.yaml: id: Field required"
        ]

    def test_a_nested_field_is_keys_and_indices(self, tmp_path: Path) -> None:
        refusal = _refusal(
            tmp_path, "swc", "name: S\nkind: application\nports:\n- name: p\n  direction: provide\n  interface: 5\n"
        )
        assert refusal.details == ("ports[0].interface: Input should be a valid string",)

    def test_a_discriminated_unions_tag_is_dropped(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "hardware", "name: P1\nkind: partition\ncore: 5\n")
        assert refusal.details == ("core: Input should be a valid string",)

    def test_a_key_spelled_like_the_kind_is_still_the_dropped_tag(self, tmp_path: Path) -> None:
        # `partition` is both the mapping's `kind` and a key it holds: the step is the member's
        # tag, dropped, so the error reads as `core`'s and never as `partition.core`'s.
        refusal = _refusal(tmp_path, "hardware", "name: P1\nkind: partition\npartition: x\ncore: 5\n")
        assert refusal.details == ("core: Input should be a valid string",)

    @pytest.mark.parametrize("text", ["", "- x\n", "5\n"], ids=["empty", "list", "scalar"])
    @pytest.mark.parametrize(
        ("layer", "message"),
        [
            ("tasks", "Input should be a valid dictionary or instance of Task"),
            ("hardware", "Input should be a valid dictionary or object to extract fields from"),
        ],
        ids=["model", "union"],
    )
    def test_a_document_that_is_no_mapping_is_the_message_alone(
        self, tmp_path: Path, layer: str, message: str, text: str
    ) -> None:
        # No field to name: the line is Pydantic's message, and the union layer gives no
        # "matches none of the forms" line for a document that is no mapping at all.
        assert _refusal(tmp_path, layer, text).details == (message,)

    def test_a_yaml_error_without_a_mark_is_its_texts_first_line(self) -> None:
        assert ms._load_error_details(yaml.YAMLError("first line\nsecond line"), None) == ("first line",)

    def test_a_yaml_error_without_any_text_is_its_type_name(self) -> None:
        assert ms._load_error_details(yaml.YAMLError(""), None) == ("YAMLError",)

    def test_a_closed_data_types_stray_key_is_its_own_line(self, tmp_path: Path) -> None:
        # `extra="forbid"`: the one unknown key a load refuses; every other kind leaves it to
        # `check-model`'s `model.unknown-key` finding.
        refusal = _refusal(tmp_path, "base_types", "name: B\nkind: base_type\nsize: 8\nencoding: NONE\nbsae: x\n")
        assert refusal.details == ("bsae: Extra inputs are not permitted",)

    @pytest.mark.parametrize(
        ("spelled", "written"),
        [("bad\\nkey", "bad\\nkey"), ("a\\tb\\e", "a\\tb\\x1b"), ("p\\Lq", "p\\u2028q")],
        ids=["newline", "tab-and-escape", "line-separator"],
    )
    def test_a_key_holding_a_control_character_is_written_escaped_on_one_line(
        self, tmp_path: Path, spelled: str, written: str
    ) -> None:
        # Written raw, a newline would split the field's line in two: the key is escaped as `repr` does.
        text = f'name: B\nkind: base_type\nsize: 8\nencoding: NONE\n"{spelled}": x\n'
        refusal = _refusal(tmp_path, "base_types", text)
        assert refusal.details == (f"{written}: Extra inputs are not permitted",)
        assert all("\n" not in line for line in refusal.render_lines(full_path=False))

    def test_an_open_kinds_stray_key_is_no_line_of_a_load_refusal(self, tmp_path: Path) -> None:
        # `model.unknown-key` is a `check-model` finding: the stray key is dropped on load, so a
        # refusal of the same file names its real error alone.
        refusal = _refusal(tmp_path, "hardware", "name: P1\nkind: partition\ncore: 5\ncroe: C\n")
        assert refusal.details == ("core: Input should be a valid string",)

    def test_two_errors_are_two_lines(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "tasks", TWO_ERRORS_TASK)
        assert refusal.render_lines(full_path=False) == [
            "Invalid task YAML in tasks/Bad.yaml: period: Input should be a finite number",
            "Invalid task YAML in tasks/Bad.yaml: priority: Input should be a valid integer, unable to parse "
            "string as an integer",
        ]

    def test_a_yaml_syntax_error_is_placed_by_line_and_column(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "tasks", "name: T\n  period: [1\n")
        assert refusal.details == ("line 2, column 9: mapping values are not allowed in this context",)

    def test_the_full_form_names_the_path_on_every_line(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "tasks", TWO_ERRORS_TASK)
        path = tmp_path / "model" / "tasks" / "Bad.yaml"
        lines = refusal.render_lines(full_path=True)
        assert len(lines) == 2
        assert all(line.startswith(f"Invalid task YAML in {path}: ") for line in lines)

    def test_str_is_one_line_the_details_joined(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "tasks", TWO_ERRORS_TASK)
        assert "\n" not in str(refusal) and "\n" not in refusal.render(full_path=True)
        assert str(refusal) == (
            "Invalid task YAML in tasks/Bad.yaml: period: Input should be a finite number; "
            "priority: Input should be a valid integer, unable to parse string as an integer"
        )


class TestTheUnionsOneLineEach:
    def test_a_model_validator_inside_a_discriminated_union_is_its_message_alone(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "application_types", "name: E\nkind: enum\n")
        assert refusal.details == (
            "Value error, an enum states exactly one of 'literals' (inline) and 'compu_method_ref' "
            "(a texttable compu method)",
        )

    def test_an_event_matching_no_event_form_is_one_line(self, tmp_path: Path) -> None:
        refusal = _refusal(
            tmp_path, "swc", "name: S\nkind: application\nrunnables:\n- name: R\n  events:\n  - type: bogus\n"
        )
        assert refusal.details == ("runnables[0].events[0]: matches none of the forms this field allows",)

    @pytest.mark.parametrize("value", ["2020-01-01", "[2020-01-01]", "{1: 2}"])
    def test_an_init_matching_no_init_form_is_one_line(self, tmp_path: Path, value: str) -> None:
        # `InitValue` is a smart union of bool, int, float, str, list and dict: a YAML timestamp is
        # none of them, alone or inside a list, and neither is a mapping with an int key.
        refusal = _refusal(
            tmp_path,
            "swc",
            "name: S\nkind: application\nports:\n- name: p\n  direction: provide\n  interface: I\n"
            f"  com_specs:\n  - data_element: D\n    init: {value}\n",
        )
        assert refusal.details == ("ports[0].com_specs[0].init: matches none of the forms this field allows",)

    def test_a_union_error_from_one_member_keeps_its_own_lines(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "hardware", "name: P1\nkind: partition\ncore: 5\ntrust: wide_open\n")
        assert refusal.details == (
            "core: Input should be a valid string",
            "trust: Input should be 'non_trusted', 'trusted_with_protection' or 'trusted'",
        )

    def test_identical_lines_appear_once(self) -> None:
        # The same field reached once directly and once through a dropped tag is one line.
        error = ValidationError.from_exception_data(
            "Hardware",
            [
                {"type": "missing", "loc": ("core",), "input": {}},
                {"type": "missing", "loc": ("partition", "core"), "input": {}},
            ],
        )
        assert ms._load_error_details(error, {"kind": "partition"}) == ("core: Field required",)

    def test_a_validator_message_carrying_a_newline_is_one_line(self, tmp_path: Path) -> None:
        refusal = _refusal(
            tmp_path, "swc", 'name: S\nkind: application\ndata_type_mappings:\n- "Set\\n  A"\n- "Set\\n  A"\n'
        )
        assert refusal.details == (
            "data_type_mappings: Value error, mapping set named twice in data_type_mappings: Set A",
        )


class TestNoLineCarriesPydanticsText:
    @pytest.mark.parametrize(
        ("layer", "text"),
        [
            ("tasks", TWO_ERRORS_TASK),
            ("swc", "name: S\nkind: application\nrunnables:\n- name: R\n  events:\n  - type: bogus\n"),
            ("base_types", "name: B\nkind: base_type\nbsae: x\n"),
            ("application_types", "name: E\nkind: enum\n"),
            ("tasks", "name: T\n  period: [1\n"),
        ],
    )
    def test_no_input_value_no_url_no_pydantic_header(self, tmp_path: Path, layer: str, text: str) -> None:
        refusal = _refusal(tmp_path, layer, text)
        for line in [*refusal.render_lines(full_path=False), *refusal.render_lines(full_path=True), str(refusal)]:
            # The text after the file's name: pytest's tmp_path carries this test's own name.
            said = line.split(".yaml: ", 1)[1]
            assert "input_value" not in said and "errors.pydantic.dev" not in said, line
            assert "validation error for" not in said and "\n" not in line, line

    def test_the_storage_name_form_names_no_directory_above_the_model_root(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path, "tasks", TWO_ERRORS_TASK)
        for line in [*refusal.render_lines(full_path=False), str(refusal)]:
            assert str(tmp_path) not in line and tmp_path.as_posix() not in line and "model/" not in line, line


@pytest.mark.parametrize("layer", sorted(READER_LABELS))
def test_the_derived_parse_label_is_the_one_the_readers_use(layer: str) -> None:
    facts = production_registry().layer(layer)
    assert ms.parse_label(facts) == READER_LABELS[layer]


def test_a_layer_with_no_snapshot_attribute_still_gets_a_label(merged: ModelRegistry) -> None:
    assert ms.parse_label(merged.layer("synthetic")) == "synthetic"


# ---------------------------------------------------------------------------
# The seam: a sixteenth layer, resolved at call time
# ---------------------------------------------------------------------------


def test_a_synthetic_layer_is_read_from_its_declared_directory(model: Path, merged: ModelRegistry) -> None:
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "Powertrain"})
    _write(model / "synthetic" / "S2.yaml", {"name": "S2", "title": "Two", "composition": "Powertrain"})

    with use_registry(merged):
        state = load_model_state(model)

    assert list(state)[-1] == "synthetic"
    assert [element.name for element in state["synthetic"]] == ["S1", "S2"]
    assert all(isinstance(element, SyntheticElement) for element in state["synthetic"])
    assert state["swc"] == reader.read_swc_model(str(model)), "the shipped layers are unaffected"


def test_the_registry_is_resolved_at_call_time_not_at_definition_time(model: Path, merged: ModelRegistry) -> None:
    """Calls with the context switched in between see different registries.

    A ``registry=current_registry()`` default expression would pass the first call and fail the
    second.
    """
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "Powertrain"})

    assert "synthetic" not in load_model_state(model)
    with use_registry(merged):
        assert "synthetic" in load_model_state(model)
    assert "synthetic" not in load_model_state(model)


def test_an_explicit_registry_beats_the_context(model: Path, merged: ModelRegistry) -> None:
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "Powertrain"})
    with use_registry(merged):
        assert "synthetic" not in load_model_state(model, registry=production_registry())
    assert "synthetic" in load_model_state(model, registry=merged)


def test_the_synthetic_layers_directory_is_not_special_cased(model: Path, merged: ModelRegistry) -> None:
    """The directory read is the storage record's, not the layer's name."""
    _write(model / "elsewhere" / "S1.yaml", {"name": "S1", "title": "One", "composition": "Powertrain"})
    moved = _with_directory(merged, "synthetic", "elsewhere")
    with use_registry(moved):
        assert [element.name for element in load_model_state(model)["synthetic"]] == ["S1"]


def _with_directory(registry: ModelRegistry, layer: str, directory: str) -> ModelRegistry:
    """*registry* with one layer's storage directory replaced."""
    layers = tuple(
        dataclasses.replace(facts, storage=dataclasses.replace(facts.storage, directory=directory))
        if facts.name == layer
        else facts
        for facts in registry.layers
    )
    return dataclasses.replace(registry, layers=layers)


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def test_a_revision_equals_todays_load_snapshot_layer_by_layer(git_repo) -> None:
    _, model_root = git_repo
    _write(
        model_root / "tasks" / "Task_5ms.yaml",
        {"name": "Task_5ms", "period": 0.005, "priority": 3, "partition": "PARTITION0"},
    )
    subprocess.run(["git", "add", "-A"], cwd=model_root.parent, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.x", "commit", "-m", "second"],
        cwd=model_root.parent,
        check=True,
        capture_output=True,
    )
    (model_root / "tasks" / "Task_5ms.yaml").unlink()

    for rev in ("HEAD", "HEAD~1"):
        state = load_model_state(model_root, rev=rev)
        expected = snapshot.load_snapshot(str(model_root), rev=rev)
        assert state["swc"] == expected.swcs, rev
        assert state["tasks"] == expected.tasks, rev
        assert state.placement["swc"] == expected.swc_placement, rev

    assert [element.name for element in load_model_state(model_root, rev="HEAD")["tasks"]] == ["Task_5ms"]
    assert load_model_state(model_root, rev="HEAD~1")["tasks"] == []
    assert load_model_state(model_root)["tasks"] == [], "the working tree, where the file was removed"


def test_a_revision_leaves_no_temporary_tree_behind(git_repo, monkeypatch) -> None:
    """The loader cleans up the materializer's temporary directory.

    The primitive hands its ``TemporaryDirectory`` to the caller unentered, so the caller owns
    the cleanup.
    """
    _, model_root = git_repo
    created: list[Any] = []
    real = ms.tempfile.TemporaryDirectory

    def spy(*args: Any, **kwargs: Any) -> Any:
        made = real(*args, **kwargs)
        created.append(made)
        return made

    monkeypatch.setattr(ms.tempfile, "TemporaryDirectory", spy)
    load_model_state(model_root, rev="HEAD")
    assert created, "the revision path did not materialize anything"
    assert not any(Path(made.name).exists() for made in created)


def test_an_option_like_revision_is_still_refused(git_repo) -> None:
    _, model_root = git_repo
    with pytest.raises(ValueError, match="may not start with '-'"):
        load_model_state(model_root, rev="--output=/tmp/x")


# ---------------------------------------------------------------------------
# Union synthesis
# ---------------------------------------------------------------------------

#: One valid document per top-level kind, plus the two malformed shapes a
#: discriminated union has to refuse: a ``kind`` no member declares, and no
#: ``kind`` at all.
UNION_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    "interfaces": [
        {"name": "ISR", "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": "float32"}]},
        {"name": "ICS", "kind": "client_server", "operations": [{"name": "Read"}]},
        {"name": "IExt", "kind": "external_interface", "external_ref": "/P/IExt", "external_dest": "PORT-INTERFACE"},
        {
            "name": "IBad",
            "kind": "publish_subscribe",
            "data_elements": [{"name": "v", "implementation_type": "float32"}],
        },
        {"name": "INone", "data_elements": [{"name": "v", "implementation_type": "float32"}]},
    ],
    # No base type or application-side kind: those are other layers' (the two `..._refuses_...`
    # tests below).
    "implementation_types": [
        {"name": "Speed", "kind": "value_type", "base": "u8"},
        {"name": "SpeedRef", "kind": "reference_type", "base": "Speed"},
        {"name": "Ext", "kind": "external_type", "external_ref": "/P/Ext", "external_dest": "IMPLEMENTATION-DATA-TYPE"},
        {"name": "Rec_impl", "kind": "implementation_record", "elements": [{"name": "x", "type": "u8"}]},
        {"name": "Arr_impl", "kind": "implementation_array", "element_type": "u8", "length": 4, "element_name": "E"},
        {"name": "Ptr", "kind": "pointer", "target": "Rec_impl"},
        {"name": "Bad", "kind": "union", "size": 8, "encoding": "NONE"},
        {"name": "None_", "size": 8, "encoding": "NONE"},
    ],
    "application_types": [
        {"name": "Mode", "kind": "enum", "literals": [{"name": "Off", "value": 0}]},
        {"name": "Rec", "kind": "record", "fields": [{"name": "a", "type": "Kph"}]},
        {"name": "Arr", "kind": "array", "element_type": "Kph", "length": 4},
        {"name": "Kph", "kind": "application_type", "unit": "km/h"},
        {"name": "Bad", "kind": "union", "unit": "km/h"},
        {"name": "None_", "unit": "km/h"},
    ],
    "hardware": [
        {"name": "CPU", "kind": "processor"},
        {"name": "CORE0", "kind": "core", "threshold": 0.7},
        {
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 1024,
            "accessible": ["CORE0"],
            "threshold": 0.8,
        },
        {"name": "PARTITION0", "kind": "partition", "core": "CORE0"},
        {"name": "Bad", "kind": "gpu_core", "threshold": 0.7},
        {"name": "None_", "threshold": 0.7},
    ],
}

#: The ``schema.py`` aliases the synthesized annotations must match. No ``binding.json`` row
#: names them, which is why the kernel synthesizes its own and why this comparison exists.
#: ``schema.DataType`` unions every data type kind, so it serves both multi-kind data description
#: layers, each compared over documents of its own kinds.
SCHEMA_UNIONS = {
    "interfaces": schema.Interface,
    "implementation_types": schema.DataType,
    "application_types": schema.DataType,
    "hardware": schema.Hardware,
}


@pytest.mark.parametrize("layer", sorted(SCHEMA_UNIONS))
def test_the_synthesized_union_accepts_and_rejects_exactly_the_schema_alias(layer: str) -> None:
    synthesized: TypeAdapter[Any] = TypeAdapter(production_registry().layer(layer).element_annotation)
    declared: TypeAdapter[Any] = TypeAdapter(SCHEMA_UNIONS[layer])

    verdicts = []
    for document in UNION_DOCUMENTS[layer]:
        try:
            expected = declared.validate_python(document)
        except ValidationError:
            with pytest.raises(ValidationError):
                synthesized.validate_python(document)
            verdicts.append(False)
            continue
        assert synthesized.validate_python(document) == expected
        assert type(synthesized.validate_python(document)) is type(expected)
        verdicts.append(True)

    assert verdicts.count(False) == 2, f"{layer}: the wrong-kind and missing-kind documents must both be refused"
    assert verdicts.count(True) == len(production_registry().layer(layer).top_level_kinds)


@pytest.mark.parametrize("layer", sorted(set(READERS) - set(SCHEMA_UNIONS)))
def test_a_single_kind_layers_annotation_is_its_bound_class(layer: str) -> None:
    facts = production_registry().layer(layer)
    assert len(facts.top_level_kinds) == 1
    assert facts.element_annotation is facts.top_level_kinds[0].model
    assert isinstance(facts.element_annotation, type)


def test_the_implementation_types_layer_refuses_a_base_type() -> None:
    """A base type is no member of the `implementation_types` union."""
    document = {"name": "u8", "kind": "base_type", "size": 8, "encoding": "NONE"}
    synthesized: TypeAdapter[Any] = TypeAdapter(production_registry().layer("implementation_types").element_annotation)
    with pytest.raises(ValidationError):
        synthesized.validate_python(document)
    base_types: TypeAdapter[Any] = TypeAdapter(production_registry().layer("base_types").element_annotation)
    assert type(base_types.validate_python(document)) is schema.BaseDataType


def test_the_implementation_types_layer_refuses_an_application_type() -> None:
    """An application-side kind is no member of the `implementation_types` union."""
    document = {"name": "Mode", "kind": "enum", "literals": [{"name": "Off", "value": 0}]}
    synthesized: TypeAdapter[Any] = TypeAdapter(production_registry().layer("implementation_types").element_annotation)
    with pytest.raises(ValidationError):
        synthesized.validate_python(document)
    application: TypeAdapter[Any] = TypeAdapter(production_registry().layer("application_types").element_annotation)
    assert type(application.validate_python(document)) is schema.EnumDataType


def test_the_loader_parses_a_multi_kind_layer_into_its_several_classes(model: Path) -> None:
    state = load_model_state(model)
    kinds = {type(element).__name__ for element in state["implementation_types"]}
    assert kinds == {"ReferenceDataType", "ValueDataType"}
    assert {type(element).__name__ for element in state["base_types"]} == {"BaseDataType"}
    assert {type(element).__name__ for element in state["application_types"]} == {"EnumDataType"}


# ---------------------------------------------------------------------------
# A field declared after the shipped layers were already read once
# ---------------------------------------------------------------------------

#: The field the extension fixture declares; it exists in no ``schema.py`` and no contract file.
EXTENSION_FIELD = "telemetry_channel"


@pytest.fixture
def extended_swcomponent() -> Any:
    """Read the shipped model once, then declare one new ``SWComponent`` field.

    **The guard against an adapter cache in ``model_state.py``**: a cached ``TypeAdapter`` built
    before the field existed would drop it silently. The leading read is the priming such a cache
    needs. The fast-gate copy of ``packages/mcp/tests/test_greenfield_acceptance.py``'s
    ``slow`` ``extended_schema`` precondition.
    """
    load_model_state(BASIC_MODEL)

    original = dict(schema.SWComponent.model_fields)
    schema.SWComponent.model_fields[EXTENSION_FIELD] = FieldInfo.from_annotated_attribute(str | None, None)
    schema.SWComponent.model_rebuild(force=True)
    try:
        yield
    finally:
        schema.SWComponent.model_fields.clear()
        schema.SWComponent.model_fields.update(original)
        schema.SWComponent.model_rebuild(force=True)
        # Asserted in the teardown, not in a sibling test, which would prove the restoration only
        # when file order happened to put it second.
        assert EXTENSION_FIELD not in schema.SWComponent.model_fields
        assert set(schema.SWComponent.model_fields) == set(original)
        assert not hasattr(load_model_state(BASIC_MODEL)["swc"][0], EXTENSION_FIELD), (
            "the loader still parses the extension after teardown — a stale cached adapter"
        )


def test_a_field_declared_after_the_adapter_was_built_is_parsed_and_published(
    model: Path, extended_swcomponent: None
) -> None:
    """A field declared after a first load is parsed and published.

    The parse is where a stale adapter would lose the field, the publish where the loss surfaces.
    """
    _write(
        model / "swc" / "control" / "TelemetrySWC.yaml",
        {"name": "TelemetrySWC", "type": "application", EXTENSION_FIELD: "can0"},
    )

    state = load_model_state(model)
    (element,) = [swc for swc in state["swc"] if swc.name == "TelemetrySWC"]
    parsed = getattr(element, EXTENSION_FIELD, None)
    assert parsed == "can0", "the loader parsed the element without the newly declared field"

    published = publish_element(
        element,
        editing_registry.build_registry(),
        placement=state.placement_of("swc")["TelemetrySWC"],
    )
    assert published[EXTENSION_FIELD] == "can0"


def test_the_extension_is_visible_while_its_fixture_is_active(model: Path, extended_swcomponent: None) -> None:
    """While the fixture holds, a parsed `swc` element carries the declared field.

    So the teardown's restoration check (in the fixture) undoes a real change, not a no-op.
    """
    (element,) = [swc for swc in load_model_state(model)["swc"] if swc.name == "SpeedSensor"]
    assert hasattr(element, EXTENSION_FIELD)
    assert getattr(element, EXTENSION_FIELD) is None


# ---------------------------------------------------------------------------
# The dependency order
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every ``swcraft_tools.*`` module the file names in an import, anywhere, read off the AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("swcraft_tools")}


def test_model_state_imports_none_of_reader_snapshot_or_differ() -> None:
    imported = _imported_modules(TOOLS_SRC / "model_state.py")
    for forbidden in ("swcraft_tools.reader", "swcraft_tools.snapshot", "swcraft_tools.differ"):
        assert forbidden not in imported, f"model_state.py imports {forbidden}; the arrow points the other way"
    assert not any(name.startswith("swcraft_tools.editing") for name in imported)
    assert "swcraft_tools.model_registry" in imported


def test_reader_and_snapshot_import_model_state_and_re_export_the_moved_primitives() -> None:
    assert "swcraft_tools.model_state" in _imported_modules(TOOLS_SRC / "reader.py")
    assert "swcraft_tools.model_state" in _imported_modules(TOOLS_SRC / "snapshot.py")

    assert reader._load_element is parse_element
    assert reader.ElementParseError is ms.ElementParseError
    assert snapshot._materialize_revision is materialize_revision


def test_the_moved_parser_behaves_as_it_did_from_either_name(tmp_path: Path) -> None:
    root = tmp_path / "model"
    _write(root / "tasks" / "T.yaml", {"name": "T", "period": 0.01, "priority": 1, "partition": "PARTITION0"})
    path = root / "tasks" / "T.yaml"
    assert parse_element(path, "task", schema.Task.model_validate, root) == reader.read_tasks(str(root))[0]

    bad = root / "tasks" / "Bad.yaml"
    bad.write_text("name: [\n", encoding="utf-8")
    with pytest.raises(ms.ElementParseError) as raised:
        parse_element(bad, "task", schema.Task.model_validate, root)
    assert raised.value.target == "tasks/Bad.yaml"
    assert str(bad) in raised.value.render(full_path=True)


def test_the_state_is_constructible_and_comparable_without_reading_anything() -> None:
    """``ModelState`` is a value, not a handle on a directory: equal content compares equal."""
    one = ModelState(layers={"swc": [], "tasks": []}, placement={"swc": {}}, absent=frozenset({"hardware"}))
    other = ModelState(layers={"swc": [], "tasks": []}, placement={"swc": {}}, absent=frozenset({"hardware"}))
    assert one == other
    assert list(one) == ["swc", "tasks"]
    assert one["tasks"] == []
    assert one.absent == frozenset({"hardware"})


def test_the_default_state_is_empty() -> None:
    empty = ModelState(layers={})
    assert list(empty) == []
    assert empty.placement == {}
    assert empty.absent == frozenset()


def test_the_current_registry_is_the_production_one_outside_a_swap() -> None:
    assert current_registry() is production_registry()


# ---------------------------------------------------------------------------
# Subprocess discipline -- every child must be told what its stdin is
# ---------------------------------------------------------------------------


class TestSubprocessStdinDiscipline:
    """No product subprocess may inherit its parent's stdin.

    Under the MCP server stdin is the JSON-RPC pipe, and a child inheriting it hangs on Windows.
    A declaration rule: ``DEVNULL`` or an explicit pipe are fine, silence inherits. The one spawn
    that must inherit, ``cli.handover._hand_over`` (its child is the stdio server itself),
    declares ``stdin=None``, and only it may.

    What this does **not** reach: only ``swcraft_tools`` is scanned, only a ``subprocess.<name>``
    attribute call is recognised (not ``from subprocess import Popen`` or ``os``/
    ``multiprocessing`` spawns), and it reads the declaration, never the run-time handle.
    """

    #: Product modules only: `scripts/` is not shipped and runs from a shell.
    PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools"

    def _spawn_calls(self) -> list[tuple[str, int, ast.Call]]:
        """Every ``subprocess`` spawn call in the shipped package, by package-relative path.

        The templates are excluded: they run in a customer project from a shell, never from the
        MCP server.
        """
        found: list[tuple[str, int, ast.Call]] = []
        for path in sorted(self.PACKAGE_ROOT.rglob("*.py"), key=lambda path: path.as_posix()):
            if "templates" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute):
                    continue
                # `call`, `check_call` and `check_output` wrap `Popen` and inherit the same way.
                if function.attr not in {"run", "Popen", "call", "check_call", "check_output"}:
                    continue
                module = function.value
                if isinstance(module, ast.Name) and module.id == "subprocess":
                    found.append((path.relative_to(self.PACKAGE_ROOT).as_posix(), node.lineno, node))
        return found

    def test_the_scan_finds_the_calls_it_is_meant_to_police(self) -> None:
        """The scan is not empty, so the rule below cannot pass by finding nothing."""
        calls = self._spawn_calls()
        assert len(calls) >= 3, f"the subprocess scan found only {len(calls)} call(s); it has gone blind"
        assert {name for name, _, _ in calls} >= {"builder.py", "model_state.py"}

    def test_every_product_subprocess_declares_its_stdin(self) -> None:
        """Every spawn names ``stdin=`` or ``input=`` (which makes stdin a pipe and refuses ``stdin=``)."""
        offenders = [
            f"{name}:{lineno}"
            for name, lineno, call in self._spawn_calls()
            if not any(keyword.arg in {"stdin", "input"} for keyword in call.keywords)
        ]
        assert offenders == [], (
            "these subprocess calls inherit their parent's stdin: "
            f"{offenders}. Under the MCP server that parent handle is the JSON-RPC "
            "pipe, and a child holding it hangs the tool on Windows. Pass "
            "stdin=subprocess.DEVNULL (or an explicit pipe) — the point is that the "
            "call says which."
        )

    def test_exactly_one_spawn_inherits_and_it_is_the_handover(self) -> None:
        """``stdin=None`` is inheritance declared, and only the handover may say it."""
        inheriting = [
            (name, lineno)
            for name, lineno, call in self._spawn_calls()
            for keyword in call.keywords
            if keyword.arg == "stdin" and isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        ]
        assert [name for name, _ in inheriting] == ["cli/handover.py"], (
            f"these spawns declare `stdin=None`, which inherits: {inheriting}. Exactly one may — "
            "the handover, whose child is this process as far as its caller is concerned. Anything "
            "else the MCP server runs must pass DEVNULL or an explicit pipe."
        )
