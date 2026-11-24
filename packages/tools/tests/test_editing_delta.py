"""The ``ModelDiff`` -> ``deltaEntry[]`` projection.

Two things are pinned here, beside the vector suite:

1. **The normative mapping table**, shape by shape: each row gets a case that produces
   exactly that shape and nothing beside it.
2. **The four rules a naive projection gets wrong**, as statements about a class rather
   than the one instance a vector states: a ``whole_value`` member changed and held
   twice on both sides, a reorder that is not pure, a map key added rather than
   changed, a nested member three levels down.

Everything runs against a real project on disk and two real snapshots of it; building
a ``ModelDiff`` by hand would be building a second differ to test the first against.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import AGENT_TASK_BENCH, REPO_ROOT, model_element_storage_name, write_model_element
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.delta import elided_fields, project_delta, summarize_removed_elements, summarize_response
from swcraft_tools.editing.protocol import DeltaEntry
from swcraft_tools.editing.read import placement_of, publish_element, summarize, summarize_published
from swcraft_tools.snapshot import load_snapshot

# Fixtures and helpers


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real, valid project on disk — the vector harness's base model, unseeded."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _delta(project_root: Path, edit: Callable[[Path], None]) -> list[dict[str, Any]]:
    """The delta *edit* produces, as the JSON the response would carry.

    The baseline snapshot is taken before the edit and the staged one after; the
    projection cannot tell a shadow's model root from the project's, and must not.
    """
    model_root = project_root / "model"
    baseline = load_snapshot(str(model_root))
    edit(model_root)
    staged = load_snapshot(str(model_root))
    return [
        entry.to_json() for entry in project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
    ]


def _read(model_root: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _write(model_root: Path, relative: str, document: dict[str, Any]) -> None:
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _edit(model_root: Path, relative: str, change: Callable[[dict[str, Any]], None]) -> None:
    document = _read(model_root, relative)
    change(document)
    _write(model_root, relative, document)


def _runnable(model_root: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Apply *change* to ``ExampleSWC``'s one runnable."""
    _edit(model_root, "swc/example/ExampleSWC.yaml", lambda document: change(document["runnables"][0]))


_ALIAS = {
    "name": "VehicleSpeed_kph",
    "kind": "value_type",
    "base": "float32",
    "unit": "kph",
    "value_min": 0.0,
    "value_max": 300.0,
    "description": "Vehicle speed in kilometres per hour",
}

_EVENT_ADDRESS = [
    {"field": "runnables"},
    {"member": "ExampleRun"},
    {"field": "events"},
]

#: An application primitive with all three physics fields declared: ``unit`` is a
#: plain scalar, ``conversion`` and ``range`` are singular member kinds.
_APPLICATION_TYPE = {
    "name": "Speed_Kph",
    "kind": "application_type",
    "unit": "kph",
    "conversion": {"factor": 0.1, "offset": 0.0},
    "range": {"min": 0.0, "max": 300.0},
    "description": "Vehicle speed, physical view",
}

#: The set that pairs it with its implementation type, plus a second pair so that an
#: added and a removed map are not the same edit as an emptied collection.
_MAPPING_SET = {
    "name": "PhysicalMapping",
    "maps": [{"application": "Speed_Kph", "implementation": "float32"}],
    "description": "The implementation types that store the physical ones",
}


def _write_application_type(model_root: Path) -> None:
    """Put the physical view and the set that pairs it into the baseline."""
    _write(model_root, model_element_storage_name("Speed_Kph", kind="application_type"), _APPLICATION_TYPE)


def _write_mapping_set(model_root: Path) -> None:
    _write_application_type(model_root)
    _write(model_root, model_element_storage_name("PhysicalMapping", layer="mapping_sets"), _MAPPING_SET)


