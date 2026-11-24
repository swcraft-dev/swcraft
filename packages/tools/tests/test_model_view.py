"""``ModelView``: the one edge between the validator's checks and the model's storage.

A view reads nothing when built, reads each layer on its first access through its reader, holds
what it read for its life and hands every caller a new list. A read that raises is not held. The
composer builds one view and hands it to every check, so a composed run reads each layer once.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from swcraft_tools.data_description import DataDescription
from swcraft_tools.model_state import ElementParseError, ModelRootNotFoundError, RetiredStorageError
from swcraft_tools.reader import read_hardware, read_swc_model
from swcraft_tools.validator import check_connector_consistency, check_cross_layer_consistency, model_view
from swcraft_tools.validator.model_view import ModelView, as_view

EXAMPLE_MODEL = REPO_ROOT / "examples" / "basic" / "model"

#: Every layer accessor of the view, with the reader it calls.
LAYER_READERS: dict[str, str] = {
    "swcs": "read_swc_model",
    "interfaces": "read_interfaces",
    "requirements": "read_requirements",
    "compositions": "read_compositions",
    "connectors": "read_connectors",
    "tasks": "read_tasks",
    "sequences": "read_sequences",
    "hardware": "read_hardware",
    "mapping_sets": "read_mapping_sets",
    "data_description": "read_data_description",
}


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """A writable copy of the basic example's model."""
    root = tmp_path / "model"
    shutil.copytree(EXAMPLE_MODEL, root)
    return root


