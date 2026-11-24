"""The specimen preset, the leaf table, the feasibility frame and the CLI.

The leaf table covers the vendored schema, a passport becomes a preset and back deterministically,
the CLI's three commands share one code path, and the `version` and `later-shape` refusals are
reached before generation; every stated leaf but the two versions is `met`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens.cli import run
from swcraft_specimens.errors import PresetError
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.passport import (
    PassportRefused,
    PassportSchemaError,
    schema_document,
    validate_passport,
)
from swcraft_specimens.preset import Preset, load_any_preset
from swcraft_specimens.specimen import leaves as leaves_module
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import LEAVES, schema_leaf_paths, stated_leaves
from swcraft_specimens.specimen.preset import (
    DEFAULT_NAME,
    SpecimenPreset,
    parse_specimen_text,
    preset_text,
)
from swcraft_specimens.specimen.vocabulary import (
    CONTRACT_VERSION_LEAF,
    LATER_SHAPE,
    MET,
    PASSPORT_VERSION_LEAF,
    SUM,
    VERSION,
)
from tests.oracle.specimen_helpers import (
    LAYERS,
    MET_IDENTIFIER_LAYERS,
    neutral_passport,
    write_json,
)

ROOT = Path(__file__).parents[1]
WATERS_SMALL = preset_path("small")

MET_LAYERS = (
    "swc",
    "interfaces",
    "connectors",
    "implementation_types",
    "base_types",
    "hardware",
    "compositions",
    # The five type layers' counts.
    "application_types",
    "mapping_sets",
    "units",
    "compu_methods",
    "data_constraints",
    "tasks",
    "sequences",
    "requirements",
)


def refusal_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("refused: ")]


# --- the leaf table -----------------------------------------------------------------------------


class TestLeafTable:
    def test_the_schema_has_76_leaves(self) -> None:
        assert len(schema_leaf_paths(schema_document())) == 76

    def test_the_table_covers_exactly_the_schema_leaves_in_schema_order(self) -> None:
        assert list(LEAVES) == schema_leaf_paths(schema_document())

    def test_each_leaf_has_its_class(self) -> None:
        expected: dict[str, str] = {"passport_version": VERSION, "contract_version": VERSION}
        assert set(LAYERS) == set(MET_LAYERS)
        for layer in LAYERS:
            expected[f"layers.{layer}"] = MET
        for leaf in (
            "components.ports.total",
            "components.ports.top",
            "components.hubs",
            "components.unconnected.total",
            "components.unconnected.max_on_one",
            "components.by_declared_level.QM",
            "components.by_declared_level.A",
            "components.by_declared_level.B",
            "components.by_declared_level.C",
            "components.by_declared_level.D",
            "components.runnables",
            "interfaces.sender_receiver",
            "interfaces.client_server",
            "interfaces.fan_out.max",
            "interfaces.fan_out.top",
            "connectors.assembly",
            "connectors.delegation",
            "connectors.links",
            "hardware.cores",
            "hardware.partitions",
            "hardware.memories",
            "hardware.rated_partitions",
            "identifiers.max_length",
        ):
            expected[leaf] = MET
        assert expected["layers.compositions"] == MET
        expected["tasks.idle"] = MET
        for leaf in ("on_all_site_kinds", "on_one_port", "unallocated", "stale_links", "links"):
            expected[f"requirements.{leaf}"] = MET
        assert set(LAYERS) == set(MET_IDENTIFIER_LAYERS)
        for layer in LAYERS:
            expected[f"identifiers.{layer}.max_length"] = MET
            expected[f"identifiers.{layer}.mean_length"] = MET
        assert {path: leaf.leaf_class for path, leaf in LEAVES.items()} == expected
        # No leaf is `later-shape`.
        assert LATER_SHAPE not in {leaf.leaf_class for leaf in LEAVES.values()}

    @pytest.mark.parametrize(
        ("path", "table"),
        [
            ("layers.swc", "MET_LAYERS"),
            ("identifiers.swc.max_length", "MET_IDENTIFIER_LAYERS"),
        ],
    )
    def test_an_unclassified_schema_layer_is_refused(
        self, path: str, table: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A re-vendored schema's new layer is never classified silently (`met` or otherwise).
        monkeypatch.setattr(leaves_module, table, frozenset())
        with pytest.raises(PassportSchemaError, match="the vendored schema's layer swc is not"):
            leaves_module._classify(path)

    def test_layers_interfaces_is_met_above_sr_plus_cs(self) -> None:
        # Met at any value >= SR + CS, the rest stubs.
        assert LEAVES["layers.interfaces"].leaf_class == MET
        passport = neutral_passport()
        passport["layers"]["interfaces"] = 4
        assert check_feasibility(passport) == []

    def test_lists_are_one_leaf_each(self) -> None:
        assert "components.hubs" in LEAVES
        assert "components.ports.top" in LEAVES
        assert "interfaces.fan_out.top" in LEAVES
        assert not any(path.split(".")[-1].isdigit() for path in LEAVES)

    def test_stated_leaves_follow_the_passport_in_schema_order(self) -> None:
        stated = stated_leaves(neutral_passport())
        assert list(stated) == [path for path in LEAVES if path in stated]
        assert stated["components.hubs"] == []
        assert stated["components.ports.top"] == [1, 1]
        assert stated["identifiers.swc.mean_length"] == 7.0


# --- passport <-> preset ------------------------------------------------------------------------


def shuffled(document: dict[str, Any]) -> dict[str, Any]:
    """The same passport with every object's keys in reverse order."""

    def reverse(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: reverse(value[key]) for key in reversed(list(value))}
        return value

    result: dict[str, Any] = reverse(document)
    return result


