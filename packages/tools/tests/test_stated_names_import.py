"""Contract ``8.0.0``: the importers state the names the source gives.

A source's internal behaviour, event and read, write or call point keeps its
``SHORT-NAME`` when it differs from the name the writer derives: the importer
states it (``behavior_name``, an event's or an access entry's ``name``) in one
settling pass over the **final** model -- after the reserved names are
reconciled, so a renamed component's derived behaviour name is the one
compared -- run by ``import model`` and ``import component`` alike
(``swc_mapping.settle_stated_names``). A name equal to the derived one states
nothing.

A source name is **not** stated, and keeps family F19's line with its reason,
when it is not a legal AUTOSAR identifier of at most 128 characters, or when it
would collide, ignoring case (``[constr_2508]``), within its scope -- the
behaviour's events and runnables, a runnable's access points, the component's
ports for the behaviour -- with a name that stays: a runnable's, a port's, the
derived name of an element written with it, or another candidate the source
names alike. Declining restores a derived name, so the pass repeats until no
candidate is declined; a swap of two derived names is no collision.

Every ``import model`` here is run through the name and identifier checks
(``_assert_no_name_finding``), so the settling pass and the rules it twins cannot
drift apart unseen. Every name in this module is invented. What it does **not**
reach: real tool exports.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from behaviour_losses_support import NS, snippet
from conftest import ACCESS_POINTS_MODEL_ROOT
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_reader import SkipCategory, SkipEntry
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.component_exchange import export_component, import_component
from swcraft_tools.validator import check_cross_layer_consistency, check_identifier_conformance, check_name_uniqueness
from swcraft_tools.writer import generate_arxml

ACCESS = "/ComponentTypes/AccessSwc"
BEHAVIOUR = f"{ACCESS}/AccessSwc_InternalBehavior"
SERVER_BEHAVIOUR = "/ComponentTypes/ServerSwc/ServerSwc_InternalBehavior"
MAIN = f"{BEHAVIOUR}/AccessMain"

#: The family F19 mark (``report_family_support._FAMILY_MARKS``).
F19 = "SWCraft names it itself"
COLLIDES = "because the source's name would collide with"
INADMISSIBLE = "because the source's name is not a legal AUTOSAR Identifier"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model(tmp_path: Path) -> Path:
    """``access_points`` with an init event on ``QuietRun`` and an operation event on ``ServerMain``."""
    root = tmp_path / "model"
    shutil.copytree(ACCESS_POINTS_MODEL_ROOT, root)
    for name, runnable, event in (
        ("AccessSwc", "QuietRun", {"type": "init"}),
        ("ServerSwc", "ServerMain", {"type": "operation", "port": "pControl", "operation": "Apply"}),
    ):
        path = root / "swc" / "access" / f"{name}.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        (target,) = [r for r in document["runnables"] if r["name"] == runnable]
        target["events"].append(event)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """SWCraft's own whole-model export of :func:`_model`: every name the writer derives."""
    folder = tmp_path_factory.mktemp("stated_names")
    out = folder / "model.arxml"
    generate_arxml(str(_model(folder)), str(out))
    return out


def _element(root: etree._Element, path: str) -> etree._Element:
    """The element whose ``SHORT-NAME`` path is *path* (the last segment's, matched in its parent's subtree)."""
    parent_path, _, name = path.rpartition("/")
    for candidate in root.iter():
        if isinstance(candidate.tag, str) and candidate.findtext(f"{NS}SHORT-NAME") == name:
            if _path_of(candidate) == path:
                return candidate
    raise AssertionError(f"no element at {path} (parent {parent_path})")


def _path_of(element: etree._Element) -> str:
    names = []
    node = element
    while node is not None:
        name = node.findtext(f"{NS}SHORT-NAME") if isinstance(node.tag, str) else None
        if name is not None:
            names.append(name)
        node = node.getparent()
    return "/" + "/".join(reversed(names))


