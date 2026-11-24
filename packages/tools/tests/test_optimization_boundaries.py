"""Contract tests for the four replaceable optimization boundaries.

Each class pins one boundary's entry point(s) -- input/output types, raised
exceptions, side effects -- end to end through its declared public types. A
reimplementation of a boundary's internals must keep them green without changing
the entry point's signature or the CLI/MCP contracts built on it:

1. model/project indexing & graph traversal -- ``snapshot.load_snapshot()`` +
   ``impact.build_reference_graph()``
2. ARXML parse/import indexing -- ``arxml_reader.read_arxml()`` +
   ``arxml_import.registry.PathRegistry``
3. ARXML generation/rendering -- ``writer.arxml_generation.generate_arxml()``
4. semantic diff/impact queries -- ``differ.diff_snapshots()`` +
   ``impact.impact_of()``

No test imports a private helper or mocks an internal call: that would pin the
internals the boundary exists to make replaceable. Unlike ``test_snapshot.py``,
``test_impact.py``, ``test_differ.py``, ``test_writer_generators.py`` and
``test_arxml_import_types.py``, every scenario goes through real on-disk YAML and
``load_snapshot()``/``read_arxml()`` (or ``PathRegistry``'s public API), not
hand-built dataclasses.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml
from conftest import _root_composition, write_model_element
from swcraft_tools.arxml_import.registry import NameCollisionError, PathRegistry
from swcraft_tools.arxml_reader import ImportModel, read_arxml
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.impact import ElementRef, build_reference_graph, impact_of
from swcraft_tools.query import EdgeDetail
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.writer.arxml_generation import generate_arxml

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"

# ---------------------------------------------------------------------------
# Shared model-root builder (boundaries 1, 3, 4)
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _write_model(model_root: Path) -> None:
    """A tiny cross-referenced model: interface -> port -> SWC -> task, a connector and a requirement link."""
    # The interface names a value type, which rests on a base type.
    write_model_element(
        model_root,
        {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float"},
    )
    write_model_element(model_root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
    _write_yaml(
        model_root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "float32"}],
        },
    )
    _write_yaml(
        model_root / "swc" / "sensors" / "SpeedSensorSWC.yaml",
        {
            "name": "SpeedSensorSWC",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "runnables": [{"name": "Run1", "min_start_interval": 0.01, "events": [{"type": "timing", "period": 0.01}]}],
            "handle_termination_and_restart": "no_support",
            # The trace link lives in the component.
            "satisfies": ["REQ-1@1"],
        },
    )
    _write_yaml(
        model_root / "swc" / "control" / "SpeedCtrlSWC.yaml",
        {
            "name": "SpeedCtrlSWC",
            "type": "application",
            "ports": [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
        },
    )
    _write_yaml(
        model_root / "connectors" / "SpeedSensor_to_SpeedCtrl.yaml",
        {
            "name": "SpeedSensor_to_SpeedCtrl",
            "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
            "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
        },
    )
    # The composition the connector above is drawn inside. It names none, so it
    # belongs to the root.
    _root_composition(model_root, ["SpeedSensorSWC", "SpeedCtrlSWC"])
    _write_yaml(
        model_root / "tasks" / "Task10ms.yaml",
        {
            "name": "Task10ms",
            "period": 0.01,
            "priority": 1,
            "partition": "PARTITION0",
            "runnables": [{"swc": "SpeedSensorSWC", "runnable": "Run1"}],
        },
    )
    _write_yaml(
        model_root / "requirements" / "REQ-1.yaml",
        {
            "id": "REQ-1",
            "title": "Speed sensing",
            "description": "SWC provides wheel speed.",
            "version": 1,
            "source": "architecture",
        },
    )


# ---------------------------------------------------------------------------
# Boundary 1: model/project indexing & graph traversal
# (snapshot.load_snapshot + impact.build_reference_graph)
# ---------------------------------------------------------------------------


class TestModelIndexingAndGraphTraversalBoundary:
    """``load_snapshot`` (a pure read) feeding ``build_reference_graph`` (a pure in-memory traversal).

    ``load_snapshot(model_root, rev=None) -> ModelSnapshot`` has no side effects; ``build_reference_graph(snapshot) ->
    dict[ElementRef, dict[ElementRef, tuple[EdgeDetail, ...]]]``."""

    def test_load_snapshot_reads_all_layers_into_typed_lists(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)

        snapshot = load_snapshot(str(model_root), rev=None)

        assert isinstance(snapshot, ModelSnapshot)
        assert {s.name for s in snapshot.swcs} == {"SpeedSensorSWC", "SpeedCtrlSWC"}
        assert {i.name for i in snapshot.interfaces} == {"ISpeed"}
        assert {t.name for t in snapshot.tasks} == {"Task10ms"}
        assert {r.id for r in snapshot.requirements} == {"REQ-1"}
        assert {c.name for c in snapshot.connectors} == {"SpeedSensor_to_SpeedCtrl"}
        # The fixture declares a root composition (a connector belongs to one), so
        # `compositions` is populated; `sequences`, `hardware` and `mapping_sets`
        # stay empty lists rather than None/missing.
        assert {c.name for c in snapshot.compositions} == {"Root"}
        assert snapshot.sequences == []
        assert snapshot.hardware == []
        assert snapshot.mapping_sets == []
        # Every layer the registry declares has an attribute, and the empty ones
        # are empty because the fixture declares nothing in them.
        from swcraft_tools.model_registry import production_registry

        assert all(hasattr(snapshot, layer.snapshot_attribute or layer.name) for layer in production_registry().layers)

    def test_load_snapshot_missing_model_root_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            load_snapshot(str(tmp_path / "does_not_exist"), rev=None)

    def test_build_reference_graph_produces_expected_edges(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        snapshot = load_snapshot(str(model_root), rev=None)

        graph = build_reference_graph(snapshot)

        # ISpeed is referenced by both SWCs' ports -> two dependent edges.
        iface_dependents = graph[ElementRef("interface", "ISpeed")]
        assert {ref.name for ref in iface_dependents} == {"SpeedSensorSWC", "SpeedCtrlSWC"}

        # SpeedSensorSWC's Run1 runnable is mapped onto Task10ms -> one edge,
        # with the runnable name carried as detail (ports/runnables are never
        # their own graph node, see impact.py's module docstring).
        swc_dependents = graph[ElementRef("swc", "SpeedSensorSWC")]
        assert swc_dependents[ElementRef("task", "Task10ms")] == (EdgeDetail("runnable", "Run1"),)

    def test_build_reference_graph_produces_hardware_budget_edges(self, tmp_path):
        """A memory is referenced by the SWCs budgeting against it and a core by the tasks assigned to it.

        The node is keyed ``hardware`` by the element's own name, because
        ``Task.core`` references that name.
        """
        model_root = tmp_path / "model"
        _write_model(model_root)
        _write_yaml(
            model_root / "hardware" / "RAM.yaml",
            {
                "name": "RAM",
                "kind": "memory",
                "type": "RAM",
                "size": 262144,
                "accessible": ["CORE0"],
                "threshold": 0.8,
            },
        )
        _write_yaml(
            model_root / "hardware" / "CORE0.yaml",
            {"name": "CORE0", "kind": "core", "threshold": 0.7},
        )
        _write_yaml(
            model_root / "hardware" / "PARTITION0.yaml",
            {"name": "PARTITION0", "kind": "partition", "core": "CORE0"},
        )
        swc_path = model_root / "swc" / "sensors" / "SpeedSensorSWC.yaml"
        swc_data = yaml.safe_load(swc_path.read_text(encoding="utf-8"))
        swc_data["memory"] = {"RAM": 2048}
        _write_yaml(swc_path, swc_data)
        snapshot = load_snapshot(str(model_root), rev=None)

        graph = build_reference_graph(snapshot)

        assert graph[ElementRef("hardware", "RAM")] == {
            ElementRef("swc", "SpeedSensorSWC"): (EdgeDetail("budget", "2048"),)
        }
        # The task names its partition, and the partition the core.
        assert graph[ElementRef("hardware", "PARTITION0")][ElementRef("task", "Task10ms")] == (
            EdgeDetail("partition", "PARTITION0"),
        )
        assert graph[ElementRef("hardware", "CORE0")][ElementRef("hardware", "PARTITION0")] == (
            EdgeDetail("core", "CORE0"),
        )

    def test_build_reference_graph_duplicate_identity_key_raises(self, tmp_path):
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "swc" / "a" / "Dup.yaml",
            {"name": "Dup", "type": "application", "ports": [], "runnables": []},
        )
        _write_yaml(
            model_root / "swc" / "b" / "Dup.yaml",
            {"name": "Dup", "type": "application", "ports": [], "runnables": []},
        )
        snapshot = load_snapshot(str(model_root), rev=None)

        with pytest.raises(ValueError, match="duplicate key"):
            build_reference_graph(snapshot)


# ---------------------------------------------------------------------------
# Boundary 2: ARXML parse/import indexing
# (arxml_reader.read_arxml + arxml_import.registry.PathRegistry)
# ---------------------------------------------------------------------------


class TestArxmlParseImportIndexingBoundary:
    """``read_arxml(path) -> ImportModel`` (pure parse) feeding ``PathRegistry`` (pure path -> name resolution).

    The ``data_types.arxml`` fixture holds a real app/impl twin (``BrakeModeEnum``,
    linked by the export's DATA-TYPE-MAPPING-SET) and an accidental same-name
    collision with no twin relationship (``DoorStateEnum``): the two cases name
    normalization tells apart."""

    FIXTURE = FIXTURES_DIR / "data_types.arxml"

    def test_read_arxml_returns_import_model_with_typed_data(self):
        model = read_arxml(str(self.FIXTURE))

        assert isinstance(model, ImportModel)
        impl_paths = {t.path for t in model.impl_data_types}
        app_paths = {t.path for t in model.app_primitive_data_types}
        assert "/Acme/Chassis/Types/BrakeModeEnum" in impl_paths
        assert "/Acme/Chassis/ApplicationTypes/BrakeModeEnum" in app_paths
        assert "/Acme/Chassis/Types/DoorStateEnum" in impl_paths
        assert "/Acme/Chassis/ApplicationTypes/DoorStateEnum" in app_paths

    def test_read_arxml_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            read_arxml(str(FIXTURES_DIR / "does_not_exist.arxml"))

    def test_read_arxml_malformed_xml_raises_value_error(self, tmp_path):
        bad_file = tmp_path / "bad.arxml"
        bad_file.write_text("<AUTOSAR><unclosed>", encoding="utf-8")

        with pytest.raises(ValueError, match="Malformed ARXML"):
            read_arxml(str(bad_file))

    def test_registry_twin_collapse_and_per_layer_name_spaces(self):
        """Drives ``PathRegistry`` as ``arxml_import/type_mapping.py`` does.

        Impl types register first; app types collapse onto a known twin or register
        in their own layer. An application and an implementation type of one name
        are two elements of two layers, so the collision suffix fires only for a
        collision inside one layer."""
        model = read_arxml(str(self.FIXTURE))
        impl_by_path = {t.path: t for t in model.impl_data_types}

        registry = PathRegistry()
        for impl in model.impl_data_types:
            registry.register(impl.path, impl.name, "implementation_types")

        # BrakeModeEnum: real app/impl twin -> collapses onto ONE emitted
        # name, recorded as a reduction (never a rename).
        brake_impl_path = "/Acme/Chassis/Types/BrakeModeEnum"
        brake_app_path = "/Acme/Chassis/ApplicationTypes/BrakeModeEnum"
        emitted = registry.register_twin(brake_app_path, brake_impl_path, impl_by_path[brake_impl_path].name)
        assert emitted == "BrakeModeEnum"
        assert registry.resolve(brake_app_path) == "BrakeModeEnum"
        assert registry.resolve(brake_impl_path) == "BrakeModeEnum"
        assert registry.layer_of(brake_app_path) == "implementation_types"
        assert any("app/impl twin collapse" in line for line in registry.reductions)

        # DoorStateEnum: an application type beside a same-name implementation
        # type -- NOT a twin, and no longer a collision: each keeps the declared
        # name in its own layer, and no rename line is recorded.
        door_impl_path = "/Acme/Chassis/Types/DoorStateEnum"
        door_app_path = "/Acme/Chassis/ApplicationTypes/DoorStateEnum"
        emitted_app = registry.register(door_app_path, "DoorStateEnum", "application_types")
        assert emitted_app == "DoorStateEnum"
        assert registry.resolve(door_impl_path) == "DoorStateEnum"
        assert registry.resolve(door_app_path) == "DoorStateEnum"
        assert registry.layer_of(door_impl_path) == "implementation_types"
        assert registry.layer_of(door_app_path) == "application_types"
        assert not any(door_app_path in line for line in registry.renames)

        # A second application type of that name collides inside ONE layer:
        # it gets the deterministic `_app` suffix.
        other_app_path = "/Acme/Body/ApplicationTypes/DoorStateEnum"
        assert registry.register(other_app_path, "DoorStateEnum", "application_types") == "DoorStateEnum_app"
        assert registry.resolve(other_app_path) == "DoorStateEnum_app"
        assert any(other_app_path in line for line in registry.renames)

    def test_registry_duplicate_path_raises_name_collision_error(self):
        registry = PathRegistry()
        registry.register("/Acme/Foo", "Foo", "implementation_types")

        with pytest.raises(NameCollisionError):
            registry.register("/Acme/Foo", "Foo", "implementation_types")


# ---------------------------------------------------------------------------
# Boundary 3: ARXML generation/rendering (writer.arxml_generation.generate_arxml)
# ---------------------------------------------------------------------------


class TestArxmlGenerationRenderingBoundary:
    """``generate_arxml(model_root, output_path) -> str``: the one boundary with a file-write side effect.

    No audit entry is written: audit covers the mutations, never the read-only generators."""

    def test_generate_arxml_writes_file_and_returns_its_path(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"

        result = generate_arxml(str(model_root), str(output_path))

        assert result == str(output_path)
        assert output_path.is_file()
        content = output_path.read_text(encoding="utf-8")
        assert "<AUTOSAR" in content
        assert "SpeedSensorSWC" in content
        assert "ISpeed" in content

    def test_generate_arxml_preserves_a_relative_output_path(self, tmp_path, monkeypatch):
        model_root = tmp_path / "model"
        _write_model(model_root)
        monkeypatch.chdir(tmp_path)

        result = generate_arxml(str(model_root), "out/model.arxml")

        # The property is that the path stays *relative* -- never silently made
        # absolute against some other base. The separator is not part of it:
        # `save()` returns the path through `Path`, so Windows answers
        # `out\model.arxml`, which is the same relative path natively spelled.
        assert result == str(Path("out/model.arxml"))
        assert (tmp_path / result).is_file()

    def test_generate_arxml_creates_missing_parent_directories(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "deeply" / "nested" / "out" / "model.arxml"
        assert not output_path.parent.exists()

        generate_arxml(str(model_root), str(output_path))

        assert output_path.is_file()

    def test_generate_arxml_undeclared_type_raises_value_error(self, tmp_path):
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "interfaces" / "IBad.yaml",
            {
                "name": "IBad",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "no_such_type"}],
            },
        )

        with pytest.raises(ValueError, match="not declared"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_generate_arxml_moves_a_different_previous_file_aside_and_returns_the_path(self, tmp_path):
        """Two side effects, moving the different previous file aside and the write; the return is unchanged."""
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(b"<previous build/>\n")

        result = generate_arxml(str(model_root), str(output_path))

        assert result == str(output_path)
        assert (tmp_path / "out" / "model_old.arxml").read_bytes() == b"<previous build/>\n"
        assert "SpeedSensorSWC" in output_path.read_text(encoding="utf-8")
        assert sorted(entry.name for entry in output_path.parent.iterdir()) == ["model.arxml", "model_old.arxml"]

    def test_generate_arxml_moves_nothing_when_the_bytes_are_the_same(self, tmp_path):
        """Identical bytes move nothing, so ``_old`` stays the previous *build's* file."""
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"
        generate_arxml(str(model_root), str(output_path))
        first = output_path.read_bytes()

        generate_arxml(str(model_root), str(output_path))

        assert output_path.read_bytes() == first
        assert not (tmp_path / "out" / "model_old.arxml").exists()

    def test_a_rejected_model_moves_nothing_and_writes_nothing(self, tmp_path):
        """The move follows the in-memory build, so a refusal leaves the previous file where it was."""
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "interfaces" / "IBad.yaml",
            {
                "name": "IBad",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "no_such_type"}],
            },
        )
        output_path = tmp_path / "out.arxml"
        output_path.write_bytes(b"<previous build/>\n")

        with pytest.raises(ValueError, match="not declared"):
            generate_arxml(str(model_root), str(output_path))

        assert output_path.read_bytes() == b"<previous build/>\n"
        assert not (tmp_path / "out_old.arxml").exists()

    def test_generate_arxml_still_takes_exactly_two_positional_parameters(self):
        """The signature is pinned, not the internals: exactly two positional-or-keyword parameters.

        The target AUTOSAR version is keyword-*only* and defaulted, so
        `generate_arxml(model_root, output_path)` stays the whole positional surface."""
        parameters = inspect.signature(generate_arxml).parameters
        positional = [p for p in parameters.values() if p.kind is not inspect.Parameter.KEYWORD_ONLY]
        assert [p.name for p in positional] == ["model_root", "output_path"]
        assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in positional)
        assert all(p.default is inspect.Parameter.empty for p in positional)
        # Anything beyond the two is keyword-only and optional.
        keyword_only = [p for p in parameters.values() if p.kind is inspect.Parameter.KEYWORD_ONLY]
        assert all(p.default is not inspect.Parameter.empty for p in keyword_only)

    def test_generate_arxml_defaults_to_4_2_2_when_no_version_is_given(self, tmp_path):
        """The two-positional call produces 4.2.2 output."""
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"

        generate_arxml(str(model_root), str(output_path))

        assert 'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd"' in output_path.read_text(
            encoding="utf-8"
        )

    def test_generate_arxml_emits_the_requested_version(self, tmp_path):
        """The version is a keyword of the boundary itself, so CLI, MCP and component export reach it alike."""
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"

        generate_arxml(str(model_root), str(output_path), version="4.3.0")

        assert 'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd"' in output_path.read_text(
            encoding="utf-8"
        )

    def test_generate_arxml_rejects_an_unsupported_version_before_writing(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        output_path = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match="Unsupported AUTOSAR version"):
            generate_arxml(str(model_root), str(output_path), version="4.5.0")

        assert not output_path.exists()

    @pytest.mark.parametrize(
        ("layer", "filename", "content", "match"),
        [
            (
                "data_types/implementation",
                "Chain.yaml",
                {"name": "Chain", "kind": "value_type", "base": "Link"},
                r"base type 'Link' is not declared in data_types/base/",
            ),
            (
                "interfaces",
                "IRaw.yaml",
                {
                    "name": "IRaw",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "raw", "implementation_type": "float32_base"}],
                },
                r"Data type 'float32_base' not declared in data_types/implementation/",
            ),
            (
                "swc",
                "ServiceSWC.yaml",
                {
                    "name": "ServiceSWC",
                    "type": "service",
                    "ports": [],
                    "runnables": [],
                    "handle_termination_and_restart": "no_support",
                },
                "only 'application' SWCs are generated",
            ),
        ],
    )
    def test_generate_arxml_raises_the_same_documented_value_errors(self, tmp_path, layer, filename, content, match):
        """The exceptions and their wording are part of the boundary's contract, not of its implementation.

        A value type's base resolves in the base type layer alone and every other
        role in ``implementation_types``, so both models are refused as naming a
        type not declared where it is looked up.
        """
        model_root = tmp_path / "model"
        _write_model(model_root)
        write_model_element(model_root, {"name": "Link", "kind": "value_type", "base": "float32_base"})
        _write_yaml(model_root / layer / filename, content)

        with pytest.raises(ValueError, match=match):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_generate_arxml_leaves_no_output_file_when_it_raises(self, tmp_path):
        """A failed generation writes nothing: validation runs before serialization, not during it."""
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "interfaces" / "IBad.yaml",
            {
                "name": "IBad",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "no_such_type"}],
            },
        )
        output_path = tmp_path / "out.arxml"

        with pytest.raises(ValueError):
            generate_arxml(str(model_root), str(output_path))

        assert not output_path.exists()