def _count(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """Route the view's reader *name* through a counter that records each root it is handed."""
    calls: list[Any] = []
    real: Callable[[Any], Any] = getattr(model_view, name)

    def counted(root: Any) -> Any:
        calls.append(root)
        return real(root)

    monkeypatch.setattr(model_view, name, counted)
    return calls


def _count_all(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    return {reader: _count(monkeypatch, reader) for reader in LAYER_READERS.values()}


def _broken_requirement(model_root: Path) -> Path:
    path = model_root / "requirements" / "BROKEN.yaml"
    path.write_text("id: [unclosed\n", encoding="utf-8", newline="\n")
    return path


def _broken_component(model_root: Path) -> Path:
    path = model_root / "swc" / "broken" / "Broken.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    return path


class TestReading:
    def test_the_table_names_every_layer_accessor_of_the_view(self) -> None:
        accessors = {
            name for name, member in vars(ModelView).items() if isinstance(member, property) and name != "model_root"
        }

        assert accessors == set(LAYER_READERS)

    def test_nothing_is_read_on_construction(self, monkeypatch: pytest.MonkeyPatch, model_root: Path) -> None:
        calls = _count_all(monkeypatch)
        for name in ("scan_layer", "declared_identity"):
            monkeypatch.setattr(model_view, name, lambda *_args, **_kwargs: pytest.fail("read on construction"))

        ModelView(model_root)
        as_view(str(model_root))

        assert all(not reads for reads in calls.values()), calls

    @pytest.mark.parametrize("accessor", sorted(LAYER_READERS))
    def test_each_layer_is_read_once_per_view(
        self, monkeypatch: pytest.MonkeyPatch, model_root: Path, accessor: str
    ) -> None:
        calls = _count_all(monkeypatch)
        view = ModelView(str(model_root))

        first = getattr(view, accessor)
        second = getattr(view, accessor)

        assert first == second
        assert {reader: len(reads) for reader, reads in calls.items() if reads} == {LAYER_READERS[accessor]: 1}

    def test_one_view_reads_every_layer_with_its_own_reader(
        self, monkeypatch: pytest.MonkeyPatch, model_root: Path
    ) -> None:
        calls = _count_all(monkeypatch)
        view = ModelView(model_root)

        for accessor in LAYER_READERS:
            getattr(view, accessor)

        assert {reader: len(reads) for reader, reads in calls.items()} == dict.fromkeys(LAYER_READERS.values(), 1)

    def test_two_views_read_twice(self, monkeypatch: pytest.MonkeyPatch, model_root: Path) -> None:
        calls = _count(monkeypatch, "read_swc_model")

        ModelView(model_root).swcs
        ModelView(model_root).swcs

        assert len(calls) == 2

    def test_the_root_is_handed_to_the_reader_as_spelled(
        self, monkeypatch: pytest.MonkeyPatch, model_root: Path
    ) -> None:
        calls = _count(monkeypatch, "read_swc_model")
        spelled = f"{model_root}/"

        ModelView(spelled).swcs
        ModelView(model_root).swcs

        assert calls == [spelled, str(model_root)]

    def test_the_view_states_its_root_as_spelled(self, model_root: Path) -> None:
        spelled = f"{model_root}/"

        assert ModelView(spelled).model_root == spelled
        assert ModelView(model_root).model_root == str(model_root)


class TestTheReadersErrors:
    def test_a_missing_root_raises_on_the_first_swcs_access_with_the_readers_own_error(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "absent") + "/"
        view = ModelView(missing)
        with pytest.raises(ModelRootNotFoundError) as expected:
            read_swc_model(missing)

        with pytest.raises(ModelRootNotFoundError) as raised:
            view.swcs

        assert raised.value.render(full_path=True) == expected.value.render(full_path=True)
        assert str(raised.value) == str(expected.value)

    def test_a_malformed_file_raises_the_readers_error_only_for_its_layer(self, model_root: Path) -> None:
        _broken_component(model_root)
        view = ModelView(model_root)

        assert view.interfaces
        with pytest.raises(ElementParseError):
            view.swcs

    def test_a_retired_directory_raises_the_readers_refusal(self, model_root: Path) -> None:
        retired = model_root / "mapping_sets"
        retired.mkdir()
        (retired / "Old.yaml").write_text("name: Old\n", encoding="utf-8", newline="\n")
        with pytest.raises(RetiredStorageError) as expected:
            read_hardware(str(model_root))

        with pytest.raises(RetiredStorageError) as raised:
            ModelView(model_root).hardware

        assert str(raised.value) == str(expected.value)

    def test_a_failed_read_is_not_held(self, monkeypatch: pytest.MonkeyPatch, model_root: Path) -> None:
        broken = _broken_component(model_root)
        calls = _count(monkeypatch, "read_swc_model")
        view = ModelView(model_root)

        for _ in range(2):
            with pytest.raises(ElementParseError):
                view.swcs
        broken.unlink()
        repaired = view.swcs

        assert len(calls) == 3
        assert {swc.name for swc in repaired} == {swc.name for swc in read_swc_model(str(model_root))}


class TestWhatIsHandedOut:
    @pytest.mark.parametrize("accessor", sorted(set(LAYER_READERS) - {"data_description"}))
    def test_each_access_returns_a_new_list_over_the_same_elements(self, model_root: Path, accessor: str) -> None:
        view = ModelView(model_root)
        first = getattr(view, accessor)
        first.append(object())
        first.reverse()

        second = getattr(view, accessor)
        third = getattr(view, accessor)

        assert second is not third
        assert len(second) == len(first) - 1
        assert all(a is b for a, b in zip(second, third, strict=True))

    def test_the_data_description_is_one_object(self, model_root: Path) -> None:
        view = ModelView(model_root)

        assert isinstance(view.data_description, DataDescription)
        assert view.data_description is view.data_description


class TestTheStorageAccessors:
    def test_layer_present_tells_an_absent_hardware_layer_from_an_empty_one(self, model_root: Path) -> None:
        view = ModelView(model_root)
        assert view.layer_present("hardware")

        shutil.rmtree(model_root / "hardware")
        assert not view.layer_present("hardware")

        (model_root / "hardware").mkdir()
        assert view.layer_present("hardware")
        assert view.hardware == []

    def test_the_stored_paths_are_root_relative_posix_and_each_text_is_its_files(self, model_root: Path) -> None:
        view = ModelView(model_root)

        paths = view.stored_paths("swc")

        assert paths
        assert all(path.startswith("swc/") and "\\" not in path for path in paths)
        for path in paths:
            assert view.stored_text("swc", path) == (model_root / path).read_text(encoding="utf-8")

    def test_the_declared_identity_reads_the_layers_own_identity_field(self, model_root: Path) -> None:
        declared = {
            "requirements/PLANTED_A.yaml": ("requirements", "id: REQ-PLANTED-A\nname: Ignored\n", "REQ-PLANTED-A"),
            "requirements/PLANTED_B.yaml": ("requirements", "id: REQ-PLANTED-B\n", "REQ-PLANTED-B"),
            "requirements/NO_ID.yaml": ("requirements", "name: NoIdentity\n", None),
            "swc/planted/Planted.yaml": ("swc", "name: PlantedComponent\nid: Ignored\n", "PlantedComponent"),
        }
        for path, (_layer, text, _identity) in declared.items():
            (model_root / path).parent.mkdir(parents=True, exist_ok=True)
            (model_root / path).write_text(text, encoding="utf-8", newline="\n")
        view = ModelView(model_root)

        assert {path: view.declared_identity(layer, path) for path, (layer, _text, _identity) in declared.items()} == {
            path: identity for path, (_layer, _text, identity) in declared.items()
        }


class TestAsView:
    def test_a_view_is_handed_back_unchanged(self, model_root: Path) -> None:
        view = ModelView(model_root)

        assert as_view(view) is view

    @pytest.mark.parametrize("spelling", [str, Path], ids=["str", "path"])
    def test_a_root_gets_a_new_view(self, model_root: Path, spelling: Callable[[Path], Any]) -> None:
        view = as_view(spelling(model_root))

        assert isinstance(view, ModelView)
        assert view.swcs == ModelView(model_root).swcs


#: The order of the composer's first read of each layer on the basic example: the first error a
#: broken model raises follows it.
FIRST_READS = (
    "read_swc_model",
    "read_requirements",
    "read_interfaces",
    "read_compositions",
    "read_tasks",
    "read_hardware",
    "read_connectors",
    "read_data_description",
    "read_sequences",
    "read_mapping_sets",
)


class TestTheComposer:
    def test_the_composer_reads_each_layer_first_in_the_order_it_always_did(
        self, monkeypatch: pytest.MonkeyPatch, model_root: Path
    ) -> None:
        order: list[str] = []
        for reader in LAYER_READERS.values():
            real: Callable[[Any], Any] = getattr(model_view, reader)

            def recorded(root: Any, *, _real: Callable[[Any], Any] = real, _name: str = reader) -> Any:
                order.append(_name)
                return _real(root)

            monkeypatch.setattr(model_view, reader, recorded)

        check_cross_layer_consistency(str(model_root))

        assert tuple(dict.fromkeys(order)) == FIRST_READS

    def test_a_check_never_reads_a_layer_it_does_not_check(self, model_root: Path) -> None:
        _broken_requirement(model_root)
        with pytest.raises(ElementParseError):
            ModelView(model_root).requirements

        result = check_connector_consistency(str(model_root))

        assert result.consistent, result.issues

    def test_one_cross_layer_run_reads_each_layer_once(self, monkeypatch: pytest.MonkeyPatch, model_root: Path) -> None:
        calls = _count_all(monkeypatch)

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues
        assert {reader: len(reads) for reader, reads in calls.items()} == dict.fromkeys(LAYER_READERS.values(), 1)

    def test_the_composer_reads_a_view_it_is_handed_and_no_other(
        self, monkeypatch: pytest.MonkeyPatch, model_root: Path
    ) -> None:
        view = ModelView(model_root)
        for accessor in LAYER_READERS:
            getattr(view, accessor)
        calls = _count_all(monkeypatch)

        result = check_cross_layer_consistency(view)

        assert result == check_cross_layer_consistency(str(model_root))
        assert all(len(reads) == 1 for reads in calls.values())