def _addresses(delta: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Every entry as ``(operation, address)`` — the contract-bearing half."""
    return [(entry["operation"], entry["address"]) for entry in delta]


# 1. The mapping table, shape by shape


class TestElementShapes:
    """``LayerDiff.added`` / ``.removed``: one entry, the element carried whole."""

    def test_an_added_element_is_one_create_carrying_the_whole_element(self, project: Path) -> None:
        delta = _delta(
            project,
            lambda root: _write(root, model_element_storage_name("VehicleSpeed_kph", kind="value_type"), _ALIAS),
        )
        assert len(delta) == 1
        assert delta[0]["operation"] == "create"
        assert delta[0]["address"] == {"layer": "implementation_types", "element": "VehicleSpeed_kph"}
        # The published element carries every declared field, the defaults
        # among them, though the file names none of them.
        assert delta[0]["after"] == {**_ALIAS, "compu_method_ref": None, "package": None}
        assert "before" not in delta[0]

    def test_a_removed_element_is_one_remove_carrying_the_whole_element(self, project: Path) -> None:
        delta = _delta(project, lambda root: (root / "hardware" / "RAM0.yaml").unlink())
        assert _addresses(delta) == [("remove", {"layer": "hardware", "element": "RAM0"})]
        assert delta[0]["before"]["name"] == "RAM0"
        assert delta[0]["before"]["size"] == 262144
        assert "after" not in delta[0]

    def test_a_requirement_is_addressed_by_its_declared_identity_field(self, project: Path) -> None:
        # `requirements` is keyed by `id`: the identity field is read off the
        # registry rather than assumed.
        delta = _delta(project, lambda root: (root / "requirements" / "REQ_EXAMPLE_001.yaml").unlink())
        assert _addresses(delta) == [("remove", {"layer": "requirements", "element": "REQ_EXAMPLE_001"})]


class TestPlacementMetadata:
    """A create's ``after`` and a remove's ``before`` answer ``group``.

    Placement is not model content (``SWComponent`` has no ``group`` field), so without
    this a create's stated group could not be reported back.
    """

    _SWC = {
        "name": "SpeedSensorSWC",
        "type": "application",
        "description": "Reads the wheel speed sensors",
        "memory": {"RAM0": 1024},
    }

    def test_a_created_element_reports_the_group_it_was_placed_in(self, project: Path) -> None:
        delta = _delta(project, lambda root: _write(root, "swc/sensors/SpeedSensorSWC.yaml", self._SWC))
        assert delta[0]["after"]["group"] == "sensors"

    def test_a_removed_element_reports_the_group_it_was_taken_from(self, project: Path) -> None:
        delta = _delta(project, lambda root: (root / "swc" / "example" / "ExampleSWC.yaml").unlink())
        removed = next(entry for entry in delta if entry["address"] == {"layer": "swc", "element": "ExampleSWC"})
        assert removed["before"]["group"] == "example"


class TestFieldShapes:
    """A changed scalar or object field: one ``update`` at the field's address."""

    def test_a_changed_scalar_field_is_one_update_at_the_field(self, project: Path) -> None:
        delta = _delta(project, lambda root: _edit(root, "tasks/Task_10ms.yaml", lambda doc: doc.update(priority=6)))
        assert _addresses(delta) == [
            ("update", {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]})
        ]
        assert (delta[0]["before"], delta[0]["after"]) == (5, 6)

    def test_a_changed_object_field_is_addressed_at_the_component_that_moved(self, project: Path) -> None:
        # The endpoint object is descended into rather than carried whole: the
        # finest granularity the address form expresses.
        delta = _delta(
            project,
            lambda root: _edit(
                root, "connectors/Example_to_Consumer.yaml", lambda doc: doc["from"].update(port="pAlt")
            ),
        )
        assert _addresses(delta) == [
            (
                "update",
                {
                    "layer": "connectors",
                    "element": "Example_to_Consumer",
                    "path": [{"field": "from"}, {"field": "port"}],
                },
            )
        ]
        assert (delta[0]["before"], delta[0]["after"]) == ("pOut", "pAlt")

    def test_two_components_of_one_object_field_are_two_entries(self, project: Path) -> None:
        delta = _delta(
            project,
            lambda root: _edit(
                root,
                "connectors/Example_to_Consumer.yaml",
                lambda doc: doc["from"].update(swc="OtherSWC", port="pAlt"),
            ),
        )
        assert [entry["address"]["path"][-1] for entry in delta] == [{"field": "swc"}, {"field": "port"}]