class TestPresetFromPassport:
    def test_keys_are_written_in_schema_order_whatever_the_input_order(self) -> None:
        passport = neutral_passport()
        assert preset_text(shuffled(passport)) == preset_text(passport)
        loaded = yaml.safe_load(preset_text(shuffled(passport)))
        assert list(loaded) == ["kind", "passport"]
        assert list(loaded["passport"]) == list(passport)
        assert list(loaded["passport"]["identifiers"]) == list(passport["identifiers"])

    def test_the_text_is_byte_stable_through_the_cli(self, tmp_path: Path) -> None:
        source = write_json(tmp_path / "passport.json", shuffled(neutral_passport()))
        first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
        assert run(["preset-from-passport", str(source), "--out", str(first)]) == 0
        assert run(["preset-from-passport", str(source), "--out", str(second)]) == 0
        assert first.read_bytes() == second.read_bytes()
        assert first.read_text(encoding="utf-8") == preset_text(neutral_passport())
        assert b"\r" not in first.read_bytes()

    def test_the_preset_reads_back_to_the_same_passport(self) -> None:
        passport = neutral_passport()
        preset = parse_specimen_text(preset_text(passport), "preset")
        assert preset == SpecimenPreset(DEFAULT_NAME, passport)
        assert type(preset.passport["identifiers"]["units"]["mean_length"]) is int
        assert type(preset.passport["identifiers"]["swc"]["mean_length"]) is float

    def test_a_name_is_written_and_read(self, tmp_path: Path) -> None:
        text = preset_text(neutral_passport(), name="DemoSpecimen")
        assert list(yaml.safe_load(text)) == ["kind", "name", "passport"]
        assert parse_specimen_text(text, "preset").name == "DemoSpecimen"
        source = write_json(tmp_path / "passport.json", neutral_passport())
        out = tmp_path / "named.yaml"
        argv = ["preset-from-passport", str(source), "--out", str(out), "--name", "DemoSpecimen"]
        assert run(argv) == 0
        assert out.read_text(encoding="utf-8") == text

    def test_an_illegal_name_is_refused(self, tmp_path: Path, capsys: Any) -> None:
        source = write_json(tmp_path / "passport.json", neutral_passport())
        argv = ["preset-from-passport", str(source), "--out", str(tmp_path / "p.yaml")]
        assert run([*argv, "--name", "1bad name"]) == 2
        assert "name" in capsys.readouterr().err
        assert not (tmp_path / "p.yaml").exists()

    def test_a_passport_that_is_not_schema_valid_is_refused(self, tmp_path: Path, capsys: Any) -> None:
        document = neutral_passport()
        document["layers"]["swc"] = "two"
        source = write_json(tmp_path / "passport.json", document)
        out = tmp_path / "p.yaml"
        assert run(["preset-from-passport", str(source), "--out", str(out)]) == 2
        lines = refusal_lines(capsys.readouterr().err)
        assert lines == ["refused: layers.swc (schema): is a string, not an integer"]
        assert not out.exists()

    def test_a_repeated_key_is_refused(self, tmp_path: Path, capsys: Any) -> None:
        text = json.dumps(neutral_passport())
        text = text.replace('"swc": 2,', '"swc": 2, "swc": 3,', 1)
        source = tmp_path / "passport.json"
        source.write_text(text, encoding="utf-8", newline="\n")
        argv = ["preset-from-passport", str(source), "--out", str(tmp_path / "p.yaml")]
        assert run(argv) == 2
        assert refusal_lines(capsys.readouterr().err) == ["refused: layers.swc (reading): the key is repeated"]

    def test_an_existing_output_is_not_overwritten(self, tmp_path: Path, capsys: Any) -> None:
        source = write_json(tmp_path / "passport.json", neutral_passport())
        out = tmp_path / "p.yaml"
        out.write_text("keep\n", encoding="utf-8", newline="\n")
        assert run(["preset-from-passport", str(source), "--out", str(out)]) == 2
        assert "exists" in capsys.readouterr().err
        assert out.read_text(encoding="utf-8") == "keep\n"

    def test_a_missing_passport_file_exits_2(self, tmp_path: Path, capsys: Any) -> None:
        argv = ["preset-from-passport", str(tmp_path / "none.json"), "--out", str(tmp_path / "p")]
        assert run(argv) == 2
        assert "passport not found" in capsys.readouterr().err


