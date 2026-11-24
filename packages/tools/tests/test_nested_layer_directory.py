"""The kernel reads a layer stored in a directory of two segments.

A layer's ``storage.directory`` is a model-root-relative path of one or two segments. This module runs
the composer, the one layer scan, :func:`free_storage_path` and its ``~N`` suffix, the storage
portability rule and the element order on a nested value, over a synthetic registry (through
``use_registry()`` or an explicit ``registry=``): the flat ``synthetic`` layer and two layers stored in
``nest/alpha`` and ``nest/beta`` under one model root.

The directory policy (equal and case-only directories refused, siblings accepted) is
``test_model_registry.py``'s; the longest-directory match of the editing audit and of the commit gate
is ``test_editing_audit.py``'s and ``test_session_generic.py``'s; the component import's target scan is
``test_component_exchange_storage.py``'s; the meta-schema's two-segment pattern is
``test_model_contract.py``'s.

**What this does not reach.** No shipped layer is nested here, so nothing below says a shipped
consumer reads a nested layer correctly, only that the kernel functions it would call do. A nesting
(placement-carrying) layer in a two-segment directory is reached by the pure composer alone: the
synthetic kind declares no placement field, so no registry here has one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_tools.model_registry import ModelRegistry, StorageRecord, compose_storage_path, use_registry
from swcraft_tools.model_state import free_storage_path, load_layer, load_model_state, scan_layer
from swcraft_tools.validator import storage_portability_records
from synthetic_support import SIBLING_NESTED_LAYERS, nested_registry

ALPHA, BETA = (name for name, _ in SIBLING_NESTED_LAYERS)


@pytest.fixture
def registry() -> ModelRegistry:
    return nested_registry(*SIBLING_NESTED_LAYERS)


def _write(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {"name": name, "title": name, "composition": "VehicleTop"}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


class TestTheComposer:
    def test_a_flat_layer_in_a_two_segment_directory(self, registry: ModelRegistry) -> None:
        assert compose_storage_path(registry.storage(ALPHA), "Foo") == "nest/alpha/Foo.yaml"

    def test_a_nesting_record_in_a_two_segment_directory(self) -> None:
        record = StorageRecord(directory="nest/beta", nesting="group", file_suffix=".yaml", filename_from="name")
        assert compose_storage_path(record, "Foo", {"group": "g"}) == "nest/beta/g/Foo.yaml"
        assert compose_storage_path(record, "Foo", {"group": ""}) == "nest/beta/Foo.yaml"


class TestTheScan:
    def test_the_scan_reads_the_nested_directory_and_nothing_around_it(
        self, tmp_path: Path, registry: ModelRegistry
    ) -> None:
        root = tmp_path / "model"
        _write(root / "nest" / "alpha" / "Foo.yaml", "Foo")
        _write(root / "nest" / "alpha" / "sub" / "Deep.yaml", "Deep")  # a flat layer reads one level
        _write(root / "nest" / "Parent.yaml", "Parent")  # `nest/` itself is no layer's
        _write(root / "nest" / "beta" / "Bar.yaml", "Bar")

        alpha = scan_layer(root, ALPHA, registry=registry)
        assert alpha.present
        assert [entry.path.relative_to(root).as_posix() for entry in alpha.files] == ["nest/alpha/Foo.yaml"]
        beta = scan_layer(root, BETA, registry=registry)
        assert [entry.path.relative_to(root).as_posix() for entry in beta.files] == ["nest/beta/Bar.yaml"]

    def test_the_model_state_holds_each_nested_layer_apart(self, tmp_path: Path, registry: ModelRegistry) -> None:
        root = tmp_path / "model"
        _write(root / "nest" / "alpha" / "Foo.yaml", "Foo")
        _write(root / "nest" / "beta" / "Foo.yaml", "Foo")  # one name in two layers is two elements
        _write(root / "synthetic" / "Flat.yaml", "Flat")

        with use_registry(registry):
            state = load_model_state(root)
        assert [element.name for element in state[ALPHA]] == ["Foo"]
        assert [element.name for element in state[BETA]] == ["Foo"]
        assert [element.name for element in state["synthetic"]] == ["Flat"]
        assert ALPHA not in state.absent and BETA not in state.absent

    def test_an_absent_nested_directory_is_reported_absent(self, tmp_path: Path, registry: ModelRegistry) -> None:
        root = tmp_path / "model"
        (root / "nest").mkdir(parents=True)
        assert not scan_layer(root, ALPHA, registry=registry).present
        assert load_layer(root, ALPHA, registry=registry) == []


class TestTheFreeStoragePath:
    def test_the_plain_name_is_free(self, tmp_path: Path, registry: ModelRegistry) -> None:
        free = free_storage_path(tmp_path, ALPHA, "Foo", registry=registry)
        assert (free.path, free.suffixed, free.occupied_by_self) == ("nest/alpha/Foo.yaml", False, False)

    def test_a_case_only_name_takes_the_suffix_in_the_nested_directory(
        self, tmp_path: Path, registry: ModelRegistry
    ) -> None:
        _write(tmp_path / "nest" / "alpha" / "Foo.yaml", "Foo")
        free = free_storage_path(tmp_path, ALPHA, "foo", registry=registry)
        assert (free.path, free.suffixed) == ("nest/alpha/foo~1.yaml", True)
        own = free_storage_path(tmp_path, ALPHA, "Foo", registry=registry)
        assert (own.path, own.occupied_by_self) == ("nest/alpha/Foo.yaml", True)

    def test_a_sibling_directory_occupies_nothing(self, tmp_path: Path, registry: ModelRegistry) -> None:
        _write(tmp_path / "nest" / "beta" / "Foo.yaml", "Foo")
        free = free_storage_path(tmp_path, ALPHA, "foo", registry=registry, reserved=("nest/beta/foo.yaml",))
        assert (free.path, free.suffixed) == ("nest/alpha/foo.yaml", False)

    def test_a_reserved_path_in_the_nested_directory_occupies(self, tmp_path: Path, registry: ModelRegistry) -> None:
        free = free_storage_path(tmp_path, ALPHA, "Foo", registry=registry, reserved=("nest/alpha/FOO.yaml",))
        assert (free.path, free.suffixed) == ("nest/alpha/Foo~1.yaml", True)


class TestTheStoragePortabilityRule:
    """Over a listing, so that the case-only pair needs no case-sensitive filesystem."""

    def test_a_case_only_pair_in_one_nested_directory_is_a_finding(self, registry: ModelRegistry) -> None:
        stored = {ALPHA: ["nest/alpha/Foo.yaml", "nest/alpha/foo.yaml"]}
        names = {"nest/alpha/Foo.yaml": "Foo", "nest/alpha/foo.yaml": "foo"}
        records = storage_portability_records(stored, lambda _layer, path: names[path], registry=registry)
        assert [(record.rule, record.subjects[0].layer) for record in records] == [
            ("storage.filename-case-collision", ALPHA)
        ]
        assert {subject.element for subject in records[0].subjects} == {"Foo", "foo"}

    def test_one_name_in_two_nested_directories_is_none(self, registry: ModelRegistry) -> None:
        stored = {ALPHA: ["nest/alpha/Foo.yaml"], BETA: ["nest/beta/foo.yaml"]}
        names = {"nest/alpha/Foo.yaml": "Foo", "nest/beta/foo.yaml": "foo"}
        assert storage_portability_records(stored, lambda _layer, path: names[path], registry=registry) == []

    def test_a_reserved_device_name_in_a_nested_directory_is_a_finding(self, registry: ModelRegistry) -> None:
        records = storage_portability_records(
            {BETA: ["nest/beta/CON.yaml"]}, lambda _layer, _path: "CON", registry=registry
        )
        assert [(record.rule, record.subjects[0].layer) for record in records] == [
            ("storage.reserved-device-name", BETA)
        ]


class TestTheElementOrder:
    def test_the_order_is_the_composed_path_not_the_file_name(self, tmp_path: Path, registry: ModelRegistry) -> None:
        root = tmp_path / "model"
        directory = root / "nest" / "alpha"
        _write(directory / "b.yaml", "A")
        _write(directory / "a.yaml", "B")
        _write(directory / "zz.yaml", "Zebra")
        _write(directory / "aa.yaml", "alpha")
        assert [element.name for element in load_layer(root, ALPHA, registry=registry)] == [
            "A",
            "B",
            "Zebra",
            "alpha",
        ]