class TestListMemberShapes:
    """An added or removed list member is one entry at the **member's** address."""

    _PORT = {"name": "pExtra", "direction": "provide", "interface": "IExample", "description": "another port"}

    def test_an_added_member_is_one_create_at_the_member(self, project: Path) -> None:
        delta = _delta(
            project,
            lambda root: _edit(root, "swc/example/ExampleSWC.yaml", lambda doc: doc["ports"].append(self._PORT)),
        )
        assert _addresses(delta) == [
            (
                "create",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "ports"}, {"member": "pExtra"}],
                },
            )
        ]
        # The staged member as the differ sees it: every declared field, a default
        # included. Not compared against `_PORT`, which carries only the keys a human wrote.
        assert delta[0]["after"] == {
            **self._PORT,
            "observed_interface": None,
            "measures": {},
            "com_specs": [],
            "satisfies": [],
        }

    def test_a_removed_member_is_one_remove_at_the_member(self, project: Path) -> None:
        delta = _delta(
            project,
            lambda root: _edit(root, "swc/example/ExampleSWC.yaml", lambda doc: doc["ports"].clear()),
        )
        assert _addresses(delta) == [
            (
                "remove",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "ports"}, {"member": "pOut"}],
                },
            )
        ]
        assert delta[0]["before"]["name"] == "pOut"

    def test_a_composite_identity_is_an_object_member_step(self, project: Path) -> None:
        # `Task.runnables` members are `{swc, runnable}` references, so the
        # address spells that identity as an object.
        delta = _delta(
            project,
            lambda root: _edit(root, "tasks/Task_10ms.yaml", lambda doc: doc["runnables"].clear()),
        )
        assert _addresses(delta) == [
            (
                "remove",
                {
                    "layer": "tasks",
                    "element": "Task_10ms",
                    "path": [
                        {"field": "runnables"},
                        # Emitted with all three keys, the absent selector `None`.
                        {"member": {"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}},
                    ],
                },
            )
        ]

    def test_a_rewrite_from_swc_to_instance_is_a_remove_and_a_create(self, project: Path) -> None:
        """The same component named the other way is another member: generic matching never resolves an occurrence."""

        def rewrite(document: dict[str, Any]) -> None:
            document["runnables"] = [{"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}]

        delta = _delta(project, lambda root: _edit(root, "tasks/Task_10ms.yaml", rewrite))
        assert sorted(_addresses(delta), key=lambda entry: entry[0]) == [
            (
                "create",
                {
                    "layer": "tasks",
                    "element": "Task_10ms",
                    "path": [
                        {"field": "runnables"},
                        {"member": {"swc": None, "instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}},
                    ],
                },
            ),
            (
                "remove",
                {
                    "layer": "tasks",
                    "element": "Task_10ms",
                    "path": [
                        {"field": "runnables"},
                        {"member": {"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}},
                    ],
                },
            ),
        ]


# 2. Rule 1: a changed `whole_value` member is a remove plus a create


class TestWholeValueMembersAreNeverUpdated:
    """A changed ``whole_value`` member is a remove plus a create, never an update.

    Such a member has no addressable interior, so an ``update`` could not name what changed.
    """

    @staticmethod
    def _events(*periods: float) -> Callable[[Path], None]:
        def edit(model_root: Path) -> None:
            _runnable(
                model_root,
                lambda runnable: runnable.update(events=[{"type": "timing", "period": p} for p in periods]),
            )

        return edit

    def test_a_retimed_event_is_a_remove_plus_a_create(self, project: Path) -> None:
        delta = _delta(project, self._events(0.02))
        assert [entry["operation"] for entry in delta] == ["remove", "create"]
        # The member step is the event's identity, which leaves out a field at its
        # default; the value is the full one the model holds.
        assert delta[0]["address"]["path"] == [*_EVENT_ADDRESS, {"member": {"type": "timing", "period": 0.01}}]
        assert delta[1]["address"]["path"] == [*_EVENT_ADDRESS, {"member": {"type": "timing", "period": 0.02}}]
        assert delta[0]["before"] == {"type": "timing", "period": 0.01, "disabled_modes": [], "name": None}
        assert delta[1]["after"] == {"type": "timing", "period": 0.02, "disabled_modes": [], "name": None}

    def test_no_entry_is_an_update(self, project: Path) -> None:
        assert "update" not in {entry["operation"] for entry in _delta(project, self._events(0.02))}

    def test_a_value_held_twice_on_both_sides_produces_nothing(self, project: Path) -> None:
        # The multiset half of the rule: counting, not keying. Two identical
        # occurrences on both sides cancel.
        _write_two = self._events(0.01, 0.01)
        _write_two(project / "model")
        assert _delta(project, self._events(0.01, 0.01)) == []

    def test_clearing_one_of_two_occurrences_is_one_remove(self, project: Path) -> None:
        self._events(0.01, 0.01)(project / "model")
        delta = _delta(project, self._events(0.01))
        assert [entry["operation"] for entry in delta] == ["remove"]
        assert delta[0]["address"]["path"][-1] == {"member": {"type": "timing", "period": 0.01}}

    def test_clearing_both_occurrences_is_two_entries_at_one_address(self, project: Path) -> None:
        self._events(0.01, 0.01, 0.02)(project / "model")
        delta = _delta(project, self._events(0.02))
        assert [entry["operation"] for entry in delta] == ["remove", "remove"]
        assert delta[0] == delta[1], "one entry per occurrence, each at the member's own address"

    def test_a_second_occurrence_of_an_existing_value_is_one_create(self, project: Path) -> None:
        delta = _delta(project, self._events(0.01, 0.01))
        assert [entry["operation"] for entry in delta] == ["create"]
        assert delta[0]["after"] == {"type": "timing", "period": 0.01, "disabled_modes": [], "name": None}

    def test_a_whole_value_string_member_is_the_same_shape(self, project: Path) -> None:
        # `SWComponent.satisfies` is a `whole_value` list of strings, so confirming
        # a link against a new version is a remove plus a create.
        delta = _delta(
            project,
            lambda root: _edit(
                root, "swc/example/ExampleSWC.yaml", lambda doc: doc["satisfies"].__setitem__(0, "REQ_EXAMPLE_001@2")
            ),
        )
        assert _addresses(delta) == [
            (
                "remove",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
                },
            ),
            (
                "create",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@2"}],
                },
            ),
        ]


# 3. Rule 2: a map's three verbs


class TestMapEntries:
    """A changed entry is an ``update``, an added key a ``create``, a removed key a ``remove``."""

    @staticmethod
    def _memory(**budgets: int) -> Callable[[Path], None]:
        def edit(model_root: Path) -> None:
            _edit(model_root, "swc/example/ExampleSWC.yaml", lambda doc: doc.update(memory=dict(budgets)))

        return edit

    _ADDRESS = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]}

    def test_a_changed_entry_is_one_update_at_the_key(self, project: Path) -> None:
        delta = _delta(project, self._memory(RAM0=4096))
        assert _addresses(delta) == [("update", self._ADDRESS)]
        assert (delta[0]["before"], delta[0]["after"]) == (2048, 4096)

    def test_a_removed_key_is_a_remove_carrying_the_entry_value(self, project: Path) -> None:
        delta = _delta(project, self._memory())
        assert _addresses(delta) == [("remove", self._ADDRESS)]
        assert delta[0]["before"] == 2048
        assert "after" not in delta[0]

    def test_an_added_key_is_a_create_carrying_the_entry_value(self, project: Path) -> None:
        delta = _delta(project, self._memory(RAM0=2048, FLASH=512))
        assert _addresses(delta) == [
            (
                "create",
                {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "FLASH"}]},
            )
        ]
        assert delta[0]["after"] == 512
        assert "before" not in delta[0]

    def test_a_changed_entry_is_never_read_as_a_remove_plus_a_create(self, project: Path) -> None:
        # A map key is unique by construction, so an entry has an identity a new
        # value can be given.
        assert {entry["operation"] for entry in _delta(project, self._memory(RAM0=4096))} == {"update"}


# 4. Rule 3: a changed nested member reports what changed inside it


class TestNestedMembers:
    def test_a_field_three_levels_down_is_addressed_at_that_field(self, project: Path) -> None:
        delta = _delta(project, lambda root: _runnable(root, lambda run: run.update(min_start_interval=0.02)))
        assert _addresses(delta) == [
            (
                "update",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "runnables"}, {"member": "ExampleRun"}, {"field": "min_start_interval"}],
                },
            )
        ]

    def test_no_entry_is_produced_for_the_member_or_for_the_element(self, project: Path) -> None:
        # An element-granular projection would leave every scope entry below the
        # element level unenforceable.
        delta = _delta(project, lambda root: _runnable(root, lambda run: run.update(min_start_interval=0.02)))
        paths = [len(entry["address"].get("path", ())) for entry in delta]
        assert paths == [3], "one entry at the leaf, and none at the runnable or at the component"

    def test_two_changes_inside_one_member_are_two_entries(self, project: Path) -> None:
        def edit(model_root: Path) -> None:
            _runnable(
                model_root,
                lambda run: run.update(min_start_interval=0.02, events=[{"type": "timing", "period": 0.02}]),
            )

        delta = _delta(project, edit)
        assert [entry["operation"] for entry in delta] == ["update", "remove", "create"]

    def test_a_singular_member_kind_extends_the_address_by_a_field_step(self, project: Path) -> None:
        """A singular member kind (``conversion``, ``range``) adds no step vocabulary: plain field steps.

        ``conversion.factor`` is two field steps and needs no case in ``differ.py``.
        """

        def edit(model_root: Path) -> None:
            _write(
                model_root,
                model_element_storage_name("Speed_Kph", kind="application_type"),
                {**_APPLICATION_TYPE, "conversion": {"factor": 0.2}},
            )

        _write_application_type(project / "model")
        delta = _delta(project, edit)
        assert _addresses(delta) == [
            (
                "update",
                {
                    "layer": "application_types",
                    "element": "Speed_Kph",
                    "path": [{"field": "conversion"}, {"field": "factor"}],
                },
            )
        ]
        assert (delta[0]["before"], delta[0]["after"]) == (0.1, 0.2)
        assert delta[0]["summary"] == "application_types/Speed_Kph: ~ conversion.factor 0.1 -> 0.2"

    def test_a_singular_member_kind_declared_or_cleared_is_one_entry_at_its_own_field(self, project: Path) -> None:
        """A singular member kind appearing or disappearing whole is one entry at its own field.

        So a ``range`` cleared and one whose two bounds both moved are different deltas.
        """

        def edit(model_root: Path) -> None:
            document = {**_APPLICATION_TYPE}
            del document["range"]
            _write(model_root, model_element_storage_name("Speed_Kph", kind="application_type"), document)

        _write_application_type(project / "model")
        delta = _delta(project, edit)
        assert _addresses(delta) == [
            ("update", {"layer": "application_types", "element": "Speed_Kph", "path": [{"field": "range"}]})
        ]
        assert (delta[0]["before"], delta[0]["after"]) == ({"min": 0.0, "max": 300.0}, None)

    def test_a_map_is_addressed_by_a_member_step_keyed_on_the_application_type(self, project: Path) -> None:
        """A ``mapping_sets`` map is a list member keyed on the application type, not a ``{"key": ...}`` entry.

        Its entries take the ``{"member": ...}`` step, and the identity is the application
        side, the side the layer's element-local uniqueness rule is keyed on.
        """

        def edit(model_root: Path) -> None:
            document = {**_MAPPING_SET}
            document["maps"] = [*document["maps"], {"application": "EngineSpeed_Rpm", "implementation": "uint16"}]
            _write(model_root, model_element_storage_name("PhysicalMapping", layer="mapping_sets"), document)
            write_model_element(model_root, {"name": "EngineSpeed_Rpm", "kind": "application_type", "unit": "rpm"})

        _write_mapping_set(project / "model")
        delta = _delta(project, edit)
        # The delta lists layers in registry order, which puts the application
        # type's create first.
        assert _addresses(delta) == [
            ("create", {"layer": "application_types", "element": "EngineSpeed_Rpm"}),
            (
                "create",
                {
                    "layer": "mapping_sets",
                    "element": "PhysicalMapping",
                    "path": [{"field": "maps"}, {"member": "EngineSpeed_Rpm"}],
                },
            ),
        ]
        assert delta[1]["after"] == {"application": "EngineSpeed_Rpm", "implementation": "uint16"}
        assert delta[1]["summary"] == "mapping_sets/PhysicalMapping: + maps[EngineSpeed_Rpm]"

    def test_a_re_pointed_implementation_is_an_update_beneath_the_member_step(self, project: Path) -> None:
        """Three steps, the last the field: the map has an identity, so it is *changed*."""

        def edit(model_root: Path) -> None:
            _write(
                model_root,
                model_element_storage_name("PhysicalMapping", layer="mapping_sets"),
                {**_MAPPING_SET, "maps": [{"application": "Speed_Kph", "implementation": "float64"}]},
            )

        _write_mapping_set(project / "model")
        delta = _delta(project, edit)
        assert _addresses(delta) == [
            (
                "update",
                {
                    "layer": "mapping_sets",
                    "element": "PhysicalMapping",
                    "path": [{"field": "maps"}, {"member": "Speed_Kph"}, {"field": "implementation"}],
                },
            )
        ]
        assert (delta[0]["before"], delta[0]["after"]) == ("float32", "float64")

    def test_the_components_reference_to_a_set_is_a_whole_value_member(self, project: Path) -> None:
        """``swc.data_type_mappings`` is a list of identities, ``swc.satisfies``' shape."""

        def edit(model_root: Path) -> None:
            _edit(
                model_root,
                "swc/example/ExampleSWC.yaml",
                lambda doc: doc.update(data_type_mappings=["PhysicalMapping"]),
            )

        _write_mapping_set(project / "model")
        delta = _delta(project, edit)
        assert _addresses(delta) == [
            (
                "create",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "data_type_mappings"}, {"member": "PhysicalMapping"}],
                },
            )
        ]
        assert delta[0]["after"] == "PhysicalMapping"

    def test_a_member_of_a_member_extends_the_address_by_two_steps(self, project: Path) -> None:
        # `interfaces -> operations -> arguments`: the model's other two-level nesting.
        def edit(model_root: Path) -> None:
            document = _read(model_root, "interfaces/IDiagnosticService.yaml")
            document["operations"][0]["arguments"][0]["direction"] = "out"
            _write(model_root, "interfaces/IDiagnosticService.yaml", document)

        delta = _delta(project, edit)
        assert len(delta) == 1
        assert [step for step in delta[0]["address"]["path"]][:4] == [
            {"field": "operations"},
            {"member": "ReadDtc"},
            {"field": "arguments"},
            {"member": "dtc_id"},
        ]