def _rename(root: etree._Element, path: str, name: str) -> None:
    """Give the element at *path* the ``SHORT-NAME`` *name*, and every reference to it or below it follows."""
    _element(root, path).find(f"{NS}SHORT-NAME").text = name
    renamed = f"{path.rpartition('/')[0]}/{name}"
    for node in root.iter():
        if isinstance(node.tag, str) and node.text and (node.text == path or node.text.startswith(f"{path}/")):
            node.text = renamed + node.text[len(path) :]


def _second_timing_event(root: etree._Element, name: str) -> None:
    """A second timing event on ``AccessMain``, last in ``EVENTS``: the derived names become ``TMT_AccessMain_0/_1``."""
    (event,) = snippet(
        f"<TIMING-EVENT><SHORT-NAME>{name}</SHORT-NAME>"
        f'<START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">{MAIN}</START-ON-EVENT-REF>'
        "<PERIOD>0.1</PERIOD></TIMING-EVENT>"
    )
    _element(root, BEHAVIOUR).find(f"{NS}EVENTS").append(event)


def _patched(source: Path, tmp_path: Path, mutate) -> Path:
    tree = etree.parse(str(source))
    if mutate is not None:
        mutate(tree.getroot())
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "source.arxml"
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


def _import(source: Path, tmp_path: Path, mutate=None) -> tuple[Path, list[SkipEntry]]:
    """``import model`` of *source* after *mutate(root)*: the written model root and the report's entries."""
    patched = _patched(source, tmp_path, mutate)
    project = init_project(tmp_path / "project")
    report = run_import(str(patched), str(project / "swcraft.yaml"))
    _assert_no_name_finding(project / "model")
    return project / "model", list(report.skip_entries)


def _assert_no_name_finding(model_root: Path) -> None:
    """The settling pass never hands ``check-model`` a name finding it made itself.

    The pass is a hand-written twin of four rules -- ``name.name-space-collision``,
    ``name.runnable-event-collision``, ``name.duplicate-event`` and
    ``swc.access.name-collision`` -- and of ``identifier.declared`` on the names it
    states, so every import of this module is run through the checks that hold them.
    """
    records = [
        *check_name_uniqueness(str(model_root)).records,
        *check_identifier_conformance(str(model_root)).records,
        *check_cross_layer_consistency(str(model_root)).records,
    ]
    named = [
        (record.rule, record.message)
        for record in records
        if record.rule.startswith(("name.", "identifier.")) or record.rule == "swc.access.name-collision"
    ]
    assert named == [], named


