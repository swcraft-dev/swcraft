"""The trace link search's configuration file: its keys, its defaults and its refusals."""

from __future__ import annotations

import hashlib
import typing
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools.model_state import load_model_state
from swcraft_tools.trace_search import (
    DEFAULT_NAME,
    TARGET_KINDS,
    TargetKind,
    TraceConfig,
    TraceConfigUnreadableError,
    TraceExperimentError,
    check_config_names,
    load_config,
    parse_config,
)
from trace_models import TraceModel, config_document, port, runnable


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "trace.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class TestTheDefaults:
    @pytest.mark.parametrize("text", ["", "# comments only\n# and another\n"])
    def test_an_empty_file_is_the_defaults(self, tmp_path: Path, text: str) -> None:
        config, _digest = load_config(str(_write(tmp_path, text)))
        assert config == TraceConfig()

    def test_the_defaults(self) -> None:
        config = parse_config(None)
        assert TraceConfig.kind == DEFAULT_NAME == config.name == "trace"
        assert config.output is None
        assert config.search.candidates == 5
        assert config.search.labelling == []
        assert config.search.labelling_share == 0.05
        assert config.constraints.documents is None
        assert config.constraints.without_document is True
        assert config.constraints.requirements is None
        assert tuple(config.constraints.target_kinds) == TARGET_KINDS and len(TARGET_KINDS) == 10
        assert config.constraints.exclude == []

    def test_the_kind_literal_is_the_kinds_tuple(self) -> None:
        assert typing.get_args(TargetKind) == TARGET_KINDS

    def test_each_key_is_read_from_its_section(self) -> None:
        config = parse_config(
            config_document(
                name="brakes",
                output="dist/elsewhere",
                candidates=3,
                labelling=["Spec A"],
                labelling_share=0.5,
                documents=["Spec A", "Spec B"],
                without_document=False,
                requirements=["SR-1"],
                target_kinds=["swc", "port"],
                exclude=["Diag"],
            )
        )
        assert (config.name, config.output) == ("brakes", "dist/elsewhere")
        assert (config.search.candidates, config.search.labelling, config.search.labelling_share) == (
            3,
            ["Spec A"],
            0.5,
        )
        constraints = config.constraints
        assert constraints.documents == ["Spec A", "Spec B"] and constraints.without_document is False
        assert constraints.requirements == ["SR-1"] and constraints.target_kinds == ["swc", "port"]
        assert constraints.exclude == ["Diag"]

    def test_labelling_all_against_a_list(self) -> None:
        assert parse_config({"search": {"labelling": "all"}}).search.labelling == "all"
        assert parse_config({"search": {"labelling": ["all"]}}).search.labelling == ["all"]

    def test_the_digest_is_of_the_bytes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "name: x\n")
        _config, digest = load_config(str(path))
        assert digest == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