# 5. Rule 4: order is a signal about a collection, never about a member


class TestReordering:
    """Reordering over ``ExampleSWC.satisfies``, seeded with a second link before the baseline.

    The delta validates nothing, so ``REQ_OTHER`` needs no file of its own.
    """

    _PATH = "swc/example/ExampleSWC.yaml"

    @pytest.fixture
    def project(self, project: Path) -> Path:
        _edit(project / "model", self._PATH, lambda doc: doc.update(satisfies=["REQ_EXAMPLE_001@1", "REQ_OTHER@1"]))
        return project

    @classmethod
    def _links(cls, *members: str) -> Callable[[Path], None]:
        def edit(model_root: Path) -> None:
            _edit(model_root, cls._PATH, lambda doc: doc.update(satisfies=list(members)))

        return edit

    _FIELD_ADDRESS = {
        "layer": "swc",
        "element": "ExampleSWC",
        "path": [{"field": "satisfies"}],
    }

    def test_a_pure_reordering_is_one_update_at_the_collections_field(self, project: Path) -> None:
        delta = _delta(project, self._links("REQ_OTHER@1", "REQ_EXAMPLE_001@1"))
        assert _addresses(delta) == [("update", self._FIELD_ADDRESS)]

    def test_a_pure_reordering_carries_both_orders(self, project: Path) -> None:
        delta = _delta(project, self._links("REQ_OTHER@1", "REQ_EXAMPLE_001@1"))
        assert delta[0]["before"] == ["REQ_EXAMPLE_001@1", "REQ_OTHER@1"]
        assert delta[0]["after"] == ["REQ_OTHER@1", "REQ_EXAMPLE_001@1"]

    def test_a_reordering_beside_an_addition_reports_both(self, project: Path) -> None:
        # The addition's own entry does not describe the reordering, or a reviewer
        # shown "one link added" would approve a permutation nobody stated. The
        # collection-level entry comes first.
        delta = _delta(project, self._links("REQ_NEW@1", "REQ_OTHER@1", "REQ_EXAMPLE_001@1"))
        assert _addresses(delta) == [
            ("update", self._FIELD_ADDRESS),
            (
                "create",
                {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "satisfies"}, {"member": "REQ_NEW@1"}],
                },
            ),
        ]

    def test_a_reordering_beside_an_addition_carries_both_orders(self, project: Path) -> None:
        # The ordering change reaches the file, so the reviewed delta states it;
        # neither swapped member is the added one.
        delta = _delta(project, self._links("REQ_NEW@1", "REQ_OTHER@1", "REQ_EXAMPLE_001@1"))
        assert delta[0]["before"] == ["REQ_EXAMPLE_001@1", "REQ_OTHER@1"]
        assert delta[0]["after"] == ["REQ_NEW@1", "REQ_OTHER@1", "REQ_EXAMPLE_001@1"]