# --- loading a preset: the dispatch on kind -----------------------------------------------------


def write_preset(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class TestKindDispatch:
    def test_a_waters_preset_loads_as_today(self) -> None:
        loaded = load_any_preset(WATERS_SMALL)
        assert isinstance(loaded, Preset)
        assert loaded.name

    def test_a_specimen_preset_loads(self, tmp_path: Path) -> None:
        path = write_preset(tmp_path / "s.yaml", preset_text(neutral_passport()))
        assert load_any_preset(path) == SpecimenPreset(DEFAULT_NAME, neutral_passport())

    @pytest.mark.parametrize("kind", ["waters", "Specimen", "1", "[specimen]"])
    def test_an_unknown_kind_is_refused(self, tmp_path: Path, kind: str) -> None:
        text = preset_text(neutral_passport()).replace("kind: specimen", f"kind: {kind}", 1)
        path = write_preset(tmp_path / "s.yaml", text)
        with pytest.raises(PresetError, match="kind"):
            load_any_preset(path)

    def test_an_unknown_kind_exits_2(self, tmp_path: Path, capsys: Any) -> None:
        text = preset_text(neutral_passport()).replace("kind: specimen", "kind: other", 1)
        path = write_preset(tmp_path / "s.yaml", text)
        assert run(["generate", str(path), "--out", str(tmp_path / "out")]) == 2
        assert "unknown preset kind" in capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_an_unknown_root_key_of_a_specimen_preset_is_refused(self, tmp_path: Path) -> None:
        path = write_preset(tmp_path / "s.yaml", preset_text(neutral_passport()) + "seed: 1\n")
        with pytest.raises(PresetError, match="seed"):
            load_any_preset(path)

    def test_a_specimen_preset_without_a_passport_is_refused(self, tmp_path: Path) -> None:
        path = write_preset(tmp_path / "s.yaml", "kind: specimen\n")
        with pytest.raises(PresetError, match="passport"):
            load_any_preset(path)

    def test_a_yaml_nan_in_a_specimen_preset_is_refused(self, tmp_path: Path) -> None:
        text = preset_text(neutral_passport()).replace("mean_length: 7.0", "mean_length: .nan", 1)
        path = write_preset(tmp_path / "s.yaml", text)
        with pytest.raises(PassportRefused) as caught:
            load_any_preset(path)
        assert caught.value.refusals[0].leaf == "identifiers.swc.mean_length"

    def test_a_waters_preset_with_kind_absent_keeps_its_errors(self, tmp_path: Path) -> None:
        path = write_preset(tmp_path / "w.yaml", "- one\n- two\n")
        with pytest.raises(PresetError, match="root must be a mapping"):
            load_any_preset(path)


# --- refusals before generation: version and later-shape ----------------------------------------


def generate_refusals(tmp_path: Path, passport: dict[str, Any], capsys: Any) -> list[str]:
    """The refusal lines `generate` prints."""
    path = write_preset(tmp_path / "specimen.yaml", preset_text(passport))
    out = tmp_path / "out"
    assert run(["generate", str(path), "--out", str(out)]) == 2
    assert not out.exists()
    return refusal_lines(capsys.readouterr().err)


class TestVersionRefusals:
    def test_contract_major_11_is_refused_naming_both(self, tmp_path: Path, capsys: Any) -> None:
        passport = neutral_passport()
        passport["contract_version"] = "11.4.0"
        lines = generate_refusals(tmp_path, passport, capsys)
        assert lines[0] == (
            "refused: contract_version (version): states model contract 11.4.0 (major 11); "
            "the specimen generator writes model contract 12.0.0 (major 12)"
        )

    def test_a_contract_major_past_the_integer_conversion_limit_is_refused(self) -> None:
        # int() of a major of 5,000 digits once raised ValueError.
        passport = neutral_passport()
        passport["contract_version"] = "1" * 5000 + ".0.0"
        refusals = check_feasibility(validate_passport(passport))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("contract_version", VERSION)]

    def test_any_contract_12_is_accepted(self) -> None:
        passport = neutral_passport()
        passport["contract_version"] = "12.3.1"
        assert check_feasibility(passport) == []

    def test_any_passport_1_x_y_is_accepted(self) -> None:
        passport = neutral_passport()
        passport["passport_version"] = "1.7.0"
        assert check_feasibility(passport) == []

    @pytest.mark.parametrize("version", ["1.0.0\n", "2.0.0"])
    def test_a_passport_version_outside_1_x_y_is_refused(self, version: str) -> None:
        passport = neutral_passport()
        passport["passport_version"] = version
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("passport_version", VERSION)]
        assert version.strip() not in refusals[0].sentence

    def test_a_contract_version_with_a_trailing_newline_is_refused(self) -> None:
        passport = neutral_passport()
        passport["contract_version"] = "12.0.0\n"
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("contract_version", VERSION)]


