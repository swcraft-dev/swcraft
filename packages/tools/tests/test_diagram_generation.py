"""The diagram generator's entry point: ``writer/diagrams/__init__.py``'s ``generate_diagrams``.

It reads the model once, lays every ELK graph out in **one** bridge call, renders everything **in memory**, and only
then writes: each file compared before it is written (an unchanged file keeps its bytes and mtime), and every file of
the generator's own naming this run did not produce removed. A layout failure, or a view file name the generator
refuses, writes **nothing**: a failed build leaves no outputs.

The goldens are the generator's whole output on four models this repository owns, under ``fixtures/diagrams_golden/``,
frozen by ``python packages/tools/tests/test_diagram_generation.py --freeze`` (needs Node), never by hand.

What these tests do not reach: whether a picture is right (the goldens are **change detectors**; the correctness oracle
is a reader's look at real tool exports), and another platform's Node: the goldens' byte identity across macOS, Linux
and Windows is measured by CI, not here.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import threading
import tomllib
from pathlib import Path
from xml.etree import ElementTree

import pytest
from conftest import REPO_ROOT, run_captured
from layout_seam_support import refuse_to_spawn
from node_support import requires_node
from swcraft_tools import writer
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.writer import diagrams
from swcraft_tools.writer.diagrams import deployment, graph, layout, nesting, page, report, sequence, svg
from symlink_support import symlink_or_skip

FIXTURES = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures"
GOLDEN_DIR = FIXTURES / "diagrams_golden"
DIAGRAMS_MODEL = FIXTURES / "diagrams" / "model"

#: The four models the goldens are frozen on: golden directory -> (model root, project name).
#: ``examples/basic`` and the showcase take their manifest's ``name``; the two fixture models
#: have no manifest and take a name of their own.
GOLDEN_MODELS: dict[str, tuple[Path, str]] = {
    "basic": (REPO_ROOT / "examples" / "basic" / "model", "swcraft-basic-example"),
    "showcase": (FIXTURES / "showcase" / "model", "vehicle-control-showcase"),
    "compositions": (FIXTURES / "compositions" / "model", "Composition fixture"),
    "diagrams": (DIAGRAMS_MODEL, "Diagram fixture"),
}

PROJECT = "Diagram fixture"
NS = "{http://www.w3.org/2000/svg}"

#: Mid-2001, far from any time a test runs at: a file the generator rewrote cannot keep it.
OLD_MTIME_NS = 993_902_400_000_000_000


@pytest.fixture
def node() -> str:
    return requires_node()


def _files(directory: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in directory.iterdir() if entry.is_file()}


def _expected_files(view_ids: list[str]) -> list[str]:
    names = [name for view in view_ids for name in (f"{view}.svg", page.page_file(view))]
    return [*names, report.REPORT_FILE, page.INDEX_PAGE]


def _age(directory: Path) -> None:
    for entry in directory.iterdir():
        os.utime(entry, ns=(OLD_MTIME_NS, OLD_MTIME_NS))


def _block(page_text: str, block_id: str) -> dict:
    """The JSON data block *block_id* of a page, parsed."""
    found = re.findall(rf'<script type="application/json" id="{block_id}">(.*?)</script>', page_text, re.DOTALL)
    assert len(found) == 1, block_id
    return json.loads(found[0])


def _copy_model(tmp_path: Path) -> Path:
    model_root = tmp_path / "model"
    shutil.copytree(DIAGRAMS_MODEL, model_root)
    return model_root


# The goldens


@pytest.mark.parametrize("golden", sorted(GOLDEN_MODELS))
def test_the_output_is_the_golden_byte_for_byte(golden: str, node: str, tmp_path: Path) -> None:
    model_root, project_name = GOLDEN_MODELS[golden]
    views_root = tmp_path / "views"

    diagrams.generate_diagrams(str(model_root), str(views_root), project_name)

    produced = _files(views_root)
    frozen = _files(GOLDEN_DIR / golden)
    assert sorted(produced) == sorted(frozen), "the file set moved; re-freeze with the reason in the log"
    changed = [name for name in sorted(produced) if produced[name] != frozen[name]]
    assert changed == [], f"these files differ from the golden: {changed}"


def test_every_golden_directory_is_a_frozen_model() -> None:
    assert sorted(entry.name for entry in GOLDEN_DIR.iterdir() if entry.is_dir()) == sorted(GOLDEN_MODELS)


def test_the_generated_svg_is_the_renderers_golden_for_the_fixture_model(node: str, tmp_path: Path) -> None:
    """The generator composes the renderers and adds nothing: its SVGs are the renderers' goldens."""
    views_root = tmp_path / "views"
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    svg_goldens = FIXTURES / "diagrams" / "svg"
    for view in result.views:
        assert (views_root / f"{view}.svg").read_bytes() == (svg_goldens / f"{view}.svg").read_bytes(), view


# What is written, and what the result says


