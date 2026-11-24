"""`rule:data-type.base-type-native-declaration-missing` — a finding, never a load failure.

``native_declaration`` is the C type name generated code declares for a base
type. It is required by ``check-model`` and never derived: the architect fills it
in. One project built the way an architect builds one — ``swcraft init`` and one
hand-written base type — carries the three properties together, and each is
asserted here on that one project:

1. it **loads** — the field stays optional in ``schema.py``, so the read-only
   commands still read the project;
2. ``check-model`` **reports** it, once, under the rule, naming the base type —
   and the editing protocol's pass D refuses a change set that creates one, as
   ``consistency-violation``;
3. ``generate arxml`` and ``validate`` **refuse** it, naming the base type, and
   no ARXML file exists afterwards: the refusal is raised while the document is
   built in memory.

A blank value is the same case as an absent one. A declared value clears all
three and is emitted exactly as written.

What this does **not** reach: whether a declared value is a C type name at all —
any non-blank string is accepted and emitted.
"""

from __future__ import annotations

from pathlib import Path

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import declared_outputs, model_element_path
from swcraft_tools.cli import main
from swcraft_tools.data_description import read_data_description
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

RULE = "data-type.base-type-native-declaration-missing"

#: Absent, and blank: the rule and the writer both read ``.strip()``.
_MISSING = pytest.mark.parametrize("declared", [None, "   "], ids=["absent", "blank"])


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """``swcraft ARGV`` in process; the exit code, 0 when ``main`` returns."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: str | None) -> Path:
    """A fresh ``swcraft init`` project plus one base type, ``raw_base``, declaring *declared*."""
    project = tmp_path / "proj"
    assert _run(monkeypatch, "init", str(project)) == 0
    document: dict[str, object] = {"name": "raw_base", "kind": "base_type", "size": 8, "encoding": "NONE"}
    if declared is not None:
        document["native_declaration"] = declared
    path = model_element_path(project / "model", "raw_base", kind="base_type")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return project


@_MISSING
def test_it_loads_and_a_read_only_command_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: str | None
) -> None:
    project = _project(tmp_path, monkeypatch, declared)

    loaded = read_data_description(str(project / "model")).base("raw_base")
    assert loaded is not None
    assert loaded.native_declaration == (declared or "")
    assert _run(monkeypatch, "list", "base_types", "--project", str(project / "swcraft.yaml")) == 0


@_MISSING
def test_check_model_reports_it_once_under_the_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], declared: str | None
) -> None:
    project = _project(tmp_path, monkeypatch, declared)

    records = check_cross_layer_consistency(str(project / "model")).records
    assert [record.rule for record in records] == [RULE], records
    (subject,) = records[0].subjects
    assert (subject.layer, subject.element) == ("base_types", "raw_base")

    capsys.readouterr()
    assert _run(monkeypatch, "check-model", "--project", str(project / "swcraft.yaml")) == 1
    assert "Base type 'raw_base' declares no native_declaration" in capsys.readouterr().err


@_MISSING
def test_generate_and_validate_refuse_it_naming_the_base_type_and_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], declared: str | None
) -> None:
    project = _project(tmp_path, monkeypatch, declared)
    arxml = declared_outputs(project)["arxml"]

    # `generate arxml` prints the writer's refusal and `validate` the finding its
    # consistency check reports first -- each pinned by its own sentence, so an
    # unrelated message that happens to carry both words cannot pass.
    expected = {
        ("generate", "arxml"): "Base type(s) ['raw_base'] in data_types/base/ declare no native_declaration",
        ("validate",): "Base type 'raw_base' declares no native_declaration",
    }
    for command, sentence in expected.items():
        capsys.readouterr()
        assert _run(monkeypatch, *command, "--project", str(project / "swcraft.yaml")) == 1, command
        stderr = capsys.readouterr().err
        assert sentence in stderr, (command, stderr)
        assert not arxml.exists(), f"{command} left {arxml} on disk"

    output = tmp_path / "direct.arxml"
    with pytest.raises(
        ValueError, match=r"Base type\(s\) \['raw_base'\] in data_types/base/ declare no native_declaration"
    ):
        generate_arxml(str(project / "model"), str(output))
    assert not output.exists()


def test_a_declared_value_clears_all_three_and_is_emitted_exactly_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch, "uint8_t")

    assert check_cross_layer_consistency(str(project / "model")).records == ()
    assert _run(monkeypatch, "generate", "arxml", "--project", str(project / "swcraft.yaml")) == 0
    text = declared_outputs(project)["arxml"].read_text(encoding="utf-8")
    assert text.count("<NATIVE-DECLARATION>") == 1
    assert "<NATIVE-DECLARATION>uint8_t</NATIVE-DECLARATION>" in text


def test_pass_d_refuses_a_change_set_that_creates_one(tmp_path: Path) -> None:
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

    response = handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project / "model"),
            "intent": "a base type with no native declaration",
            "operations": [
                {
                    "operation": "create",
                    "address": {"layer": "base_types", "element": "Raw8"},
                    "value": {"name": "Raw8", "kind": "base_type", "size": 8, "encoding": "NONE"},
                }
            ],
        },
        project=project,
    )

    assert response["error"] == "consistency-violation", response
    assert "Base type 'Raw8' declares no native_declaration" in response["message"], response


@pytest.mark.parametrize("blank", ["", "   "], ids=["cleared", "blank"])
def test_pass_d_refuses_a_change_set_that_blanks_one_a_base_type_declares(tmp_path: Path, blank: str) -> None:
    """An ``update`` of an existing base type's declaration to nothing is the same finding as a create without one.

    An ``update`` is the one shape that takes a declaration away (a ``remove`` of a
    field is ``schema-violation``), and pass D refuses it at the field.
    """
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    address = {"layer": "base_types", "element": "float32_base", "path": [{"field": "native_declaration"}]}
    before = {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")}

    response = handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project / "model"),
            "intent": "a base type's native declaration taken away",
            "operations": [{"operation": "update", "address": address, "value": blank}],
        },
        project=project,
    )

    assert response["error"] == "consistency-violation", response
    assert "Base type 'float32_base' declares no native_declaration" in response["message"], response
    assert response["addresses"] == [address], response
    assert {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")} == before