#: The requirement vectors, each refused on the neutral passport, whose requirement layer is empty
#: and whose `requirements.links` states 0 (an empty layer states no identifier length).
REQUIREMENT_VECTORS = [
    (
        ("layers", "requirements"),
        1,
        "requirements.links",
        "requirements",
        "states 0; the 0 spread, 0 one-port and 1 general requirements need at least 1",
    ),
    (
        ("requirements", "on_all_site_kinds"),
        1,
        "layers.requirements",
        "sum",
        "states 0; requirements.on_all_site_kinds, requirements.on_one_port and "
        "requirements.unallocated state 1 in all",
    ),
    (
        ("requirements", "on_one_port"),
        1,
        "layers.requirements",
        "sum",
        "states 0; requirements.on_all_site_kinds, requirements.on_one_port and "
        "requirements.unallocated state 1 in all",
    ),
    (
        ("requirements", "unallocated"),
        1,
        "layers.requirements",
        "sum",
        "states 0; requirements.on_all_site_kinds, requirements.on_one_port and "
        "requirements.unallocated state 1 in all",
    ),
    (
        ("requirements", "stale_links"),
        1,
        "requirements.stale_links",
        "requirements",
        "states 1; requirements.links states 0",
    ),
    (
        ("requirements", "links"),
        2,
        "requirements.links",
        "requirements",
        "states 2; the requirements hold at most 0 links, no site twice",
    ),
]