# 6. Properties of every entry


class TestEveryEntry:
    _EDITS: tuple[Callable[[Path], None], ...] = (
        lambda root: _write(root, model_element_storage_name("VehicleSpeed_kph", kind="value_type"), _ALIAS),
        lambda root: (root / "hardware" / "RAM0.yaml").unlink(),
        lambda root: _runnable(root, lambda run: run.update(min_start_interval=0.02)),
        lambda root: _edit(root, "swc/example/ExampleSWC.yaml", lambda doc: doc.update(memory={"FLASH": 8})),
        lambda root: _edit(root, "connectors/Example_to_Consumer.yaml", lambda doc: doc["from"].update(port="pAlt")),
    )

    @pytest.fixture(params=range(len(_EDITS)), ids=["create", "remove", "nested-update", "map", "object-field"])
    def delta(self, request: pytest.FixtureRequest, project: Path) -> list[dict[str, Any]]:
        return _delta(project, self._EDITS[request.param])

    def test_every_address_is_an_element_address(self, delta: list[dict[str, Any]]) -> None:
        # The projection parses every address through the address model, so a step
        # it would refuse cannot reach a delta entry.
        assert delta
        for entry in delta:
            assert Address.from_json(entry["address"]).is_element_address

    def test_every_entry_carries_a_summary(self, delta: list[dict[str, Any]]) -> None:
        # Prose, never parsed: `staged` exists to be reviewed.
        for entry in delta:
            assert isinstance(entry["summary"], str) and entry["summary"]

    def test_a_create_carries_after_only_and_a_remove_before_only(self, delta: list[dict[str, Any]]) -> None:
        for entry in delta:
            if entry["operation"] == "create":
                assert "after" in entry and "before" not in entry
            elif entry["operation"] == "remove":
                assert "before" in entry and "after" not in entry
            else:
                assert "before" in entry and "after" in entry


