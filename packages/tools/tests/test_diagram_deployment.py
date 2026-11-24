"""The ``deployment`` view: ``graph._deployment``'s tree and ``writer/diagrams/deployment.py``'s drawing.

The processor, its cores, the partitions on each core, the tasks in each partition and the components each task runs,
drawn by the nesting overview's renderer with this view's own id, heading, fills and leaf (no Node). The golden
``fixtures/diagrams/svg/deployment.svg`` is written by ``python packages/tools/tests/test_diagram_svg.py --freeze-svg``,
never by hand.

What these tests do not reach: whether the packing reads well on a real model. The golden is a change detector; the
label widths are estimates.
"""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import REPO_ROOT, partition_on
from swcraft_tools.writer.diagrams import deployment, graph, nesting, report, svg

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
GOLDEN = FIXTURE_DIR / "svg" / "deployment.svg"
NESTING_GOLDEN = FIXTURE_DIR / "svg" / "nesting.svg"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"

NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def tree() -> graph.NestingNode:
    extracted = graph.build_graphs(str(FIXTURE_DIR / "model")).deployment
    assert extracted is not None
    return extracted


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _component(model: Path, name: str, runnables: tuple[str, ...] = ("Run",), asil: str | None = None) -> None:
    lines = [f"name: {name}", "type: application"]
    if asil is not None:
        lines.append(f"asil: {asil}")
    lines.append("runnables:")
    lines.extend(f"  - name: {runnable}" for runnable in runnables)
    _write(model / "swc" / "group" / f"{name}.yaml", "\n".join(lines) + "\n")


def _task(model: Path, name: str, core: str, refs: tuple[tuple[str, str], ...]) -> None:
    """A task on *core*, naming the partition :func:`_core` writes there."""
    lines = [f"name: {name}", "period: 0.01", "priority: 1", f"partition: {partition_on(core)}", "runnables:"]
    for swc, runnable in refs:
        lines.extend([f"  - swc: {swc}", f"    runnable: {runnable}"])
    _write(model / "tasks" / f"{name}.yaml", "\n".join(lines) + "\n")


def _core(model: Path, name: str, capability: str | None = None, partition_asil: str | None = None) -> None:
    """A core and its partition (``conftest.partition_on``), which :func:`_task` names; rated *partition_asil*."""
    lines = [f"name: {name}", "kind: core", "threshold: 0.7"]
    if capability is not None:
        lines.append(f"asil_capability: {capability}")
    _write(model / "hardware" / f"{name}.yaml", "\n".join(lines) + "\n")
    _partition(model, partition_on(name), name, partition_asil)


def _partition(
    model: Path, name: str, core: str, asil: str | None = None, file: str | None = None, trust: str | None = None
) -> None:
    lines = [f"name: {name}", "kind: partition", f"core: {core}"]
    if asil is not None:
        lines.append(f"asil: {asil}")
    if trust is not None:
        lines.append(f"trust: {trust}")
    _write(model / "hardware" / f"{file or name}.yaml", "\n".join(lines) + "\n")


def _processor(model: Path, name: str) -> None:
    _write(model / "hardware" / f"{name}.yaml", f"name: {name}\nkind: processor\n")


def _requirement(model: Path, identity: str, asil: str, links: tuple[str, ...]) -> None:
    """The requirement, and each of *links* -- components already written -- satisfying it.

    The link lives in the component, so its file gains a top-level ``satisfies`` key.
    """
    lines = [f"id: {identity}", "title: t", "description: d", "req_type: req", "version: 1", "source: architecture"]
    lines.append(f"asil: {asil}")
    _write(model / "requirements" / f"{identity}.yaml", "\n".join(lines) + "\n")
    for link in links:
        path = model / "swc" / "group" / f"{link}.yaml"
        text = path.read_text(encoding="utf-8")
        _write(path, f"{text}satisfies:\n  - {identity}@1\n")


def _levelled_model(tmp_path: Path) -> Path:
    """Write three cores, each with its partition, and one task in each of the first two partitions.

    The cores are rated D, unrated and rated QM; their partitions rated C, unrated and empty.
    """
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0", "D", partition_asil="C")
    _core(model, "C1")
    _core(model, "C2", "QM")
    _component(model, "Declared", asil="C")
    _component(model, "Allocated")
    _component(model, "WrittenQm", asil="QM")
    _component(model, "Unrated")
    _requirement(model, "REQ_1", "B", ("Allocated",))
    _task(model, "T0", "C0", (("Declared", "Run"), ("Allocated", "Run")))
    _task(model, "T1", "C1", (("WrittenQm", "Run"), ("Unrated", "Run")))
    return model