def set_leaf(document: dict[str, Any], keys: tuple[str, ...], value: object) -> None:
    node = document
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value


class TestRequirementRefusals:
    @pytest.mark.parametrize(("keys", "value", "leaf", "cls", "sentence"), REQUIREMENT_VECTORS)
    def test_a_requirement_leaf_the_others_cannot_carry_is_refused(
        self, keys: tuple[str, ...], value: object, leaf: str, cls: str, sentence: str
    ) -> None:
        passport = neutral_passport()
        # The requirement names' lengths unstated, so a non-empty layer is named freely.
        del passport["identifiers"]["requirements"]
        set_leaf(passport, keys, value)
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class, r.sentence) for r in refusals] == [(leaf, cls, sentence)]

    def test_a_requirement_identifier_length_of_an_empty_layer_is_identifier_length(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["requirements"]["max_length"] = 9
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [
            ("identifiers.requirements.max_length", "identifier-length")
        ]

    def test_neutral_values_are_accepted_int_or_float(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["units"]["mean_length"] = 0.0
        assert check_feasibility(passport) == []

    def test_an_unstated_leaf_is_not_refused(self) -> None:
        passport = neutral_passport()
        del passport["layers"]["units"]
        del passport["identifiers"]["units"]
        del passport["connectors"]["delegation"]
        assert check_feasibility(passport) == []

    def test_the_cli_names_the_dotted_path_and_class(self, tmp_path: Path, capsys: Any) -> None:
        # The one `later-shape` refusal left: a stated `layers.hardware` of 0.
        passport = neutral_passport()
        passport["layers"]["hardware"] = 0
        lines = generate_refusals(tmp_path, passport, capsys)
        assert lines[0] == (
            "refused: layers.hardware (later-shape): states no hardware; this version builds one "
            "processor and at least one core"
        )


class TestSeveralRefusals:
    def test_several_refusals_print_together_in_leaf_order_and_exit_2(self, tmp_path: Path, capsys: Any) -> None:
        passport = neutral_passport()
        passport["contract_version"] = "11.0.0"
        # Four idle tasks of three is `sum` at `tasks.idle`; the other task refusals are not looked for.
        passport["layers"]["tasks"] = 3
        passport["tasks"]["idle"] = 4
        passport["requirements"]["links"] = 4
        lines = generate_refusals(tmp_path, passport, capsys)
        assert [line.split(":")[1].strip() for line in lines] == [
            "contract_version (version)",
            "tasks.idle (sum)",
            "requirements.links (requirements)",
        ]

    def test_nothing_prefixes_a_refusal_line(self, tmp_path: Path, capsys: Any) -> None:
        passport = neutral_passport()
        passport["layers"]["tasks"] = 3
        path = write_preset(tmp_path / "specimen.yaml", preset_text(passport))
        assert run(["generate", str(path), "--out", str(tmp_path / "out")]) == 2
        err = capsys.readouterr().err
        assert err.splitlines() == refusal_lines(err)


# --- the met leaves ----------------------------------------------------------------------------


class TestEveryMetLeafBuilt:
    def test_the_tasks_built_every_met_leaf(self) -> None:
        met = {path for path, leaf in LEAVES.items() if leaf.leaf_class == MET}
        assert met == set(LEAVES) - {PASSPORT_VERSION_LEAF, CONTRACT_VERSION_LEAF}
        # Every leaf but the two version leaves.
        assert len(met) == len(LEAVES) - 2 == 74

    def test_no_stated_met_leaf_of_the_neutral_passport_is_refused(self) -> None:
        refusals = check_feasibility(neutral_passport())
        assert refusals == []

    def test_an_unstated_met_leaf_is_not_refused(self) -> None:
        passport = neutral_passport()
        del passport["components"]["ports"]
        refusals = check_feasibility(passport)
        assert "components.ports.total" not in {r.leaf for r in refusals}

    def test_feasibility_refusals_come_alone(self) -> None:
        # More cases than requirements is `sum` at the layer.
        passport = neutral_passport()
        passport["layers"]["requirements"] = 3
        passport["requirements"]["unallocated"] = 4
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("layers.requirements", SUM)]


# --- the one-command form -----------------------------------------------------------------------


class TestSpecimenCommand:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["identifiers"].__setitem__("max_length", 9),
            lambda p: p.__setitem__("contract_version", "11.0.0"),
            lambda p: p["layers"].__setitem__("tasks", 30),
        ],
    )
    def test_specimen_gives_the_refusals_of_the_composed_commands(
        self, tmp_path: Path, capsys: Any, mutate: Any
    ) -> None:
        passport = neutral_passport()
        mutate(passport)
        source = write_json(tmp_path / "passport.json", passport)
        preset = tmp_path / "preset.yaml"
        assert run(["preset-from-passport", str(source), "--out", str(preset)]) == 0
        capsys.readouterr()
        composed = run(["generate", str(preset), "--out", str(tmp_path / "a")])
        composed_err = capsys.readouterr().err
        direct = run(["specimen", str(source), "--out", str(tmp_path / "b")])
        direct_err = capsys.readouterr().err
        assert composed == direct == 2
        assert composed_err == direct_err
        assert refusal_lines(direct_err)
        assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()

    def test_specimen_refuses_a_schema_invalid_passport_as_preset_from_passport_does(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        document = neutral_passport()
        del document["layers"]["swc"]
        source = write_json(tmp_path / "passport.json", document)
        assert run(["preset-from-passport", str(source), "--out", str(tmp_path / "p")]) == 2
        first = capsys.readouterr().err
        assert run(["specimen", str(source), "--out", str(tmp_path / "out")]) == 2
        assert capsys.readouterr().err == first
        assert refusal_lines(first) == ["refused: layers.swc (schema): is required and absent"]

    def test_specimen_refuses_an_unsafe_output(self, tmp_path: Path, capsys: Any) -> None:
        source = write_json(tmp_path / "passport.json", neutral_passport())
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8", newline="\n")
        assert run(["specimen", str(source), "--out", str(out)]) == 2
        assert "not empty" in capsys.readouterr().err

    def test_the_help_lists_the_three_commands(self, capsys: Any) -> None:
        with pytest.raises(SystemExit):
            run(["--help"])
        out = capsys.readouterr().out
        for command in ("generate", "preset-from-passport", "specimen"):
            assert command in out


def test_the_passport_of_a_preset_is_a_copy() -> None:
    passport = neutral_passport()
    preset = parse_specimen_text(preset_text(passport), "preset")
    before = copy.deepcopy(preset.passport)
    passport["layers"]["swc"] = 99
    assert preset.passport == before


# --- the CLI's and the loader's reading errors ---------------------------------------------------


class TestReadingErrors:
    def test_a_passport_that_is_a_directory_is_refused(self, tmp_path: Path, capsys: Any) -> None:
        folder = tmp_path / "passport.json"
        folder.mkdir()
        assert run(["specimen", str(folder), "--out", str(tmp_path / "out")]) == 2
        assert capsys.readouterr().err.startswith("error: passport is not a regular file: ")
        assert not (tmp_path / "out").exists()

    def test_a_passport_that_is_not_utf_8_is_refused(self, tmp_path: Path, capsys: Any) -> None:
        path = tmp_path / "passport.json"
        path.write_bytes('{"name": "Bremsgerät"}'.encode("latin-1"))
        assert run(["preset-from-passport", str(path), "--out", str(tmp_path / "p.yaml")]) == 2
        assert capsys.readouterr().err.startswith("error: cannot read passport ")
        assert not (tmp_path / "p.yaml").exists()

    def test_a_preset_that_cannot_be_written_is_an_output_error(self, tmp_path: Path, capsys: Any) -> None:
        path = tmp_path / "passport.json"
        path.write_text(json.dumps(neutral_passport()), encoding="utf-8", newline="\n")
        target = tmp_path / "missing" / "p.yaml"
        assert run(["preset-from-passport", str(path), "--out", str(target)]) == 2
        assert capsys.readouterr().err.startswith("error: cannot write preset ")

    def test_invalid_yaml_is_refused(self) -> None:
        with pytest.raises(PresetError, match=r"^invalid YAML in preset p\.yaml: "):
            parse_specimen_text("kind: specimen\npassport: [unclosed\n", "p.yaml")

    def test_a_root_that_is_not_a_mapping_is_refused(self) -> None:
        with pytest.raises(PresetError, match=r"^preset root must be a mapping: p\.yaml$"):
            parse_specimen_text("- kind\n- specimen\n", "p.yaml")

    def test_a_repeated_key_is_refused_by_the_specimen_loader(self) -> None:
        text = preset_text(neutral_passport()).replace("\n    tasks: 0\n", "\n    tasks: 0\n    tasks: 0\n", 1)
        with pytest.raises(PresetError, match=r"^duplicate YAML key: 'tasks'$"):
            parse_specimen_text(text, "p.yaml")

    def test_a_key_that_is_not_a_scalar_is_refused(self) -> None:
        # An unhashable key once raised TypeError.
        with pytest.raises(PresetError, match=r"^a YAML key is not a scalar \(line 2\)$"):
            parse_specimen_text("kind: specimen\n? [1, 2]\n: x\n", "p.yaml")

    def test_an_integer_past_the_conversion_limit_is_refused(self, tmp_path: Path) -> None:
        # YAML's int() of 5,000 digits once raised ValueError, in the specimen reader and in the
        # WATERS reader `load_any_preset` falls back to.
        text = f"kind: specimen\npassport:\n  layers: {'1' * 5000}\n"
        match = r"^a YAML integer has more digits than a reader converts \(line 3\)$"
        with pytest.raises(PresetError, match=match):
            parse_specimen_text(text, "p.yaml")
        path = write_preset(tmp_path / "specimen.yaml", text)
        with pytest.raises(PresetError, match=match):
            load_any_preset(path)
        waters = write_preset(tmp_path / "waters.yaml", f"name: {'1' * 5000}\n")
        with pytest.raises(PresetError, match=r"^a YAML integer has more digits"):
            load_any_preset(waters)

    def test_yaml_nested_deeper_than_the_reader_follows_is_refused(self, tmp_path: Path) -> None:
        # The YAML reader's RecursionError once escaped `load_any_preset` (and the WATERS reader it
        # falls back to) as a traceback.
        nested = "[" * 3000 + "]" * 3000
        match = r"^invalid YAML in preset .*: it nests deeper than the YAML reader follows$"
        with pytest.raises(PresetError, match=match):
            parse_specimen_text(f"kind: specimen\npassport: {nested}\n", "p.yaml")
        for text in (f"kind: specimen\npassport: {nested}\n", f"name: {nested}\n"):
            path = write_preset(tmp_path / "deep.yaml", text)
            with pytest.raises(PresetError, match=match):
                load_any_preset(path)

    def test_deep_nesting_is_refused_through_the_cli(self, tmp_path: Path, capsys: Any) -> None:
        passport = tmp_path / "passport.json"
        passport.write_text("[" * 100000 + "]" * 100000, encoding="utf-8", newline="\n")
        target = tmp_path / "p.yaml"
        assert run(["preset-from-passport", str(passport), "--out", str(target)]) == 2
        assert capsys.readouterr().err == (
            "refused: (document) (reading): the text nests arrays or objects deeper than the reader follows\n"
        )
        preset = write_preset(tmp_path / "deep.yaml", "kind: specimen\nx: " + "[" * 100000 + "\n")
        assert run(["generate", str(preset), "--out", str(tmp_path / "out")]) == 2
        assert capsys.readouterr().err.endswith(": it nests deeper than the YAML reader follows\n")
        assert not (tmp_path / "out").exists()

    def test_a_repeated_key_is_refused_through_generate(self, tmp_path: Path, capsys: Any) -> None:
        text = preset_text(neutral_passport()).replace("kind: specimen\n", "kind: specimen\nkind: specimen\n", 1)
        path = write_preset(tmp_path / "specimen.yaml", text)
        assert run(["generate", str(path), "--out", str(tmp_path / "out")]) == 2
        assert capsys.readouterr().err == "error: duplicate YAML key: 'kind'\n"
        assert not (tmp_path / "out").exists()