def test_every_file_of_the_views_is_written_and_nothing_else(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "out" / "views"

    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    view_ids = graph.view_ids(graph.build_graphs(str(DIAGRAMS_MODEL)))
    assert list(result.views) == view_ids
    assert list(result.written) == _expected_files(view_ids)
    assert sorted(entry.name for entry in views_root.iterdir()) == sorted(result.written)
    assert result.kept == ()
    assert result.removed == ()
    assert result.report_file == report.REPORT_FILE


def test_each_page_inlines_its_own_svg_file(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    placements = graph.build_graphs(str(DIAGRAMS_MODEL)).placements
    assert placements == {"Chassis": (("Vehicle", "Chassis"),)}, "the page is handed where each composition is placed"
    for view in result.views:
        svg_text = (views_root / f"{view}.svg").read_text(encoding="utf-8")
        page_text = (views_root / page.page_file(view)).read_text(encoding="utf-8")
        model = _block(page_text, page.MODEL_SCRIPT_ID)
        expected = page.render_view_page(PROJECT, view, svg_text, list(result.views), None, placements, model=model)
        assert page_text == expected, view


def test_an_interface_over_the_cap_reaches_the_written_svg_and_page_as_a_member_list(node: str, tmp_path: Path) -> None:
    """A capped interface reaches the written SVG and page as a member list, extractor to file (no golden holds one)."""
    model_root = _copy_model(tmp_path)
    lines = ("speed: uint8", *(f"e{index}: uint8" for index in range(2, 12)))
    elements = "".join(f"  - name: {line.split(':')[0]}\n    implementation_type: uint8\n" for line in lines)
    (model_root / "interfaces" / "ISpeed.yaml").write_text(
        f"name: ISpeed\nkind: sender_receiver\ndata_elements:\n{elements}", encoding="utf-8", newline="\n"
    )
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)
    listed = 0
    for view in ("ibd-Chassis", "ibd-Vehicle"):
        svg_text = (views_root / f"{view}.svg").read_text(encoding="utf-8")
        root = ElementTree.fromstring(svg_text)
        for port in (element for element in root.iter(f"{NS}g") if element.get("data-role") == "port"):
            listing = port[1] if len(port) > 1 and port[1].tag == f"{NS}desc" else None
            if port.get("data-interface") == "ISpeed":
                assert port[0].tag == f"{NS}title", "the title stays first"
                assert listing is not None and listing.get("class") == svg.MEMBER_LIST_CLASS, port.get("data-name")
                assert listing.text == "\n".join(lines)
                listed += 1
            else:
                assert listing is None, port.get("data-name")
        page_text = (views_root / page.page_file(view)).read_text(encoding="utf-8")
        marker = f'<desc class="{svg.MEMBER_LIST_CLASS}">'
        assert page_text.count(marker) == svg_text.count(marker)
    assert listed > 0
    sequence_text = (views_root / "sequence-speed_cycle.svg").read_text(encoding="utf-8")
    titles = [
        (element[0].text or "").split("\n")
        for element in ElementTree.fromstring(sequence_text).iter(f"{NS}g")
        if (element.get("class") or "").startswith("message ") and len(element) and element[0].tag == f"{NS}title"
    ]
    capped = [lines for lines in titles if len(lines) > 1 + svg.INTERFACE_LINES_CAP]
    assert capped, "a data message carries the grown interface"
    assert all(lines[-1] == "and 3 more" for lines in capped)
    assert svg.MEMBER_LIST_CLASS not in sequence_text and "click the port" not in sequence_text


def test_the_grid_views_are_the_renderers_output(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    extracted = graph.build_graphs(str(DIAGRAMS_MODEL))
    assert extracted.nesting is not None
    assert (views_root / "nesting.svg").read_text(encoding="utf-8") == nesting.render_nesting(extracted.nesting)
    assert extracted.deployment is not None
    assert (views_root / "deployment.svg").read_text(encoding="utf-8") == deployment.render_deployment(
        extracted.deployment
    )
    for view in extracted.sequences:
        written = (views_root / f"sequence-{view.name}.svg").read_text(encoding="utf-8")
        assert written == sequence.render_sequence(view, extracted.interface_contents)


def test_the_index_and_the_report_name_the_project_and_every_view(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    views = [(view, (views_root / f"{view}.svg").read_text(encoding="utf-8")) for view in result.views]
    report_text = (views_root / report.REPORT_FILE).read_text(encoding="utf-8")
    assert report_text == report.render_report(PROJECT, views, result.not_drawn)
    pages = [
        (view, _block((views_root / page.page_file(view)).read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID))
        for view in result.views
    ]
    elements = page.elements_index(pages)
    assert (views_root / page.INDEX_PAGE).read_text(encoding="utf-8") == page.render_index(
        PROJECT, views, report_text, elements
    )


def test_the_result_carries_the_records_the_layout_time_and_names_only(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    assert result.not_drawn == graph.build_graphs(str(DIAGRAMS_MODEL)).not_drawn
    assert [record.kind for record in result.not_drawn] == ["loop", "not-run", "not-run"]
    assert isinstance(result.layout_seconds, float)
    assert result.layout_seconds >= 0
    for name in (*result.written, *result.kept, *result.removed, result.report_file):
        assert name == Path(name).name, f"{name!r} is a file name inside views_root, never a path"


def test_the_written_text_is_utf8_with_lf_line_ends(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    index = (views_root / page.INDEX_PAGE).read_bytes()
    assert b"\r\n" not in index
    assert "—".encode() in (views_root / page.page_file("ibd-Chassis")).read_bytes()


# Compare before writing, and the stale files


def test_a_second_run_keeps_every_file_with_its_bytes_and_its_mtime(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    first = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    _age(views_root)
    before = _files(views_root)

    second = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert second.written == ()
    assert second.kept == first.written
    assert second.removed == ()
    assert _files(views_root) == before
    assert {entry.name: entry.stat().st_mtime_ns for entry in views_root.iterdir()} == dict.fromkeys(
        before, OLD_MTIME_NS
    )


def test_a_changed_file_is_rewritten_and_the_others_are_kept(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    first = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    target = views_root / "ibd-Vehicle.svg"
    original = target.read_bytes()
    target.write_bytes(original + b"<!-- edited -->\n")
    _age(views_root)

    second = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert second.written == ("ibd-Vehicle.svg",)
    assert second.kept == tuple(name for name in first.written if name != "ibd-Vehicle.svg")
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns != OLD_MTIME_NS
    assert (views_root / "ibd-Chassis.svg").stat().st_mtime_ns == OLD_MTIME_NS


def test_a_file_the_model_no_longer_produces_is_removed(node: str, tmp_path: Path) -> None:
    model_root = _copy_model(tmp_path)
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)
    (model_root / "sequences" / "speed_cycle.yaml").unlink()

    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert result.removed == ("sequence-speed_cycle.html", "sequence-speed_cycle.svg")
    assert not (views_root / "sequence-speed_cycle.svg").exists()
    assert not (views_root / "sequence-speed_cycle.html").exists()
    assert "sequence-speed_cycle" not in (views_root / page.INDEX_PAGE).read_text(encoding="utf-8")


def test_the_deployment_view_is_written_as_its_two_files_and_listed_in_the_index(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert "deployment" in result.views
    assert {"deployment.svg", "deployment.html"} <= set(result.written)
    index = (views_root / page.INDEX_PAGE).read_text(encoding="utf-8")
    assert "<h2>Deployment</h2>" in index
    assert '<li><a href="deployment.html">CPU — deployment</a>' in index
    assert index.index("<h2>Nesting</h2>") < index.index("<h2>Deployment</h2>") < index.index("<h2>Sequences</h2>")


@pytest.mark.parametrize("name", ["deployment.svg", "deployment.html"])
def test_the_deployment_files_are_of_the_generators_own_naming(name: str) -> None:
    assert diagrams.GENERATED_FILE_RE.fullmatch(name) is not None
    assert diagrams.VIEW_ID_RE.fullmatch("deployment") is not None


@pytest.mark.parametrize("name", ["deployment.txt", "deployments.svg", "deployment-x.html", "my-deployment.svg"])
def test_nothing_else_is_of_the_generators_naming_because_of_the_deployment_view(name: str) -> None:
    assert diagrams.GENERATED_FILE_RE.fullmatch(name) is None


def test_a_model_whose_tasks_are_gone_removes_the_stale_deployment_files(node: str, tmp_path: Path) -> None:
    model_root = _copy_model(tmp_path)
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)
    assert (views_root / "deployment.svg").is_file()
    shutil.rmtree(model_root / "tasks")
    (model_root / "tasks").mkdir()
    (model_root / "sequences" / "speed_cycle.yaml").unlink()

    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert "deployment" not in result.views
    assert result.removed == (
        "deployment.html",
        "deployment.svg",
        "sequence-speed_cycle.html",
        "sequence-speed_cycle.svg",
    )
    assert "Deployment" not in (views_root / page.INDEX_PAGE).read_text(encoding="utf-8")


def test_only_files_of_the_generators_own_naming_are_removed(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    views_root.mkdir()
    stale = ["ibd-Gone.svg", "ibd-Gone.html", "overview-Gone.svg", "sequence-gone.html", "nesting.html"]
    foreign = ["README.md", "model.c4", ".gitkeep", "ibd-notes.txt", "diagram.svg", "index.htm"]
    for name in stale + foreign:
        (views_root / name).write_text("old\n", encoding="utf-8", newline="\n")
    (views_root / "ibd-Dir.svg").mkdir()

    model_root = _copy_model(tmp_path)
    (model_root / "compositions" / "Vehicle.yaml").write_text(
        (model_root / "compositions" / "Vehicle.yaml").read_text(encoding="utf-8").replace("root: true", "root: false"),
        encoding="utf-8",
        newline="\n",
    )
    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert "nesting" not in result.views, "no root, so no nesting view: its old page is stale"
    assert result.removed == tuple(sorted(stale))
    for name in foreign:
        assert (views_root / name).read_text(encoding="utf-8") == "old\n", name
    assert (views_root / "ibd-Dir.svg").is_dir(), "a directory is never removed"


def test_a_symlink_of_the_generators_naming_is_never_removed(node: str, tmp_path: Path) -> None:
    """A link is the user's, wherever it points: only a regular file of the generator's naming goes."""
    views_root = tmp_path / "views"
    views_root.mkdir()
    outside = tmp_path / "elsewhere.svg"
    outside.write_text("not the generator's\n", encoding="utf-8", newline="\n")
    symlink_or_skip(views_root / "ibd-Gone.svg", outside)

    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert "ibd-Gone.svg" not in result.removed
    assert (views_root / "ibd-Gone.svg").is_symlink()
    assert outside.read_text(encoding="utf-8") == "not the generator's\n"


def test_a_file_differing_from_a_produced_one_only_in_case_is_not_removed(node: str, tmp_path: Path) -> None:
    """On a case-insensitive filesystem the two names are one file; removing it would delete the output."""
    views_root = tmp_path / "views"
    views_root.mkdir()
    (views_root / "ibd-chassis.svg").write_text("old\n", encoding="utf-8", newline="\n")

    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert "ibd-chassis.svg" not in result.removed
    assert (views_root / "ibd-Chassis.svg").read_bytes() == (
        FIXTURES / "diagrams" / "svg" / "ibd-Chassis.svg"
    ).read_bytes()
    # The file takes the name this run spells on either kind of filesystem: a rewrite through the old entry kept its
    # case where the two names are one.
    listed = {entry.name for entry in views_root.iterdir()}
    assert "ibd-Chassis.svg" in listed and "ibd-chassis.svg" not in listed
    assert "ibd-Chassis.svg" in result.written


def test_an_unchanged_file_under_another_case_takes_the_name_this_run_spells(node: str, tmp_path: Path) -> None:
    """A composition renamed by case alone draws the same bytes but another name."""
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    (views_root / "ibd-Chassis.svg").rename(views_root / "ibd-CHASSIS.svg")

    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    listed = {entry.name for entry in views_root.iterdir()}
    assert "ibd-Chassis.svg" in listed and "ibd-CHASSIS.svg" not in listed
    assert result.written == ("ibd-Chassis.svg",)
    assert result.removed == ()


# A failure writes nothing


def _populated_views(tmp_path: Path) -> Path:
    views_root = tmp_path / "views"
    views_root.mkdir()
    for name in ("ibd-Chassis.svg", "ibd-Gone.html", page.INDEX_PAGE, "notes.md"):
        (views_root / name).write_text(f"previous {name}\n", encoding="utf-8", newline="\n")
    _age(views_root)
    return views_root


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns) for entry in directory.iterdir()}


def test_a_node_the_layout_refuses_writes_nothing(tmp_path: Path) -> None:
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)
    absent = tmp_path / "absent" / "views"

    for target in (views_root, absent):
        with pytest.raises(layout.DiagramLayoutError, match="did not report a version"):
            diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(target), PROJECT, node=sys.executable)

    assert _snapshot(views_root) == before, "not a write, not a removal"
    assert not absent.parent.exists(), "the views directory is not even created"


def test_a_layout_process_that_fails_writes_nothing(node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "installation" / "broken-layout.mjs"
    # The engine file the bridge looks for before it starts Node; the broken script never imports it.
    engine = broken.parent.joinpath(*layout.LAYOUT_ENGINE_ENTRY)
    engine.parent.mkdir(parents=True)
    engine.write_text("export default class ELK {}\n", encoding="utf-8", newline="\n")
    broken.write_text('process.stderr.write("graph 0: layout exploded\\n");\nprocess.exitCode = 1;\n', encoding="utf-8")
    monkeypatch.setattr(layout, "LAYOUT_SCRIPT", broken)
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(layout.DiagramLayoutError, match="graph 0: layout exploded"):
        diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert _snapshot(views_root) == before


def test_a_model_that_cannot_be_read_writes_nothing(tmp_path: Path) -> None:
    model_root = _copy_model(tmp_path)
    (model_root / "connectors" / "speed_link.yaml").write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(ElementParseError, match="speed_link"):
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert _snapshot(views_root) == before


@pytest.mark.parametrize("name", ["a:b", "x?y", "a b", "é", ".hidden"])
def test_a_view_whose_file_name_is_refused_writes_nothing(name: str, tmp_path: Path) -> None:
    model_root = _copy_model(tmp_path)
    (model_root / "compositions" / "Odd.yaml").write_text(
        f"name: '{name}'\ntitle: Odd\n", encoding="utf-8", newline="\n"
    )
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT, node=sys.executable)

    message = str(refused.value)
    assert f"ibd-{name}" in message
    assert "check-model" in message
    assert _snapshot(views_root) == before


def test_two_compositions_whose_names_differ_only_in_case_are_both_drawn(node: str, tmp_path: Path) -> None:
    """``check-model`` accepts the pair, so refusing it failed every build graph after the gate."""
    model_root = _copy_model(tmp_path)
    (model_root / "compositions" / "chassis~1.yaml").write_text(
        "name: chassis\ntitle: Lower-case twin\n", encoding="utf-8", newline="\n"
    )
    views_root = tmp_path / "views"

    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert {"ibd-Chassis", "ibd-chassis~1", "overview-chassis~1"} <= set(result.views)
    assert sorted(entry.name for entry in views_root.iterdir()) == sorted(_expected_files(list(result.views)))
    assert (views_root / "ibd-Chassis.svg").read_bytes() == (
        FIXTURES / "diagrams" / "svg" / "ibd-Chassis.svg"
    ).read_bytes()
    assert "<title>chassis — internal block diagram</title>" in (views_root / "ibd-chassis~1.svg").read_text(
        encoding="utf-8"
    )
    assert 'href="ibd-chassis~1.html"' in (views_root / "overview-chassis~1.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("escape", ["\\L", "\\P", "\\N"], ids=["U+2028", "U+2029", "U+0085"])
def test_a_description_holding_a_unicode_line_break_is_drawn(escape: str, node: str, tmp_path: Path) -> None:
    """``check-model`` accepts a Unicode line break, and the layout answer is never split at it.

    The YAML escape is written into the file, so the character reaches the extractor as the reader loads it.
    """
    character = {"\\L": " ", "\\P": " ", "\\N": "\x85"}[escape]
    model_root = _copy_model(tmp_path)
    vehicle = model_root / "compositions" / "Vehicle.yaml"
    source = vehicle.read_text(encoding="utf-8")
    edited = (
        source.replace("title: Vehicle\n", f'title: "Vehicle{escape}root"\n', 1)
        .replace("description: The root. One member", f'description: "The root.{escape}One member', 1)
        .replace("differs from its type.\n", 'differs from its type."\n', 1)
    )
    assert edited.count(escape) == 2, "the fixture's root composition moved; re-point the edit"
    vehicle.write_text(edited, encoding="utf-8", newline="\n")
    views_root = tmp_path / "views"

    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert "ibd-Vehicle" in result.views
    drawn = (views_root / "ibd-Vehicle.svg").read_text(encoding="utf-8")
    assert f"The root.{character}One member" in drawn


@pytest.mark.parametrize(
    ("source", "line"),
    [("compositions/Vehicle.yaml", "title: Vehicle\n"), ("sequences/speed_cycle.yaml", "title: Speed cycle\n")],
    ids=["laid-out-by-elk", "drawn-in-python"],
)
def test_text_holding_a_lone_surrogate_is_refused_by_name_and_writes_nothing(
    source: str, line: str, node: str, tmp_path: Path
) -> None:
    """A quoted YAML ``\\uD800`` is refused by name at the model read, and nothing is written.

    The escape is a surrogate no UTF-8 holds, and the YAML specification excludes it from a stream, so it never reaches
    a text path. The generator's own surrogate guard is reached through the Python API seam by the two tests after this
    one; both ids stay here, though both stop at the model read.
    """
    model_root = _copy_model(tmp_path)
    target = model_root / source
    edited = target.read_text(encoding="utf-8").replace(line, 'title: "Odd \\uD800 title"\n', 1)
    assert "\\uD800" in edited, "the fixture's title moved; re-point the edit"
    target.write_text(edited, encoding="utf-8", newline="\n")
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(ElementParseError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    assert refused.value.target == source
    message = str(refused.value)
    assert source in message
    assert str(tmp_path) not in message
    assert _snapshot(views_root) == before


# One case on purpose: the ``laid-out-by-elk`` case is the next test (its refusal precedes every process, so it runs
# without Node); the parametrisation keeps this case's test id ``[drawn-in-python]``.
@pytest.mark.parametrize(
    ("reader", "kind"),
    [("read_sequences", "drawn-in-python")],
    ids=["drawn-in-python"],
)
def test_a_lone_surrogate_the_python_api_delivers_is_refused_and_writes_nothing(
    reader: str, kind: str, node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator's own surrogate guard, reached through the seam no model file can reach.

    The reader the extractor calls hands it a sequence whose title holds U+D800, drawn by a Python grid after the
    compositions are laid out (so this needs Node); the guard refuses it, naming the code point and no location, before
    the first write.
    """
    real = getattr(graph, reader)

    def with_a_surrogate(model_root: str) -> list[object]:
        elements = real(model_root)
        assert elements, f"the fixture has no element for {kind}; re-point the seam"
        return [elements[0].model_copy(update={"title": "Odd \ud800 title"}), *elements[1:]]

    monkeypatch.setattr(graph, reader, with_a_surrogate)
    model_root = _copy_model(tmp_path)
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    message = str(refused.value)
    assert "U+D800" in message
    assert "UTF-8 cannot hold" in message
    assert str(tmp_path) not in message
    assert _snapshot(views_root) == before


def test_a_lone_surrogate_on_the_elk_path_is_refused_before_any_process_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composition's lone surrogate is refused before any process starts, and nothing is written.

    Encoded inside ``communicate()``, it was met after Node had started, and on Windows that hung. The bridge encodes
    first, so every step that could start a process refuses here; no ``node`` fixture, so it runs on a ``--skip-node``
    host too.
    """
    real = graph.read_compositions

    def with_a_surrogate(model_root: str) -> list[object]:
        elements = real(model_root)
        assert elements, "the fixture has no composition; re-point the seam"
        return [elements[0].model_copy(update={"title": "Odd \ud800 title"}), *elements[1:]]

    monkeypatch.setattr(graph, "read_compositions", with_a_surrogate)
    monkeypatch.setattr(layout, "_refuse_a_missing_engine", refuse_to_spawn)
    monkeypatch.setattr(layout, "resolve_node", refuse_to_spawn)
    monkeypatch.setattr(layout, "node_version", refuse_to_spawn)
    monkeypatch.setattr(layout.subprocess, "run", refuse_to_spawn)
    model_root = _copy_model(tmp_path)
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    message = str(refused.value)
    assert "U+D800" in message
    assert "UTF-8 cannot hold" in message
    assert str(tmp_path) not in message
    assert _snapshot(views_root) == before


def test_a_project_name_holding_a_lone_surrogate_is_refused_and_writes_nothing(node: str, tmp_path: Path) -> None:
    """The project name is a text the Python API delivers directly; the guard refuses it the same way."""
    model_root = _copy_model(tmp_path)
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError, match="U\\+DC80"):
        diagrams.generate_diagrams(str(model_root), str(views_root), "proj\udc80")

    assert _snapshot(views_root) == before


@pytest.mark.parametrize(
    ("escape", "code"),
    [("\\x01", "U+0001"), ("\\x1F", "U+001F"), ("\\x00", "U+0000"), ("\\uFFFE", "U+FFFE"), ("\\uFFFF", "U+FFFF")],
)
@pytest.mark.parametrize(
    ("source", "line"),
    [("compositions/Vehicle.yaml", "title: Vehicle\n"), ("sequences/speed_cycle.yaml", "title: Speed cycle\n")],
    ids=["laid-out-by-elk", "drawn-in-python"],
)
def test_text_holding_a_character_xml_refuses_is_refused_by_name_and_writes_nothing(
    source: str, line: str, escape: str, code: str, node: str, tmp_path: Path
) -> None:
    """``check-model`` accepts a quoted YAML ``\\x01``, which no XML 1.0 document holds: refused by code point.

    The SVG was written with the character raw, a malformed document, and the run reported success.
    """
    model_root = _copy_model(tmp_path)
    target = model_root / source
    edited = target.read_text(encoding="utf-8").replace(line, f'title: "Odd {escape} title"\n', 1)
    assert escape in edited, "the fixture's title moved; re-point the edit"
    target.write_text(edited, encoding="utf-8", newline="\n")
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    message = str(refused.value)
    assert message.startswith("no diagram was written:")
    assert code in message and "XML" in message
    assert str(tmp_path) not in message
    assert _snapshot(views_root) == before


def test_the_characters_xml_takes_that_are_not_markup_are_drawn(node: str, tmp_path: Path) -> None:
    """The refusal's reach: a tab, U+007F and U+0085 are XML 1.0 characters and are drawn, not refused."""
    model_root = _copy_model(tmp_path)
    target = model_root / "compositions" / "Vehicle.yaml"
    edited = target.read_text(encoding="utf-8").replace("title: Vehicle\n", 'title: "Odd\\t\\x7F\\N title"\n', 1)
    target.write_text(edited, encoding="utf-8", newline="\n")

    diagrams.generate_diagrams(str(model_root), str(tmp_path / "views"), PROJECT)

    assert "Odd\t\x7f\x85 title" in (tmp_path / "views" / "ibd-Vehicle.svg").read_text(encoding="utf-8")


def _entry_state(views_root: Path) -> dict[str, object]:
    """Every entry by name: a link's target, a directory's marker, a file's bytes and mtime -- no link followed."""
    state: dict[str, object] = {}
    for entry in views_root.iterdir():
        if entry.is_symlink():
            state[entry.name] = ("link", os.readlink(entry))
        elif entry.is_dir():
            state[entry.name] = ("directory",)
        else:
            state[entry.name] = (entry.read_bytes(), entry.stat().st_mtime_ns)
    return state


@pytest.mark.parametrize("name", ["ibd-Chassis.svg", "index.html", "report.txt", "ibd-chassis.svg"])
def test_a_link_at_a_file_the_run_writes_is_refused_and_nothing_is_written(
    name: str, node: str, tmp_path: Path
) -> None:
    """A link in ``views_root`` named like a produced file is refused: the write followed it and wrote outside.

    The name is compared by ``str.casefold()``, as the stale-file rule compares it: on a case-insensitive filesystem
    ``ibd-chassis.svg`` *is* ``ibd-Chassis.svg``.
    """
    views_root = _populated_views(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not the generator's\n", encoding="utf-8", newline="\n")
    (views_root / "ibd-Chassis.svg").unlink()
    (views_root / page.INDEX_PAGE).unlink()
    symlink_or_skip(views_root / name, outside)
    before = _entry_state(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    message = str(refused.value)
    assert message.startswith("no diagram was written:")
    assert repr(name) in message and "link" in message
    assert str(tmp_path) not in message
    assert outside.read_text(encoding="utf-8") == "not the generator's\n"
    assert _entry_state(views_root) == before


def test_a_directory_at_a_file_the_run_writes_is_refused_before_the_first_write(node: str, tmp_path: Path) -> None:
    """A directory named like a produced file failed the run half-way, after the files before it were written."""
    views_root = _populated_views(tmp_path)
    (views_root / page.INDEX_PAGE).unlink()
    (views_root / page.INDEX_PAGE).mkdir()
    before = _entry_state(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert repr(page.INDEX_PAGE) in str(refused.value) and "not a regular file" in str(refused.value)
    assert _entry_state(views_root) == before


def _hard_link_or_skip(link: Path, target: Path) -> None:
    """Make *link* a second name of *target*, or skip: a filesystem may refuse hard links (FAT, some shares)."""
    try:
        os.link(target, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"hard links are not available here: {exc}")


def _fresh_output(tmp_path: Path) -> dict[str, bytes]:
    fresh = tmp_path / "fresh"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(fresh), PROJECT)
    return _files(fresh)


@pytest.mark.parametrize("name", ["ibd-Chassis.svg", "index.html", "report.txt"])
def test_a_hard_link_at_a_file_the_run_writes_is_replaced_and_what_it_shares_is_untouched(
    name: str, node: str, tmp_path: Path
) -> None:
    """A hard link passes the regular-file check; the write replaces the *name*, never truncates the file.

    The other name of the old file keeps its bytes.
    """
    expected = _fresh_output(tmp_path)
    views_root = tmp_path / "views"
    views_root.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not the generator's\n", encoding="utf-8", newline="\n")
    _hard_link_or_skip(views_root / name, outside)

    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert outside.read_text(encoding="utf-8") == "not the generator's\n"
    assert name in result.written
    assert (views_root / name).read_bytes() == expected[name]
    assert not os.path.samefile(views_root / name, outside)


def _plant_after_the_check(monkeypatch: pytest.MonkeyPatch, plant) -> None:
    """Run *plant* right after the pre-write check: the window a check cannot close."""
    checked = diagrams._refuse_entries_in_the_way

    def check_then_plant(root: Path, files: dict[str, str]) -> None:
        checked(root, files)
        plant(root)

    monkeypatch.setattr(diagrams, "_refuse_entries_in_the_way", check_then_plant)


@pytest.mark.parametrize("name", ["ibd-Chassis.svg", "index.html"])
def test_a_link_made_after_the_check_is_replaced_and_not_written_through(
    name: str, node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link made after the check is replaced, not written through.

    A new file is renamed over the name, and the comparison before it reads no link.
    """
    expected = _fresh_output(tmp_path)
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not the generator's\n", encoding="utf-8", newline="\n")

    def plant(root: Path) -> None:
        (root / name).unlink()
        symlink_or_skip(root / name, outside)

    _plant_after_the_check(monkeypatch, plant)
    result = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert outside.read_text(encoding="utf-8") == "not the generator's\n"
    assert not (views_root / name).is_symlink()
    assert (views_root / name).read_bytes() == expected[name]
    assert result.written == (name,)


def test_a_fifo_made_after_the_check_is_replaced_without_blocking(
    node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO made after the check is replaced; the comparison's open never blocks on it."""
    make_fifo = getattr(os, "mkfifo", None)  # absent on Windows, which has no named pipe in a directory
    if make_fifo is None:  # pragma: no cover - platform dependent
        pytest.skip("this platform makes no FIFO in a directory")
    expected = _fresh_output(tmp_path)
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    def plant(root: Path) -> None:
        (root / "ibd-Chassis.svg").unlink()
        try:
            make_fifo(root / "ibd-Chassis.svg")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            pytest.skip(f"this filesystem makes no FIFO: {exc}")

    _plant_after_the_check(monkeypatch, plant)
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT))
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=120)

    assert not worker.is_alive(), "the generation blocked on the FIFO"
    if isinstance(outcome[0], pytest.skip.Exception):  # pragma: no cover - filesystem dependent
        raise outcome[0]
    assert isinstance(outcome[0], diagrams.DiagramsResult), outcome[0]
    assert (views_root / "ibd-Chassis.svg").read_bytes() == expected["ibd-Chassis.svg"]
    assert outcome[0].written == ("ibd-Chassis.svg",)


def test_a_rewrite_leaves_no_staging_file_behind(node: str, tmp_path: Path) -> None:
    views_root = tmp_path / "views"
    first = diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)
    (views_root / "ibd-Chassis.svg").write_text("edited\n", encoding="utf-8", newline="\n")

    diagrams.generate_diagrams(str(DIAGRAMS_MODEL), str(views_root), PROJECT)

    assert sorted(entry.name for entry in views_root.iterdir()) == sorted(first.written)


def test_a_collision_at_the_staged_name_removes_nothing_this_call_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed create never unlinks the entry already at the staged name.

    The exclusive create refuses the collision, and that file, like the target, keeps its bytes.
    """
    target = tmp_path / "ibd-Chassis.svg"
    target.write_text("old\n", encoding="utf-8", newline="\n")
    collision = tmp_path / f"{diagrams._STAGING_PREFIX}-collision.tmp"
    collision.write_text("not this call's\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(diagrams, "_staging_path", lambda _target: collision)

    with pytest.raises(FileExistsError):
        diagrams._write_by_replacing(target, "new\n", [])

    assert collision.read_text(encoding="utf-8") == "not this call's\n"
    assert target.read_text(encoding="utf-8") == "old\n"


def _long_sequence(model_root: Path, name: str, file_name: str) -> None:
    source = (model_root / "sequences" / "speed_cycle.yaml").read_text(encoding="utf-8")
    edited = source.replace("name: speed_cycle\n", f"name: {name}\n", 1)
    assert edited != source, "the fixture's name line moved; re-point the edit"
    (model_root / "sequences" / file_name).write_text(edited, encoding="utf-8", newline="\n")


def test_a_view_file_name_at_the_component_limit_is_written_and_leaves_no_staging_file(
    node: str, tmp_path: Path
) -> None:
    """The staged name must not be longer than the name it is renamed to.

    A 255-byte ``sequence-<name>.html`` fits every mainstream filesystem; the hidden staging name was 38 bytes over, so
    the rename failed after the earlier views had been replaced.
    """
    model_root = _copy_model(tmp_path)
    name = "S" * (diagrams.MAX_FILE_NAME_BYTES - len("sequence-") - len(".html"))
    _long_sequence(model_root, name, f"{name}.yaml")
    views_root = _populated_views(tmp_path)

    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)

    longest = f"sequence-{name}.html"
    assert len(longest.encode("utf-8")) == diagrams.MAX_FILE_NAME_BYTES
    assert longest in result.written
    assert not [entry.name for entry in views_root.iterdir() if entry.name.endswith(".tmp")]


@pytest.mark.parametrize("suffixed", [False, True], ids=["declared-name", "case-suffix"])
def test_a_view_file_name_over_the_component_limit_is_refused_by_name_and_writes_nothing(
    suffixed: bool, tmp_path: Path
) -> None:
    """A name no filesystem holds is refused before the first write.

    ``check-model`` holds a sequence name to no length, and the ``~<n>`` a case-only twin takes lengthens a name that
    fits by itself.
    """
    model_root = _copy_model(tmp_path)
    fits = diagrams.MAX_FILE_NAME_BYTES - len("sequence-") - len(".html")
    if suffixed:
        # Each name fits alone; the second, equal to the first but for case, takes `~1` and does not.
        name = "S" * (fits - 1)
        _long_sequence(model_root, name, f"{name}.yaml")
        twin = "s" + name[1:]
        _long_sequence(model_root, twin, f"{twin[:40]}.yaml")
        view = f"sequence-{twin}~1"
    else:
        name = "S" * (fits + 1)
        _long_sequence(model_root, name, f"{name[:40]}.yaml")
        view = f"sequence-{name}"
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT, node=sys.executable)

    message = str(refused.value)
    assert message.startswith("no diagram was written:")
    assert repr(view) in message
    assert f"at most {diagrams.MAX_FILE_NAME_BYTES}" in message
    assert str(tmp_path) not in message
    assert _snapshot(views_root) == before


@pytest.mark.parametrize("layer", ["compositions", "sequences"])
def test_a_name_declared_twice_is_refused_naming_the_view_and_writes_nothing(layer: str, tmp_path: Path) -> None:
    model_root = _copy_model(tmp_path)
    source = "Chassis.yaml" if layer == "compositions" else "speed_cycle.yaml"
    shutil.copyfile(model_root / layer / source, model_root / layer / source.replace(".yaml", "~1.yaml"))
    views_root = _populated_views(tmp_path)
    before = _snapshot(views_root)

    with pytest.raises(diagrams.DiagramGenerationError) as refused:
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT, node=sys.executable)

    message = str(refused.value)
    view = "ibd-Chassis" if layer == "compositions" else "sequence-speed_cycle"
    assert repr(view) in message and "declared more than once" in message and "check-model" in message
    assert "case" not in message, "the two names are equal, not equal but for case"
    assert _snapshot(views_root) == before


# A model with nothing to lay out


def test_a_model_with_no_elk_view_needs_no_node_and_still_writes_the_index_and_the_report(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    (model_root / "compositions").mkdir(parents=True)
    views_root = tmp_path / "views"

    # A Node that could never run: the bridge is not called for an empty graph list.
    result = diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT, node=str(tmp_path / "no-node"))

    assert result.views == ()
    assert result.written == (report.REPORT_FILE, page.INDEX_PAGE)
    assert [record.kind for record in result.not_drawn] == ["no-root"]
    assert "Not drawn: 1 in 1 kind" in (views_root / report.REPORT_FILE).read_text(encoding="utf-8")


# The package surface


#: Every name ``writer/__init__.py`` re-exported from the deleted ``c4_generation.py``.
C4_GENERATOR_NAMES = ("generate_c4", "generate_c4_cli", "_escape_c4", "_find_port", "_format_ms")


def test_the_writer_package_exports_the_generator_and_not_the_old_one() -> None:
    assert writer.generate_diagrams is diagrams.generate_diagrams
    assert "generate_diagrams" in writer.__all__
    assert [name for name in C4_GENERATOR_NAMES if name in writer.__all__ or hasattr(writer, name)] == []


def test_no_console_script_names_the_c4_generator() -> None:
    """``swcraft-generate-c4`` pointed at ``writer:generate_c4_cli``; an entry point to a gone name crashes."""
    scripts = tomllib.loads((REPO_ROOT / "packages" / "tools" / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    assert [name for name, target in scripts.items() if "c4" in name or "c4" in target] == []


# What a drawn element opens: the pages' model block and the index's elements block

#: Every start tag carrying ``data-role``, as the page script counts them: ``[data-role]`` in document order.
_ROLED_TAG = re.compile(r'<[A-Za-z][^<>]*\sdata-role="[^"]*"[^<>]*>')
_TAG_ATTRIBUTE = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)="([^"]*)"')

COLLISIONS_MODEL = FIXTURES / "diagram_collisions" / "model"

#: The models the model block is checked on, each with the group directory its components are stored in, written by hand
#: from the fixtures' trees: the oracle the extractor's lookup is compared with.
BLOCK_MODELS: dict[str, tuple[Path, dict[str, str]]] = {
    "diagrams": (
        DIAGRAMS_MODEL,
        {name: "chassis" for name in ("Sensor", "Controller", "Logger", "DiagServer")},
    ),
    "compositions": (
        FIXTURES / "compositions" / "model",
        {"Beta": "core", "Consumer": "core", "Gamma": "core", "ExternalStub": "external"},
    ),
    "collisions": (COLLISIONS_MODEL, {"SensorA": "collisions", "SensorB": "collisions"}),
}


def _drawn(svg_text: str) -> list[dict[str, str]]:
    """The attributes of every ``[data-role]`` element of *svg_text*, in document order."""
    return [
        {name: html.unescape(value) for name, value in _TAG_ATTRIBUTE.findall(tag)}
        for tag in _ROLED_TAG.findall(svg_text)
    ]


def _expected_entry(view: str, drawn: list[dict[str, str]], index: int, groups: dict[str, str]) -> dict | None:
    """What element *index* must open, restated from its own ``data-*`` attributes.

    A component's file is ``swc/<group>/<type>.yaml`` by *groups*; every other element's ``<layer>/<name>.yaml``. On
    these fixtures a lifeline's or message's selector is the component's type, so a label is a type here.
    """

    def stored(layer: str, name: str) -> str:
        return f"swc/{groups[name]}/{name}.yaml" if layer == "swc" else f"{layer}/{name}.yaml"

    def entry(layer: str, name: str, member: str | None = None) -> dict:
        return {"file": stored(layer, name), "member": member}

    element = drawn[index]
    role, name = element["data-role"], element.get("data-name", "")
    frame = next(item for item in drawn if item["data-role"] == "frame")
    kind_layers = {"component": "swc", "composition": "compositions", "task": "tasks"}
    kind_layers |= {kind: "hardware" for kind in ("processor", "core", "partition")}
    if role == "frame":
        if view.startswith("sequence-"):
            return entry("sequences", name)
        if view == "deployment":
            return entry("hardware", name)
        return entry("compositions", element.get("data-type") or name)
    if role == "member":
        layer = kind_layers[element["data-kind"]]
        return entry(layer, element["data-type"] if layer in ("swc", "compositions") else name)
    if role == "port":
        owner = element["data-owner"]
        if owner == "":
            return entry("compositions", frame["data-name"], f"ports/{name}")
        box = next(item for item in drawn if item["data-role"] == "member" and item["data-name"] == owner)
        return entry(kind_layers[box["data-kind"]], box["data-type"], f"ports/{name}")
    if role == "connector":
        return entry("connectors", name)
    if role == "delegation":
        return entry("compositions", frame["data-name"], f"delegations/{name}")
    if role == "bundle":
        return None
    if role == "lifeline":
        return entry(kind_layers[element["data-kind"]], name)
    if role == "activation":
        return entry("swc", element["data-target"], f"runnables/{name.split(graph.NOTE_SEPARATOR)[0]}")
    if role == "data":
        return entry("swc", element["data-source"], f"ports/{name.split(' -> ')[0]}")
    raise AssertionError(f"a role the provenance table has no row for: {role}")


@pytest.fixture(scope="module")
def generated_blocks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    requires_node()
    roots = {}
    for name, (model_root, _groups) in BLOCK_MODELS.items():
        views_root = tmp_path_factory.mktemp(f"blocks-{name}")
        diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)
        roots[name] = views_root
    return roots


@pytest.mark.parametrize("model", sorted(BLOCK_MODELS))
def test_every_drawn_element_opens_its_expected_entry(model: str, generated_blocks: dict[str, Path]) -> None:
    """Every ``[data-role]`` element of every view opens its expected entry, or has none.

    Each file is a model-root-relative POSIX name that exists; each ordinal is within the view's ``[data-role]`` count.
    """
    model_root, groups = BLOCK_MODELS[model]
    views_root = generated_blocks[model]
    checked = 0
    for svg_file in sorted(views_root.glob("*.svg"), key=lambda path: path.as_posix()):
        view = svg_file.stem
        drawn = _drawn(svg_file.read_text(encoding="utf-8"))
        block = _block((views_root / page.page_file(view)).read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID)
        assert all(0 <= int(ordinal) < len(drawn) for ordinal in block), view
        for index in range(len(drawn)):
            expected = _expected_entry(view, drawn, index, groups)
            assert block.get(str(index)) == expected, (view, index, drawn[index])
            if expected is not None:
                file = expected["file"]
                assert not file.startswith("/") and "\\" not in file and (model_root / file).is_file(), file
                checked += 1
    assert checked > 0


def test_the_walks_port_opens_the_port_and_its_box_opens_the_box(generated_blocks: dict[str, Path]) -> None:
    """On ``ibd-Chassis`` the ``Sensor`` box and its port ``pSpeed`` are two entries."""
    views_root = generated_blocks["diagrams"]
    drawn = _drawn((views_root / "ibd-Chassis.svg").read_text(encoding="utf-8"))
    block = _block((views_root / "ibd-Chassis.html").read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID)
    box = next(i for i, item in enumerate(drawn) if item["data-role"] == "member" and item["data-name"] == "Sensor")
    port = next(
        i
        for i, item in enumerate(drawn)
        if item["data-role"] == "port" and item["data-owner"] == "Sensor" and item["data-name"] == "pSpeed"
    )
    assert block[str(box)] == {"file": "swc/chassis/Sensor.yaml", "member": None}
    assert block[str(port)] == {"file": "swc/chassis/Sensor.yaml", "member": "ports/pSpeed"}


def test_every_colliding_element_opens_its_own_file(generated_blocks: dict[str, Path]) -> None:
    """One name, two files: told apart by ordinal, never by ``data-role`` + ``data-name``."""
    views_root = generated_blocks["collisions"]

    def entries(view: str, role: str, name: str) -> list[dict]:
        drawn = _drawn((views_root / f"{view}.svg").read_text(encoding="utf-8"))
        block = _block((views_root / page.page_file(view)).read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID)
        return [
            block[str(index)]
            for index, item in enumerate(drawn)
            if item["data-role"] == role and item.get("data-name") == name
        ]

    sensor_a = "swc/collisions/SensorA.yaml"
    sensor_b = "swc/collisions/SensorB.yaml"
    assert entries("nesting", "member", "Sensor") == [
        {"file": sensor_a, "member": None},
        {"file": sensor_b, "member": None},
    ]
    assert entries("deployment", "member", "SensorA") == [{"file": sensor_a, "member": None}] * 2
    assert entries("sequence-boot", "activation", "Init") == [
        {"file": sensor_a, "member": "runnables/Init"},
        {"file": sensor_b, "member": "runnables/Init"},
    ]
    assert entries("ibd-LeftC", "member", "Sensor") == [{"file": sensor_a, "member": None}]
    assert entries("ibd-RightC", "member", "Sensor") == [{"file": sensor_b, "member": None}]


@pytest.mark.parametrize("model", sorted(BLOCK_MODELS))
def test_the_index_block_and_the_page_blocks_agree_in_both_directions(
    model: str, generated_blocks: dict[str, Path]
) -> None:
    """Every page entry's file and member is indexed under its view, and every index row has a page entry."""
    views_root = generated_blocks[model]
    index = _block((views_root / page.INDEX_PAGE).read_text(encoding="utf-8"), page.ELEMENTS_SCRIPT_ID)
    drawn: set[tuple[str, str, str | None]] = set()
    for page_file in sorted(views_root.glob("*.html"), key=lambda path: path.as_posix()):
        if page_file.name == page.INDEX_PAGE:
            continue
        for entry in _block(page_file.read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID).values():
            if "file" in entry:
                drawn.add((page_file.stem, entry["file"], entry["member"]))
    indexed = {(view, file, None) for file, row in index.items() for view in row["views"]}
    indexed |= {
        (view, file, member)
        for file, row in index.items()
        for member, views in row["members"].items()
        for view in views
    }
    assert {(view, file) for view, file, _member in drawn} == {(view, file) for view, file, _member in indexed}
    assert {item for item in drawn if item[2] is not None} == {item for item in indexed if item[2] is not None}
    order = graph.view_ids(graph.build_graphs(str(BLOCK_MODELS[model][0])))
    for row in index.values():
        assert row["views"] == sorted(row["views"], key=order.index), "the views in the index's own order"


def test_an_identity_two_files_declare_and_one_no_file_declares_are_refusals(node: str, tmp_path: Path) -> None:
    """``Logger`` declared by two files opens nothing and says why; a member of an undeclared type says ``no-file``."""
    model_root = _copy_model(tmp_path)
    twin = model_root / "swc" / "chassis" / "LoggerTwin.yaml"
    twin.write_text(
        (model_root / "swc" / "chassis" / "Logger.yaml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    chassis = model_root / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8").replace(
            "  - name: Logger\n    type: Logger\n",
            "  - name: Logger\n    type: Logger\n  - name: Ghost\n    type: Ghost\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    views_root = tmp_path / "views"
    diagrams.generate_diagrams(str(model_root), str(views_root), PROJECT)
    drawn = _drawn((views_root / "ibd-Chassis.svg").read_text(encoding="utf-8"))
    block = _block((views_root / "ibd-Chassis.html").read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID)
    by_member = {item["data-name"]: block[str(i)] for i, item in enumerate(drawn) if item["data-role"] == "member"}
    assert by_member["Logger"] == {"refused": "several-files"}
    assert by_member["Ghost"] == {"refused": "no-file"}
    logger_port = next(
        block[str(i)] for i, item in enumerate(drawn) if item["data-role"] == "port" and item["data-owner"] == "Logger"
    )
    assert logger_port == {"refused": "several-files"}
    index = _block((views_root / page.INDEX_PAGE).read_text(encoding="utf-8"), page.ELEMENTS_SCRIPT_ID)
    assert not any("Logger" in file or "Ghost" in file for file in index), "a refusal names no file"


@pytest.mark.parametrize("golden", sorted(GOLDEN_MODELS))
def test_the_stripped_svg_is_the_renderers_plain_output_on_every_golden(golden: str, node: str) -> None:
    """Collecting the entries changes no SVG byte: each view drawn with ``sources``, stripped, is its plain call."""
    model_root, _project = GOLDEN_MODELS[golden]
    extracted = graph.build_graphs(str(model_root))
    laid_out = layout.layout(extracted.elk_graphs(), node=node)
    answers = iter(laid_out)
    pairs = []
    for item in extracted.compositions:
        ibd, overview = next(answers), next(answers)
        pairs.append((lambda s, g=ibd: svg.render_ibd(g, extracted.interface_contents, sources=s)))
        pairs.append((lambda s, g=overview: svg.render_overview(g, sources=s)))
    if extracted.nesting is not None:
        pairs.append(lambda s: nesting.render_nesting(extracted.nesting, sources=s))
    if extracted.deployment is not None:
        pairs.append(lambda s: deployment.render_deployment(extracted.deployment, sources=s))
    for view in extracted.sequences:
        pairs.append(lambda s, v=view: sequence.render_sequence(v, extracted.interface_contents, sources=s))
    for draw in pairs:
        sources: list = []
        collected = draw(sources)
        assert svg.SOURCE_ATTRIBUTE in collected and sources
        stripped, entries = page.strip_sources(collected, sources)
        assert stripped == draw(None)
        assert len(entries) == len(sources)


def _generated_texts(root: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(root.iterdir(), key=lambda path: path.as_posix())
        if path.suffix in (".svg", ".html")
    }


@pytest.mark.parametrize("model", sorted(BLOCK_MODELS))
def test_no_written_file_carries_the_temporary_attribute_or_a_style_attribute(
    model: str, generated_blocks: dict[str, Path]
) -> None:
    for name, text in _generated_texts(generated_blocks[model]).items():
        assert svg.SOURCE_ATTRIBUTE not in text, name
        assert "style=" not in text, name


# Freezing -- run as a script, never by the gate


def test_the_documented_freeze_script_starts_outside_pytest() -> None:
    """This module, run as the documented script from the repository root, imports what its tests import.

    A script run is a fresh interpreter, so its ``tests/support/`` helpers come from ``conftest.py`` alone. The run
    passes no ``--freeze`` (that rewrites the goldens and needs Node) and writes no bytecode.
    """
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = run_captured(
        [sys.executable, "packages/tools/tests/test_diagram_generation.py"], REPO_ROOT, environment, timeout=120
    )
    assert result.returncode == 0, result.stderr


def _freeze() -> None:
    for golden, (model_root, project_name) in sorted(GOLDEN_MODELS.items()):
        target = GOLDEN_DIR / golden
        result = diagrams.generate_diagrams(str(model_root), str(target), project_name)
        print(f"{golden}: {len(result.written)} written, {len(result.kept)} kept, {len(result.removed)} removed")


if __name__ == "__main__":
    if "--freeze" in sys.argv:
        _freeze()