def _shape(node: graph.NestingNode) -> tuple:
    return (node.kind, node.name, node.title, tuple(_shape(child) for child in node.children))


def _drawn(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter(f"{NS}g") if element.get("data-role") in ("frame", "member")]


def _labels(root: ET.Element) -> list[str]:
    layer = next(element for element in root if element.tag == f"{NS}g" and element.get("class") == "labels")
    return [element.text or "" for element in layer]


# The tree


def test_the_tree_is_processor_cores_partitions_tasks_components(tree: graph.NestingNode) -> None:
    task = ("task", "Task_10ms", "", (("component", "Sensor", "", ()), ("component", "Controller", "", ())))
    assert _shape(tree) == (
        "processor",
        "CPU",
        "",
        (("core", "CORE0", "", (("partition", "PARTITION0", "trusted", (task,)),)),),
    )
    assert all(node.name == node.type for node in _walk(tree))


def _walk(node: graph.NestingNode) -> list[graph.NestingNode]:
    return [node, *(item for child in node.children for item in _walk(child))]


def test_a_core_carries_its_capability_a_partition_its_level_and_a_component_its_effective_level(
    tmp_path: Path,
) -> None:
    extracted = graph.build_graphs(str(_levelled_model(tmp_path)))
    assert extracted.deployment is not None
    t0 = ("task", "T0", "", (("component", "Declared", "ASIL C", ()), ("component", "Allocated", "ASIL B", ())))
    t1 = ("task", "T1", "", (("component", "WrittenQm", "", ()), ("component", "Unrated", "", ())))
    assert _shape(extracted.deployment) == (
        "processor",
        "CPU",
        "",
        (
            ("core", "C0", "ASIL capability D", (("partition", "C0_PARTITION", "ASIL C", (t0,)),)),
            ("core", "C1", "", (("partition", "C1_PARTITION", "", (t1,)),)),
            ("core", "C2", "ASIL capability QM", (("partition", "C2_PARTITION", "", ()),)),
        ),
    )
    assert [record for record in extracted.not_drawn if record.view == "deployment"] == []


def test_partitions_sit_under_their_core_and_tasks_under_their_partition_in_reader_order(tmp_path: Path) -> None:
    """Partitions sit under their core and tasks under the partition they name, in the reader's order (by name).

    A ``QM`` partition carries no suffix: ``QM`` is not an ASIL.
    """
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _partition(model, "Pb", "C0", "QM")
    _partition(model, "Pa", "C0", "B")
    _component(model, "A")
    _task(model, "T1", "C0", (("A", "Run"),))
    _write(model / "tasks" / "T3.yaml", "name: T3\nperiod: 0.01\npriority: 1\npartition: Pa\nrunnables: []\n")
    _write(model / "tasks" / "T2.yaml", "name: T2\nperiod: 0.01\npriority: 1\npartition: Pa\nrunnables: []\n")
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    (core,) = extracted.deployment.children
    assert [(node.name, node.title, [task.name for task in node.children]) for node in core.children] == [
        ("C0_PARTITION", "", ["T1"]),
        ("Pa", "ASIL B", ["T2", "T3"]),
        ("Pb", "", []),
    ]
    assert [record for record in extracted.not_drawn if record.view == "deployment"] == []


@pytest.mark.parametrize(
    ("asil", "trust", "title"),
    [
        ("D", "trusted", "ASIL D · trusted"),
        (None, "trusted", "trusted"),
        ("QM", "trusted", "trusted"),
        ("D", "trusted_with_protection", "ASIL D"),
        (None, "trusted_with_protection", ""),
        ("D", "non_trusted", "ASIL D"),
        (None, "non_trusted", ""),
        (None, None, ""),
    ],
)
def test_a_trusted_partition_carries_the_trust_mark_and_a_confined_one_none(
    tmp_path: Path, asil: str | None, trust: str | None, title: str
) -> None:
    """A trusted partition carries the trust mark; a confined class or none adds nothing.

    ``trusted`` follows the level, or stands alone on an unrated or ``QM`` partition.
    """
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _partition(model, "P", "C0", asil, trust=trust)
    _write(model / "tasks" / "T.yaml", "name: T\nperiod: 0.01\npriority: 1\npartition: P\nrunnables: []\n")
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    (core,) = extracted.deployment.children
    assert {node.name: node.title for node in core.children}["P"] == title