class TestNoChange:
    def test_an_empty_diff_projects_to_an_empty_delta(self, project: Path) -> None:
        # A no-op change set is not an error; it is a delta with nothing in it.
        assert _delta(project, lambda root: None) == []

    def test_a_rewritten_but_unchanged_file_produces_nothing(self, project: Path) -> None:
        # The delta is over model content, never bytes: a whole-file rewrite that
        # changes no value must not appear in the review surface.
        def rewrite(model_root: Path) -> None:
            _write(model_root, "tasks/Task_10ms.yaml", _read(model_root, "tasks/Task_10ms.yaml"))

        assert _delta(project, rewrite) == []


class TestAValueTooWideToRenderIsDescribedInTheSummary:
    """A value too wide to print is described in the summary, through ``rendering.render_model_scalar``.

    Guards a defect: a hand-written ``0x…`` ``init`` loads as an integer whose ``repr``
    raises, and a bare ``repr`` made ``derive`` answer a ``ValueError`` instead of a
    staged change set.

    **Does not reach:** the delta's ``before``/``after`` values, which
    ``editing/protocol.py``'s serializer still refuses deliberately: "too wide to render"
    where a number belongs would be a false model.
    """

    #: 4 000 hexadecimal digits, written as text: ``yaml.safe_dump`` would raise on the
    #: integer, and a hand-authored file reaches a snapshot without any reader guard.
    WIDE = "0x" + "f" * 4000
    WIDE_BITS = 16000

    _INTERFACE = "interfaces/IExample.yaml"

    def _with_init(self, model_root: Path, literal: str) -> None:
        (model_root / self._INTERFACE).write_text(
            "name: IExample\n"
            "kind: sender_receiver\n"
            "data_elements:\n"
            "  - name: value\n"
            "    implementation_type: float32\n"
            f"    init: {literal}\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_the_baseline_really_carries_the_unprintable_value(self, project: Path) -> None:
        """The premise: the file loads as an integer whose own ``repr`` raises."""
        model_root = project / "model"
        self._with_init(model_root, self.WIDE)

        loaded = _read(model_root, self._INTERFACE)["data_elements"][0]["init"]

        assert isinstance(loaded, int)
        assert loaded.bit_length() == self.WIDE_BITS
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(loaded)

    def test_the_summary_describes_it_instead_of_raising(self, project: Path) -> None:
        model_root = project / "model"
        self._with_init(model_root, self.WIDE)

        delta = _delta(project, lambda root: self._with_init(root, "1"))

        summaries = [entry["summary"] for entry in delta if entry["operation"] == "update"]
        assert any(f"<an integer of {self.WIDE_BITS} bits, too wide to render> -> 1" in line for line in summaries)

    def test_an_ordinary_value_is_still_the_number(self, project: Path) -> None:
        """The control: an ordinary value is still the number, not the placeholder."""
        model_root = project / "model"
        self._with_init(model_root, "7")

        delta = _delta(project, lambda root: self._with_init(root, "1"))

        summaries = [entry["summary"] for entry in delta if entry["operation"] == "update"]
        assert any("7 -> 1" in line for line in summaries)


class TestTheElisionAtTheEdge:
    """A whole-element ``remove`` leaves the engine with its ``before`` summarised.

    ``summarize_removed_elements`` applies where the delta leaves the engine (the
    ``change_set`` response and the review text), on a copy; ``project_delta`` still
    carries the element whole for the gates and the safety guard.
    """

    @staticmethod
    def _entries(project_root: Path, edit: Callable[[Path], None]) -> list[DeltaEntry]:
        model_root = project_root / "model"
        baseline = load_snapshot(str(model_root))
        edit(model_root)
        staged = load_snapshot(str(model_root))
        return project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)

    def test_a_removed_component_is_its_summary_with_the_flag(self, project: Path) -> None:
        entries = self._entries(project, lambda root: (root / "swc" / "example" / "ExampleSWC.yaml").unlink())
        whole = entries[0].to_json()
        (summarised,) = [entry.to_json() for entry in summarize_removed_elements(entries)]

        assert summarised["before_form"] == "summary"
        assert summarised["before"] == {
            "name": "ExampleSWC",
            "group": "example",
            "ports": len(whole["before"]["ports"]),
            "runnables": len(whole["before"]["runnables"]),
            "memory": len(whole["before"]["memory"]),
            "data_type_mappings": len(whole["before"]["data_type_mappings"]),
            "satisfies": len(whole["before"]["satisfies"]),
            "variables": len(whole["before"]["variables"]),
            "exclusive_areas": len(whole["before"]["exclusive_areas"]),
        }
        assert summarised["before"]["ports"] == 1
        # Everything but `before` and the flag is the engine's entry unchanged.
        assert {key: value for key, value in summarised.items() if key not in {"before", "before_form"}} == {
            key: value for key, value in whole.items() if key != "before"
        }

    def test_the_summary_is_the_one_read_answers_for_the_element(self, project: Path) -> None:
        """The same projection ``read`` serves for ``form: "summary"`` at the element's address."""
        read = fixtures.dispatch(
            {"operation": "read", "address": {"layer": "swc", "element": "ExampleSWC"}, "form": "summary"},
            project=project,
            approved_scope=None,
        )
        entries = self._entries(project, lambda root: (root / "swc" / "example" / "ExampleSWC.yaml").unlink())
        (summarised,) = summarize_removed_elements(entries)

        assert read["form"] == "summary"
        assert summarised.before == read["value"]

    def test_the_engines_delta_is_not_mutated(self, project: Path) -> None:
        entries = self._entries(project, lambda root: (root / "swc" / "example" / "ExampleSWC.yaml").unlink())
        before = [entry.to_json() for entry in entries]

        summarize_removed_elements(entries)

        assert [entry.to_json() for entry in entries] == before
        assert isinstance(entries[0].before["ports"], list)
        assert entries[0].before_form is None

    def test_a_removed_port_is_a_member_and_stays_whole(self, project: Path) -> None:
        entries = self._entries(
            project,
            lambda root: _edit(root, "swc/example/ExampleSWC.yaml", lambda document: document["ports"].pop(0)),
        )
        (removed,) = [entry for entry in entries if entry.operation == "remove"]

        assert removed.address.path
        assert summarize_removed_elements(entries) == entries
        assert isinstance(removed.before, dict) and "interface" in removed.before

    def test_a_create_and_an_update_are_never_summarised(self, project: Path) -> None:
        created = self._entries(
            project,
            lambda root: _write(root, model_element_storage_name("VehicleSpeed_kph", kind="value_type"), _ALIAS),
        )
        updated = self._entries(project, lambda root: _runnable(root, lambda runnable: runnable.update(name="Moved")))

        assert summarize_removed_elements(created) == created
        assert summarize_removed_elements(updated) == updated

    def test_what_the_summary_dropped_is_counted(self, project: Path) -> None:
        entries = self._entries(project, lambda root: (root / "swc" / "example" / "ExampleSWC.yaml").unlink())
        (summarised,) = summarize_removed_elements(entries)

        counted, hidden = elided_fields(summarised)

        assert counted == {
            "ports": 1,
            "runnables": 1,
            "memory": 1,
            "data_type_mappings": 0,
            "satisfies": 1,
            "variables": 0,
            "exclusive_areas": 0,
        }
        assert hidden == len(entries[0].before) - len(summarised.before)
        assert hidden > 0
        assert elided_fields(entries[0]) == ({}, 0)

    def test_an_unset_summary_field_is_not_counted_as_dropped(self, project: Path) -> None:
        """A connector with no ``composition``: the omitted unset reference is not counted as dropped."""
        entries = self._entries(project, lambda root: (root / "connectors" / "Example_to_Consumer.yaml").unlink())
        (summarised,) = summarize_removed_elements(entries)

        counted, hidden = elided_fields(summarised)

        assert entries[0].before["composition"] is None
        assert "composition" not in summarised.before
        assert counted == {}
        assert hidden == 3
        assert hidden == len(entries[0].before) - len(summarised.before) - 1

    def test_a_before_the_summary_does_not_narrow_carries_no_flag(self) -> None:
        """A ``before`` the summary does not narrow carries no flag.

        Built by hand: no shipped kind publishes so narrow a whole value.
        """
        narrow = DeltaEntry(
            operation="remove",
            address=Address(layer="hardware", element="CPU"),
            before={"name": "CPU", "kind": "processor"},
        )

        (unchanged,) = summarize_removed_elements([narrow])

        assert unchanged == narrow
        assert unchanged.before_form is None
        assert "before_form" not in unchanged.to_json()

    def test_a_response_is_summarised_on_a_copy_and_every_other_answer_passes(self, project: Path) -> None:
        entries = self._entries(project, lambda root: (root / "hardware" / "RAM0.yaml").unlink())
        response = {"response": "change_set", "status": "staged", "delta": [entry.to_json() for entry in entries]}
        original = {**response, "delta": [dict(entry) for entry in response["delta"]]}

        sent = summarize_response(response)

        assert sent["delta"][0]["before_form"] == "summary"
        assert sent["delta"][0]["before"]["accessible"] == len(original["delta"][0]["before"]["accessible"])
        assert response == original
        for other in (
            {"response": "read", "form": "full", "value": {"ports": [1, 2]}},
            {"error": "broken-reference", "message": "x"},
        ):
            assert summarize_response(other) == other

    @pytest.mark.parametrize("model_root", [REPO_ROOT / "examples" / "basic" / "model", AGENT_TASK_BENCH / "model"])
    def test_the_published_summary_equals_reads_own_over_every_element(self, model_root: Path) -> None:
        """``summarize_published`` is a second statement of ``summarize``'s loop; this is the oracle that they agree."""
        registry = reg.current()
        snapshot = load_snapshot(str(model_root))
        compared = 0
        for layer in registry.layers:
            if layer.name not in snapshot.state:
                continue
            for element in snapshot.state[layer.name]:
                kind = registry.kind_of_model(type(element))
                assert kind is not None
                placement = placement_of(snapshot, layer, str(getattr(element, layer.identity_field)))
                published = publish_element(element, registry, placement=placement)
                assert summarize_published(published, kind, registry) == summarize(
                    element, kind, registry, placement=placement
                ), f"{layer.name}/{getattr(element, layer.identity_field)}"
                compared += 1
        assert compared > 20