class TestTheFileRefusals:
    @pytest.mark.parametrize(
        ("document", "line"),
        [
            ({"kind": "trace"}, "'kind': Extra inputs are not permitted"),
            ({"search": {"strictness": 1}}, "'search.strictness': Extra inputs are not permitted"),
            ({"constraints": {"candidates": 2}}, "'constraints.candidates': Extra inputs are not permitted"),
            ({"search": {"candidates": "5"}}, "'search.candidates': Input should be a valid integer"),
            ({"search": {"candidates": True}}, "'search.candidates': Input should be a valid integer"),
            ({"search": {"candidates": 0}}, "'search.candidates': Input should be greater than or equal to 1"),
            ({"search": {"labelling_share": 0}}, "'search.labelling_share': Input should be greater than 0"),
            ({"search": {"labelling_share": 1.5}}, "'search.labelling_share': Input should be less than or equal to 1"),
            (
                {"search": {"labelling": ["A", "B", "A"]}},
                "'search.labelling': labelling names a document more than once: A",
            ),
            ({"search": {"labelling": "some"}}, "'search.labelling"),
            ({"constraints": {"target_kinds": ["swc", "signal"]}}, "'constraints.target_kinds.1'"),
            ({"constraints": {"without_document": "yes"}}, "'constraints.without_document'"),
            ({"name": None}, "'name'"),
            ({"name": "CON"}, "'name': name 'CON' names a device Windows reserves (CON)"),
        ],
        ids=lambda value: str(value)[:40],
    )
    def test_a_refusal_names_its_dotted_key(self, document: dict[str, Any], line: str) -> None:
        with pytest.raises(TraceExperimentError) as caught:
            parse_config(document)
        assert str(caught.value).startswith("the configuration file is refused: ")
        assert line in str(caught.value)

    @pytest.mark.parametrize("text", ["- a\n- b\n", "a scalar\n"])
    def test_a_document_that_is_not_a_mapping(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(TraceExperimentError, match="must be a mapping of keys to values"):
            load_config(str(_write(tmp_path, text)))

    def test_not_yaml_names_no_path(self, tmp_path: Path) -> None:
        with pytest.raises(TraceExperimentError) as caught:
            load_config(str(_write(tmp_path, "name: [unclosed\n")))
        assert str(caught.value).startswith("the configuration file is not valid YAML: ")
        assert str(tmp_path) not in str(caught.value)

    def test_not_utf8_is_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.yaml"
        path.write_bytes(b"name: \xff\n")
        with pytest.raises(TraceConfigUnreadableError, match="it is not UTF-8 text"):
            load_config(str(path))

    def test_a_missing_file_is_unreadable(self, tmp_path: Path) -> None:
        with pytest.raises(TraceConfigUnreadableError, match="cannot be read"):
            load_config(str(tmp_path / "absent.yaml"))


def _state(tmp_path: Path):
    model = TraceModel()
    model.requirement("SR-1", "One", document="Spec A")
    model.requirement("SR-2", "Two", document="  Spec B ")
    model.requirement("SR-3", "Three", document="   ")
    model.sr_interface("ISig", [("value", "")])
    model.component("Ctl", ports=[port("pSig", interface="ISig")], runnables=[runnable("Step")])
    model.composition("Top")
    return load_model_state(model.write(tmp_path / "model"))


class TestTheModelNames:
    def test_names_the_model_declares_pass(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        config = parse_config(
            config_document(
                documents=["Spec A", "Spec B"],
                labelling=["Spec B"],
                requirements=["SR-1", "SR-3"],
                exclude=["Ctl", "pSig", "Step", "ISig", "Top"],
            )
        )
        check_config_names(config, state)

    @pytest.mark.parametrize(
        ("keys", "sentence"),
        [
            (
                {"documents": ["Spec C"]},
                "'constraints.documents' names document 'Spec C', which no requirement of the model carries",
            ),
            (
                {"documents": [" Spec B "]},
                "'constraints.documents' names document ' Spec B ' with surrounding white space: a requirement's "
                "document is compared with its surrounding white space removed, so write 'Spec B'",
            ),
            (
                {"labelling": ["  Spec A "]},
                "'search.labelling' names document '  Spec A ' with surrounding white space: a requirement's "
                "document is compared with its surrounding white space removed, so write 'Spec A'",
            ),
            (
                {"labelling": ["spec a"]},
                "'search.labelling' names document 'spec a', which no requirement of the model carries",
            ),
            (
                {"documents": ["Spec A"], "labelling": ["Spec B"]},
                "'search.labelling' names document 'Spec B', which 'constraints.documents' leaves out",
            ),
            (
                {"requirements": ["SR-9"]},
                "'constraints.requirements' names requirement 'SR-9', which the model does not declare",
            ),
            (
                {"exclude": ["Nothing"]},
                "'constraints.exclude' names element 'Nothing', which no linkable layer of the model declares",
            ),
        ],
    )
    def test_a_name_the_model_does_not_declare_is_refused(
        self, tmp_path: Path, keys: dict[str, Any], sentence: str
    ) -> None:
        state = _state(tmp_path)
        with pytest.raises(TraceExperimentError) as caught:
            check_config_names(parse_config(config_document(**keys)), state)
        assert str(caught.value) == sentence
        assert str(tmp_path) not in str(caught.value)

    def test_labelling_all_checks_no_document(self, tmp_path: Path) -> None:
        check_config_names(parse_config({"search": {"labelling": "all"}}), _state(tmp_path))