def test_a_decomposed_level_is_printed_as_written_and_a_decomposed_qm_prints_nothing(tmp_path: Path) -> None:
    """A decomposed level prints as written (``ASIL B(D)``); ``QM(D)`` and ``QM(A)`` are no ASIL and add no suffix."""
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0", partition_asil="B(D)")
    _partition(model, "PQm", "C0", "QM(D)")
    _component(model, "Decomposed", asil="B(D)")
    _component(model, "DecomposedQm", asil="QM(D)")
    _component(model, "DerivedQm")
    _requirement(model, "REQ_1", "QM(A)", ("DerivedQm",))
    _task(model, "T0", "C0", (("Decomposed", "Run"), ("DecomposedQm", "Run"), ("DerivedQm", "Run")))
    _write(model / "tasks" / "T1.yaml", "name: T1\nperiod: 0.01\npriority: 1\npartition: PQm\nrunnables: []\n")
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    (core,) = extracted.deployment.children
    partitions = {node.name: node for node in core.children}

    assert partitions["C0_PARTITION"].title == "ASIL B(D)"
    assert partitions["PQm"].title == ""
    (t0,) = partitions["C0_PARTITION"].children
    assert [(node.name, node.title) for node in t0.children] == [
        ("Decomposed", "ASIL B(D)"),
        ("DecomposedQm", ""),
        ("DerivedQm", ""),
    ]