# ---------------------------------------------------------------------------
# Boundary 4: semantic diff/impact queries (differ.diff_snapshots + impact.impact_of)
# ---------------------------------------------------------------------------


class TestSemanticDiffImpactQueryBoundary:
    """``diff_snapshots`` and ``impact_of``: pure queries over already-loaded ``ModelSnapshot``s.

    ``diff_snapshots(old, new) -> ModelDiff`` and ``impact_of(snapshot, name) -> dict[str, list[Dependent]]`` do no I/O
    of their own."""

    def test_diff_snapshots_reports_added_removed_changed(self, tmp_path):
        old_root = tmp_path / "old"
        new_root = tmp_path / "new"
        _write_model(old_root)
        _write_model(new_root)

        _write_yaml(
            new_root / "swc" / "extra" / "ExtraSWC.yaml",
            {"name": "ExtraSWC", "type": "application", "ports": [], "runnables": []},
        )
        (new_root / "requirements" / "REQ-1.yaml").unlink()
        task_path = new_root / "tasks" / "Task10ms.yaml"
        task_data = yaml.safe_load(task_path.read_text(encoding="utf-8"))
        task_data["period"] = 0.02
        _write_yaml(task_path, task_data)

        old_snapshot = load_snapshot(str(old_root), rev=None)
        new_snapshot = load_snapshot(str(new_root), rev=None)

        diff = diff_snapshots(old_snapshot, new_snapshot)

        assert [s.name for s in diff.swcs.added] == ["ExtraSWC"]
        assert diff.swcs.removed == []
        assert [r.id for r in diff.requirements.removed] == ["REQ-1"]
        assert len(diff.tasks.changed) == 1
        assert diff.tasks.changed[0].key == "Task10ms"
        assert ("period", 0.01, 0.02) in diff.tasks.changed[0].changed_fields
        assert diff.interfaces.is_empty

    def test_impact_of_returns_transitive_dependents(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        snapshot = load_snapshot(str(model_root), rev=None)

        result = impact_of(snapshot, "float32")

        assert "implementation_type" in result
        dependent_names = {d.ref.name for d in result["implementation_type"]}
        # float32 -> ISpeed (data element type) -> both SWCs (via ports) ->
        # Task10ms (via SpeedSensorSWC's mapped runnable), the connector (both
        # ends) and `Root`, which places both components as members: transitivity
        # across hops. `REQ-1` is absent: SpeedSensorSWC satisfies it, so the
        # component depends on the requirement and not the reverse.
        assert dependent_names == {
            "ISpeed",
            "SpeedSensorSWC",
            "SpeedCtrlSWC",
            "Task10ms",
            "SpeedSensor_to_SpeedCtrl",
            "Root",
        }

    def test_impact_of_a_requirement_lists_the_element_satisfying_it(self, tmp_path):
        """The requirement is the depended-upon end of the trace link."""
        model_root = tmp_path / "model"
        _write_model(model_root)
        snapshot = load_snapshot(str(model_root), rev=None)

        result = impact_of(snapshot, "REQ-1")

        assert {d.ref.name for dependents in result.values() for d in dependents} >= {"SpeedSensorSWC"}

    def test_impact_of_unknown_name_returns_empty_dict(self, tmp_path):
        model_root = tmp_path / "model"
        _write_model(model_root)
        snapshot = load_snapshot(str(model_root), rev=None)

        assert impact_of(snapshot, "DoesNotExist") == {}