def _document(model_root: Path, name: str) -> dict:
    (path,) = model_root.glob(f"swc/**/{name}.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _stated(model_root: Path, name: str = "AccessSwc") -> dict[str, object]:
    """Every name *name* states, by where: ``behavior_name``, ``{runnable}/events/{i}``, ``{runnable}/{list}/{i}``."""
    document = _document(model_root, name)
    stated: dict[str, object] = {}
    if "behavior_name" in document:
        stated["behavior_name"] = document["behavior_name"]
    for runnable in document["runnables"]:
        for collection in ("events", "reads", "writes", "calls"):
            for index, entry in enumerate(runnable.get(collection, [])):
                if "name" in entry:
                    stated[f"{runnable['name']}/{collection}/{index}"] = entry["name"]
    return stated


def _f19(entries: list[SkipEntry]) -> list[tuple[str, str]]:
    """Family F19's lines as ``(path, reason)``."""
    return sorted((entry.path, entry.reason) for entry in entries if entry.tag == "SHORT-NAME" and F19 in entry.reason)


def _short_names(arxml: Path, *owners: str) -> list[str]:
    """Every ``SHORT-NAME`` below each of *owners* (paths), in document order."""
    root = etree.parse(str(arxml)).getroot()
    names: list[str] = []
    for owner in owners:
        element = _element(root, owner)
        names.extend(node.text for node in element.iter(f"{NS}SHORT-NAME"))
    return names


# ---------------------------------------------------------------------------
# import model: stated where the source differs, and written back
# ---------------------------------------------------------------------------


class TestImportModelStatesTheNames:
    def test_swcrafts_own_names_state_nothing_and_report_nothing(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _import(source, tmp_path)
        assert _stated(model_root) == {}
        assert _stated(model_root, "ServerSwc") == {}
        assert _f19(entries) == []

    def test_every_differing_name_is_stated_and_written_as_the_source_wrote_it(
        self, source: Path, tmp_path: Path
    ) -> None:
        def mutate(root: etree._Element) -> None:
            for path, name in (
                (f"{BEHAVIOUR}/TMT_AccessMain", "Tick10ms"),
                (f"{BEHAVIOUR}/IT_QuietRun", "StartUp"),
                (f"{MAIN}/R_rSignals_speed", "ReadSpeed"),
                (f"{MAIN}/W_pSignals_speed", "WriteSpeed"),
                (f"{MAIN}/Call_rControl_Apply", "CallApply"),
                (f"{SERVER_BEHAVIOUR}/OIT_pControl_Apply", "OnApply"),
                (BEHAVIOUR, "AccessBehaviour"),
            ):
                _rename(root, path, name)

        model_root, entries = _import(source, tmp_path, mutate)

        assert _f19(entries) == []
        assert _stated(model_root) == {
            "behavior_name": "AccessBehaviour",
            "AccessMain/events/0": "Tick10ms",
            "AccessMain/reads/0": "ReadSpeed",
            "AccessMain/writes/0": "WriteSpeed",
            "AccessMain/calls/0": "CallApply",
            "QuietRun/events/1": "StartUp",
        }
        assert _stated(model_root, "ServerSwc") == {"ServerMain/events/1": "OnApply"}

        # Written back: every SHORT-NAME below the two behaviours is the source's, the
        # unstated ones (`TMT_QuietRun`, `R_rSignals_level`, `TMT_ServerMain`) derived.
        patched = tmp_path / "source.arxml"
        for version in ArxmlVersion:
            output = tmp_path / f"out-{version}.arxml"
            generate_arxml(str(model_root), str(output), version=version)
            owners = (f"{ACCESS}/AccessBehaviour", SERVER_BEHAVIOUR)
            assert sorted(_short_names(output, *owners)) == sorted(_short_names(patched, *owners))

    def test_a_name_differing_from_the_derived_one_in_case_only_is_stated(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _import(
            source, tmp_path, lambda root: _rename(root, f"{BEHAVIOUR}/TMT_QuietRun", "tmt_quietrun")
        )
        assert _stated(model_root) == {"QuietRun/events/0": "tmt_quietrun"}
        assert _f19(entries) == []

    @pytest.mark.parametrize(
        ("name", "why"),
        [("Tick 10ms", "AUTOSAR Identifier matching"), ("T" * 129, "at most 128 characters")],
        ids=["pattern", "length"],
    )
    def test_a_name_that_is_not_an_identifier_is_declined_with_its_line(
        self, source: Path, tmp_path: Path, name: str, why: str
    ) -> None:
        model_root, entries = _import(source, tmp_path, lambda root: _rename(root, f"{BEHAVIOUR}/TMT_QuietRun", name))
        assert _stated(model_root) == {}
        ((path, reason),) = _f19(entries)
        assert path == f"{BEHAVIOUR}/{name}"
        assert INADMISSIBLE in reason
        assert why in reason
        (entry,) = [e for e in entries if e.tag == "SHORT-NAME" and F19 in e.reason]
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "report")


# ---------------------------------------------------------------------------
# The settlement
# ---------------------------------------------------------------------------


class TestTheSettlement:
    def test_a_stated_name_equal_to_a_runnables_is_declined(self, source: Path, tmp_path: Path) -> None:
        """Equal ignoring case: an exact repeat of the runnable's path is the reader's duplicate, dropped before."""
        model_root, entries = _import(
            source, tmp_path, lambda root: _rename(root, f"{BEHAVIOUR}/TMT_QuietRun", "QUIETRUN")
        )
        assert _stated(model_root) == {}
        ((path, reason),) = _f19(entries)
        assert path == f"{BEHAVIOUR}/QUIETRUN"
        assert f"{F19}, {COLLIDES} runnable 'QuietRun'" in reason

    def test_the_cascade_declines_both(self, source: Path, tmp_path: Path) -> None:
        """The cascade declines both derived event names.

        Derived ``TMT_AccessMain_0`` / ``_1``; the second's source name is the runnable's (ignoring case), so it is
        declined and its derived ``TMT_AccessMain_1`` reserved, which the first's source name is: declined next."""

        def mutate(root: etree._Element) -> None:
            _rename(root, f"{BEHAVIOUR}/TMT_AccessMain", "TMT_AccessMain_1")
            _second_timing_event(root, "ACCESSMAIN")

        model_root, entries = _import(source, tmp_path, mutate)
        assert _stated(model_root) == {}
        lines = dict(_f19(entries))
        assert set(lines) == {f"{BEHAVIOUR}/TMT_AccessMain_1", f"{BEHAVIOUR}/ACCESSMAIN"}
        assert f"{COLLIDES} runnable 'AccessMain'" in lines[f"{BEHAVIOUR}/ACCESSMAIN"]
        assert "'TMT_AccessMain_1'" in lines[f"{BEHAVIOUR}/TMT_AccessMain_1"].split(COLLIDES)[1]

    def test_a_swap_of_two_derived_names_is_stated(self, source: Path, tmp_path: Path) -> None:
        def mutate(root: etree._Element) -> None:
            _rename(root, f"{BEHAVIOUR}/TMT_AccessMain", "TMT_AccessMain_1")
            _second_timing_event(root, "TMT_AccessMain_0")

        model_root, entries = _import(source, tmp_path, mutate)
        assert _f19(entries) == []
        assert _stated(model_root) == {
            "AccessMain/events/0": "TMT_AccessMain_1",
            "AccessMain/events/1": "TMT_AccessMain_0",
        }

    def test_a_group_the_source_names_alike_is_declined_whole(self, source: Path, tmp_path: Path) -> None:
        def mutate(root: etree._Element) -> None:
            _rename(root, f"{BEHAVIOUR}/TMT_AccessMain", "Tick")
            _rename(root, f"{BEHAVIOUR}/TMT_QuietRun", "TICK")

        model_root, entries = _import(source, tmp_path, mutate)
        assert _stated(model_root) == {}
        lines = dict(_f19(entries))
        assert set(lines) == {f"{BEHAVIOUR}/Tick", f"{BEHAVIOUR}/TICK"}
        assert all(COLLIDES in reason and "the same name" in reason for reason in lines.values())

    def test_a_behaviour_name_equal_to_a_port_ignoring_case_is_declined(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _import(source, tmp_path, lambda root: _rename(root, BEHAVIOUR, "RSIGNALS"))
        assert _stated(model_root) == {}
        ((path, reason),) = _f19(entries)
        assert path == f"{ACCESS}/RSIGNALS"
        assert f"{COLLIDES} port 'rSignals'" in reason

    def test_an_access_point_named_like_another_ones_derived_name_is_declined(
        self, source: Path, tmp_path: Path
    ) -> None:
        model_root, entries = _import(
            source, tmp_path, lambda root: _rename(root, f"{MAIN}/R_rSignals_speed", "w_psignals_speed")
        )
        assert _stated(model_root) == {}
        ((path, reason),) = _f19(entries)
        assert path == f"{MAIN}/w_psignals_speed"
        assert f"{COLLIDES} the access point SWCraft writes as 'W_pSignals_speed'" in reason

    def test_two_access_points_the_source_names_alike_are_declined_and_a_third_stated(
        self, source: Path, tmp_path: Path
    ) -> None:
        def mutate(root: etree._Element) -> None:
            _rename(root, f"{MAIN}/R_rSignals_speed", "Access")
            _rename(root, f"{MAIN}/W_pSignals_speed", "ACCESS")
            _rename(root, f"{MAIN}/Call_rControl_Apply", "CallApply")

        model_root, entries = _import(source, tmp_path, mutate)
        assert _stated(model_root) == {"AccessMain/calls/0": "CallApply"}
        assert {path for path, _reason in _f19(entries)} == {f"{MAIN}/Access", f"{MAIN}/ACCESS"}

    def test_the_pass_runs_after_a_reserved_name_renames_the_component(self, source: Path, tmp_path: Path) -> None:
        """The pass runs after the reserved-name pass renames the component.

        ``ServerSwc`` renamed in the source to ``AccessSwc_Implementation``, which the writer reserves beside
        ``AccessSwc``: the reserved-name pass renames it, so the derived behaviour name changes, and the source's
        ``AccessSwc_Implementation_InternalBehavior`` -- the derived one before the rename -- is stated."""

        def mutate(root: etree._Element) -> None:
            for implementation in list(root.iter(f"{NS}SWC-IMPLEMENTATION")):
                implementation.getparent().remove(implementation)
            for node in root.iter():
                if isinstance(node.tag, str) and node.text and "ServerSwc" in node.text:
                    node.text = node.text.replace("ServerSwc", "AccessSwc_Implementation")

        model_root, entries = _import(source, tmp_path, mutate)
        stated = {
            document["name"]: document.get("behavior_name")
            for document in (yaml.safe_load(p.read_text(encoding="utf-8")) for p in model_root.glob("swc/**/*.yaml"))
        }
        (renamed,) = [name for name in stated if name.startswith("AccessSwc_Implementation")]
        assert renamed != "AccessSwc_Implementation"
        assert stated[renamed] == "AccessSwc_Implementation_InternalBehavior"
        assert stated["AccessSwc"] is None
        assert _f19(entries) == []


# ---------------------------------------------------------------------------
# import component: the same pass
# ---------------------------------------------------------------------------


def _component_export(tmp_path: Path, mutate) -> Path:
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(_model(tmp_path)), "AccessSwc", str(arxml))
    tree = etree.parse(str(arxml))
    mutate(tree.getroot())
    arxml.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return arxml


class TestImportComponent:
    def test_import_component_states_a_differing_name(self, tmp_path: Path) -> None:
        def mutate(root: etree._Element) -> None:
            _rename(root, f"{BEHAVIOUR}/TMT_AccessMain", "Tick10ms")
            _rename(root, f"{MAIN}/Call_rControl_Apply", "CallApply")
            _rename(root, BEHAVIOUR, "AccessBehaviour")

        arxml = _component_export(tmp_path, mutate)
        project = init_project(tmp_path / "proj")
        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert not [line for line in report.not_carried_lines if F19 in line]
        assert _stated(project / "model") == {
            "behavior_name": "AccessBehaviour",
            "AccessMain/events/0": "Tick10ms",
            "AccessMain/calls/0": "CallApply",
        }

    def test_import_component_prints_a_declined_name_and_imports_past_it(self, tmp_path: Path) -> None:
        arxml = _component_export(tmp_path, lambda root: _rename(root, f"{BEHAVIOUR}/TMT_QuietRun", "QUIETRUN"))
        project = init_project(tmp_path / "proj")
        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        (line,) = [line for line in report.not_carried_lines if F19 in line]
        assert f"{COLLIDES} runnable 'QuietRun'" in line
        assert _stated(project / "model") == {}