def test_a_component_several_tasks_run_is_drawn_under_each_and_once_per_task(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _component(model, "A", ("R1", "R2"))
    _task(model, "T0", "C0", (("A", "R1"), ("A", "R2")))
    _task(model, "T1", "C0", (("A", "R2"),))
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    tasks = extracted.deployment.children[0].children[0].children
    assert [(task.name, [child.name for child in task.children]) for task in tasks] == [("T0", ["A"]), ("T1", ["A"])]


def test_an_instance_entry_is_drawn_under_its_type(tmp_path: Path) -> None:
    """An ``instance`` entry resolves to its type through ``scheduling_identity``; one resolving to none is a record."""
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _component(model, "A", ("R1",))
    _component(model, "B", ("R1",))
    _write(
        model / "compositions" / "Root.yaml",
        "name: Root\ntitle: Root\nroot: true\nmembers:\n  - name: left\n    type: A\n  - name: right\n    type: A\n"
        "  - name: b\n    type: B\n",
    )
    _write(
        model / "tasks" / "T0.yaml",
        f"name: T0\nperiod: 0.01\npriority: 1\npartition: {partition_on('C0')}\nrunnables:\n"
        "  - instance: right\n    runnable: R1\n  - instance: nowhere\n    runnable: R1\n"
        "  - swc: B\n    runnable: R1\n",
    )
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    (task,) = extracted.deployment.children[0].children[0].children
    assert [child.name for child in task.children] == ["A", "B"]
    assert (
        graph.NotDrawn(
            graph.DEPLOYMENT_VIEW,
            "instance-not-resolved",
            "T0: nowhere",
            "the instance nowhere resolves to no component",
        )
        in extracted.not_drawn
    )


def test_a_core_declared_twice_holds_its_tasks_under_its_first_box_only(tmp_path: Path) -> None:
    """The second declaration is drawn empty and adds no record: ``check-model`` reports the name."""
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _write(model / "hardware" / "Second.yaml", "name: C0\nkind: core\nthreshold: 0.7\n")
    _component(model, "A")
    _task(model, "T0", "C0", (("A", "Run"),))
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    assert [
        (core.name, [(partition.name, [task.name for task in partition.children]) for partition in core.children])
        for core in extracted.deployment.children
    ] == [("C0", [("C0_PARTITION", ["T0"])]), ("C0", [])]
    assert [record for record in extracted.not_drawn if record.view == "deployment"] == []


def test_a_partition_declared_twice_holds_its_tasks_under_its_first_box_only(tmp_path: Path) -> None:
    """The second declaration is drawn empty, under the core it names, and adds no record."""
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _core(model, "C1")
    _partition(model, "C0_PARTITION", "C1", file="Z_second")
    _component(model, "A")
    _task(model, "T0", "C0", (("A", "Run"),))
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is not None
    assert [
        (core.name, [(partition.name, [task.name for task in partition.children]) for partition in core.children])
        for core in extracted.deployment.children
    ] == [("C0", [("C0_PARTITION", ["T0"])]), ("C1", [("C0_PARTITION", []), ("C1_PARTITION", [])])]
    assert [record for record in extracted.not_drawn if record.view == "deployment"] == []


# What cannot be drawn


def test_what_the_tree_cannot_hold_is_a_record_by_kind(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _processor(model, "CPU")
    _processor(model, "CPU2")
    _core(model, "C0")
    _component(model, "A")
    _component(model, "Idle")
    _component(model, "Stranded")
    _task(model, "T0", "C0", (("A", "Run"), ("Ghost", "Run"), ("A", "Missing")))
    _task(model, "Lost", "C9", (("A", "Run"),))
    _write(model / "tasks" / "OnCore.yaml", "name: OnCore\nperiod: 0.01\npriority: 1\npartition: C0\nrunnables: []\n")
    _partition(model, "Adrift", "C9")
    _partition(model, "Empty_adrift", "CPU")
    _write(
        model / "tasks" / "Unplaced.yaml",
        "name: Unplaced\nperiod: 0.01\npriority: 1\npartition: Adrift\n"
        "runnables:\n  - swc: Stranded\n    runnable: Run\n",
    )
    extracted = graph.build_graphs(str(model))

    records = [
        (record.kind, record.subject, record.detail) for record in extracted.not_drawn if record.view == "deployment"
    ]
    assert records == [
        ("processor-not-unique", "CPU2", "CPU is drawn as the processor"),
        ("partition-not-declared", "Lost", "no partition C9_PARTITION"),
        ("partition-not-declared", "OnCore", "no partition C0"),
        ("partition-core-not-declared", "Adrift", "no core C9; not drawn with it: Unplaced"),
        ("partition-core-not-declared", "Empty_adrift", "no core CPU"),
        ("component-not-declared", "T0: Ghost", "no component Ghost"),
        ("runnable-not-declared", "T0: A.Missing", "A declares no runnable Missing"),
        ("not-run", "Idle", "no task runs a runnable of it"),
        ("not-run", "Stranded", "no task runs a runnable of it"),
    ]
    assert extracted.deployment is not None
    (core,) = extracted.deployment.children
    assert [(partition.name, [task.name for task in partition.children]) for partition in core.children] == [
        ("C0_PARTITION", ["T0"])
    ]

    ours = [record for record in extracted.not_drawn if record.view == "deployment"]
    text = report.render_report("P", [], ours)
    assert "Not drawn: 9 in 6 kinds" in text
    for kind, count in (
        ("component-not-declared", 1),
        ("not-run", 2),
        ("partition-core-not-declared", 2),
        ("partition-not-declared", 2),
        ("processor-not-unique", 1),
        ("runnable-not-declared", 1),
    ):
        assert f"  {kind}: {count}\n" in text
    assert "core-not-declared:" not in text.replace("partition-core-not-declared:", "")


def test_a_component_no_task_runs_is_a_report_line_and_not_a_box(tree: graph.NestingNode) -> None:
    records = graph.build_graphs(str(FIXTURE_DIR / "model")).not_drawn
    assert [(record.kind, record.subject) for record in records if record.view == "deployment"] == [
        ("not-run", "DiagServer"),
        ("not-run", "Logger"),
    ]
    assert {"DiagServer", "Logger"}.isdisjoint(node.name for node in _walk(tree))


def test_a_model_with_tasks_and_no_processor_draws_no_view_and_says_so(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _core(model, "C0")
    _component(model, "A")
    _task(model, "T0", "C0", (("A", "Run"),))
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is None
    assert "deployment" not in graph.view_ids(extracted)
    assert [(record.view, record.kind) for record in extracted.not_drawn] == [
        ("nesting", "no-root"),
        ("deployment", "no-processor"),
    ]


def test_a_model_with_no_task_has_no_deployment_and_no_record(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _processor(model, "CPU")
    _core(model, "C0")
    _component(model, "Idle")
    extracted = graph.build_graphs(str(model))
    assert extracted.deployment is None
    assert "deployment" not in graph.view_ids(extracted)
    assert [record for record in extracted.not_drawn if record.view == "deployment"] == []


def test_the_view_comes_after_nesting_and_before_the_sequences(tree: graph.NestingNode) -> None:
    ids = graph.view_ids(graph.build_graphs(str(FIXTURE_DIR / "model")))
    assert ids[ids.index("nesting") + 1 :] == ["deployment", "sequence-speed_cycle"]


# The drawing


def test_two_renders_are_byte_identical_and_equal_the_golden(tree: graph.NestingNode) -> None:
    first = deployment.render_deployment(tree)
    assert deployment.render_deployment(tree) == first
    assert first == GOLDEN.read_text(encoding="utf-8")


def test_the_document_is_the_deployment_view_headed_by_its_processor(tree: graph.NestingNode) -> None:
    root = ET.fromstring(deployment.render_deployment(tree))
    assert root.get("data-view") == "deployment"
    assert root.find(f"{NS}title").text == "CPU — deployment"
    assert [element.get("class") for element in root if element.tag == f"{NS}g"] == ["frame", "members", "labels"]


def test_each_kind_takes_its_fill_and_only_the_component_is_a_leaf(tmp_path: Path) -> None:
    extracted = graph.build_graphs(str(_levelled_model(tmp_path)))
    assert extracted.deployment is not None
    root = ET.fromstring(deployment.render_deployment(extracted.deployment))
    drawn = _drawn(root)
    fills = {element.get("data-kind"): element.find(f"{NS}rect").get("fill") for element in drawn}
    assert fills == {
        "processor": svg.FRAME_FILL,
        "core": svg.COMPOSITION_FILL,
        "partition": deployment.DEPLOYMENT_PARTITION_FILL,
        "task": deployment.DEPLOYMENT_TASK_FILL,
        "component": svg.COMPONENT_FILL,
    }
    assert len(set(fills.values())) == 5
    labels = _labels(root)
    assert labels == [
        "CPU",
        "C0 — ASIL capability D",
        "C0_PARTITION — ASIL C",
        "T0",
        "Declared — ASIL C",
        "Allocated — ASIL B",
        "C1",
        "C1_PARTITION",
        "T1",
        "WrittenQm",
        "Unrated",
        "C2 — ASIL capability QM",
        "C2_PARTITION",
    ]
    layer = next(element for element in root if element.tag == f"{NS}g" and element.get("class") == "labels")
    anchors = {element.text: element.get("text-anchor") for element in layer}
    assert [text for text, anchor in anchors.items() if anchor == "middle"] == [
        "Declared — ASIL C",
        "Allocated — ASIL B",
        "WrittenQm",
        "Unrated",
    ]
    titles = [element[0].text for element in drawn]
    assert titles == [
        "processor CPU",
        "C0 (core)\nASIL capability D",
        "C0_PARTITION (partition)\nASIL C",
        "T0 (task)",
        "Declared (component)\nASIL C",
        "Allocated (component)\nASIL B",
        "C1 (core)",
        "C1_PARTITION (partition)",
        "T1 (task)",
        "WrittenQm (component)",
        "Unrated (component)",
        "C2 (core)\nASIL capability QM",
        "C2_PARTITION (partition)",
    ]


def test_the_nesting_call_is_unchanged_by_the_generalisation() -> None:
    """The defaults are the nesting overview's: spelled out, they draw its golden byte for byte."""
    extracted = graph.build_graphs(str(FIXTURE_DIR / "model")).nesting
    assert extracted is not None
    spelled = nesting.render_nesting(
        extracted,
        view="nesting",
        fills={"composition": svg.COMPOSITION_FILL, "component": svg.COMPONENT_FILL},
        leaves=frozenset({"component"}),
        leaf_titles=False,
    )
    assert spelled == nesting.render_nesting(extracted) == NESTING_GOLDEN.read_text(encoding="utf-8")


def test_a_leaf_title_is_shown_only_when_the_caller_asks() -> None:
    leaf = graph.NestingNode("A", "A", "component", "ASIL D", ())
    root = graph.NestingNode("R", "R", "composition", "", (leaf,))
    assert _labels(ET.fromstring(nesting.render_nesting(root)))[1] == "A"
    assert _labels(ET.fromstring(nesting.render_nesting(root, leaf_titles=True)))[1] == "A — ASIL D"


# The module's boundary


def test_the_view_id_is_the_extractors() -> None:
    assert deployment.VIEW == graph.DEPLOYMENT_VIEW
    assert nesting.NESTING_VIEW == graph.NESTING_VIEW
    assert set(deployment.FILLS) | {"processor"} == {"processor", "core", "partition", "task", "component"}
    assert deployment.LEAVES == frozenset({"component"})


def test_the_renderer_imports_only_the_nesting_renderer_and_the_svg_primitives() -> None:
    tree = ast.parse((PACKAGE_DIR / "deployment.py").read_text(encoding="utf-8"))
    imported = sorted(
        {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0}
        | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    )
    assert [name for name in imported if "swcraft" in name] == [
        "swcraft_tools.writer.diagrams.nesting",
        "swcraft_tools.writer.diagrams.svg",
    ]
