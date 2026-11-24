"""Tests for differ.py's summarize_diff: the bounded summary model_diff answers first."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

from conftest import copy_agent_task_bench
from differ_support import make_connector, make_snapshot
from swcraft_tools.differ import ChangedItem, LayerDiff, ModelDiff, diff_snapshots, format_diff_json, summarize_diff
from swcraft_tools.model_registry import production_registry
from swcraft_tools.snapshot import load_snapshot


def _apply_task_02(model: Path) -> None:
    """The small bench's task 02 change, as its prompt states it, written into *model*."""

    def edit(relative: str, old: str, new: str) -> None:
        path = model / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, relative
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")

    (model / "swc" / "application" / "BrakeDataLogger.yaml").write_text(
        "name: BrakeDataLogger\n"
        "type: application\n"
        "ports:\n"
        "- name: rBrakeDiagnostics\n"
        "  direction: require\n"
        "  interface: IBrakeDiagnostics\n"
        "runnables:\n"
        "- name: LogSnapshot\n"
        "  events:\n"
        "  - type: timing\n"
        "    period: 0.1\n"
        "  calls:\n"
        "  - port: rBrakeDiagnostics\n"
        "    operation: ReadSnapshot\n"
        "  satisfies:\n"
        "  - REQ_BTP_DIAGNOSTICS@1\n"
        "memory:\n"
        "  RAM: 1024\n",
        encoding="utf-8",
        newline="\n",
    )
    (model / "connectors" / "DiagnosticsToCluster.yaml").unlink()
    (model / "connectors" / "DiagnosticsToLogger.yaml").write_text(
        "name: DiagnosticsToLogger\n"
        "from:\n  swc: DiagnosticManager\n  port: pBrakeDiagnostics\n"
        "to:\n  swc: BrakeDataLogger\n  port: rBrakeDiagnostics\n",
        encoding="utf-8",
        newline="\n",
    )
    edit(
        "compositions/VehicleControl.yaml",
        "  - name: NvM\n",
        "  - name: BrakeDataLogger\n    type: BrakeDataLogger\n  - name: NvM\n",
    )
    edit(
        "tasks/Task_100ms.yaml",
        "satisfies:\n",
        "- swc: BrakeDataLogger\n  runnable: LogSnapshot\nsatisfies:\n",
    )
    edit(
        "swc/application/DiagnosticManager.yaml",
        "  interface: IBrakeDiagnostics\n  satisfies:\n  - REQ_SHOWCASE_BASELINE_002@1\n",
        "  interface: IBrakeDiagnostics\n",
    )


def test_the_summary_of_task_02_names_its_six_elements_under_their_layers(tmp_path: Path) -> None:
    """The small bench's task 02 change: 2 added, 1 removed, 3 changed, each under its layer and verb."""
    before = copy_agent_task_bench(tmp_path / "before") / "model"
    after = copy_agent_task_bench(tmp_path / "after") / "model"
    _apply_task_02(after)

    summary = summarize_diff(diff_snapshots(load_snapshot(str(before)), load_snapshot(str(after))))

    nothing = {"count": 0, "identities": []}
    assert summary["layers"] == {
        "swc": {
            "added": {"count": 1, "identities": ["BrakeDataLogger"]},
            "removed": nothing,
            "changed": {"count": 1, "identities": ["DiagnosticManager"]},
        },
        "connectors": {
            "added": {"count": 1, "identities": ["DiagnosticsToLogger"]},
            "removed": {"count": 1, "identities": ["DiagnosticsToCluster"]},
            "changed": nothing,
        },
        "compositions": {
            "added": nothing,
            "removed": nothing,
            "changed": {"count": 1, "identities": ["VehicleControl"]},
        },
        "tasks": {"added": nothing, "removed": nothing, "changed": {"count": 1, "identities": ["Task_100ms"]}},
    }
    assert summary["totals"] == {"added": 2, "removed": 1, "changed": 3}


def test_the_summary_names_fifty_identities_per_verb_and_counts_the_rest() -> None:
    """Sixty added connectors: fifty named, in the diff's own order, and sixty counted."""
    connectors = [make_connector(f"C{index:02d}", "A", "p", "B", "r") for index in range(60)]

    summary = summarize_diff(diff_snapshots(make_snapshot(), make_snapshot(connectors=connectors)))

    added = summary["layers"]["connectors"]["added"]
    assert added["count"] == 60
    assert added["identities"] == [f"C{index:02d}" for index in range(50)]
    assert summary["totals"] == {"added": 60, "removed": 0, "changed": 0}


def test_an_empty_diff_summarises_to_no_layer_and_zero_totals() -> None:
    """An unchanged model is the same envelope, never a message."""
    assert summarize_diff(diff_snapshots(make_snapshot(), make_snapshot())) == {
        "layers": {},
        "totals": {"added": 0, "removed": 0, "changed": 0},
    }


def test_the_summary_counts_every_diffed_layer_as_the_json_document_lists_it() -> None:
    """Every layer the diff carries, each verb touched: the summary's counts are ``format_diff_json``'s lists.

    Built over every attribute of :class:`ModelDiff`, so a skipped layer is missing here.
    """
    registry = production_registry()
    attributes = [field.name for field in dataclasses.fields(ModelDiff)]
    layer_of = {registry.layer(name).snapshot_attribute or name: name for name in registry.layer_names}
    layers = {}
    for index, attribute in enumerate(attributes):
        name = layer_of[attribute]
        identity = registry.layer(name).identity_field
        layers[attribute] = LayerDiff(
            added=[SimpleNamespace(**{identity: f"{name}_added{n}"}) for n in range(index % 3 + 1)],
            removed=[SimpleNamespace(**{identity: f"{name}_removed"})],
            changed=[ChangedItem(key=(f"{name}_changed", "part"), changed_fields=[("x", 1, 2)])],
        )
    diff = ModelDiff(**layers)

    summary = summarize_diff(diff)
    document = json.loads(format_diff_json(diff))["layers"]

    assert sorted(summary["layers"]) == sorted(layer_of[attribute] for attribute in attributes)
    for attribute in attributes:
        verbs = summary["layers"][layer_of[attribute]]
        listed = document[attribute]
        assert {verb: verbs[verb]["count"] for verb in verbs} == {
            verb: len(listed[verb]) for verb in ("added", "removed", "changed")
        }, attribute
        assert verbs["changed"]["identities"] == [entry["key"] for entry in listed["changed"]]
        assert verbs["changed"]["identities"] == [[f"{layer_of[attribute]}_changed", "part"]]
    assert summary["totals"] == {
        verb: sum(len(document[attribute][verb]) for attribute in attributes)
        for verb in ("added", "removed", "changed")
    }
