"""The working cycle, end to end: import, search, decide, Apply, in every order.

One test per way of taking the cycle's steps out of order, each asserting its outcome; then the ARXML
oracle (an Apply changes no ARXML byte and no value but ``satisfies``) and the import's report on the
cycle's re-import. Every step is the product's own: the requirements import's plan and
apply, :func:`~swcraft_tools.trace_search.run_trace` and
:func:`~swcraft_tools.trace_apply.run_apply`; a decision is written into the
search's own ``proposals.json`` as the panel or a hand edit would. The model is
synthetic (``trace_models.cycle_model``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_tools.cli import main
from swcraft_tools.model_state import load_model_state
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.requirements_import import (
    ImportPlan,
    apply_requirements_import,
    plan_requirements_import,
    render_report,
)
from swcraft_tools.requirements_interchange import parse_needs_text
from swcraft_tools.trace_apply import ApplyRun, run_apply
from swcraft_tools.trace_records import DECISIONS_FILE, TraceRecordsError, parse_decisions
from swcraft_tools.trace_search import (
    LOW_ACCURACY_NOTE,
    PendingDecisionsError,
    TargetAddress,
    TraceRun,
    _without_satisfies,
    run_trace,
)
from swcraft_tools.yaml_load import load_yaml_document
from trace_models import CYCLE_REQUIREMENTS, cycle_model, cycle_need, cycle_needs, tree, write_project, writer_form

PROPOSALS = "dist/experiments/trace/proposals.json"

PUMP = TargetAddress("swc", None, "Pump")
VALVE = TargetAddress("swc", None, "Valve")
P_FLOW = TargetAddress("port", "Pump", "pFlow")
P_SERVICE = TargetAddress("port", "Pump", "pService")
P_FLOW_IN = TargetAddress("port", "Valve", "pFlowIn")
RUN_PUMP = TargetAddress("runnable", "Pump", "Run_Pump")
RUN_VALVE = TargetAddress("runnable", "Valve", "Run_Valve")
I_FLOW = TargetAddress("interface", None, "IFlow")
I_SERVICE = TargetAddress("interface", None, "IService")
PLANT = TargetAddress("composition", None, "Plant")
TASK = TargetAddress("task", None, "Task10ms")
MCU = TargetAddress("processor", None, "Mcu")
CORE = TargetAddress("core", None, "Core0")
RAM = TargetAddress("memory", None, "Ram")
PARTITION = TargetAddress("partition", None, "AppPart")


class Cycle:
    """One synthetic project going round the cycle: its model, its imports, its searches and its Applies."""

    def __init__(self, tmp_path: Path, *, labelling: bool = True) -> None:
        self.dir = tmp_path / "project"
        keys: dict[str, Any] = {"labelling": "all", "labelling_share": 1.0} if labelling else {"name": "trace"}
        self.manifest, self.config = write_project(cycle_model(), self.dir, **keys)
        self.root = self.dir / "model"
        self.proposals = self.dir / PROPOSALS
        # The model's three links were made against the first delivery, so they are written after its
        # import: a link written ahead of an id's first import makes the import start that id above
        # it, as a requirement returning after a removal (the complete set's returned-id rule).
        held = {relative: self.element(relative) for relative in _CYCLE_LINK_FILES}
        for relative in held:
            self.edit(relative, lambda document, relative=relative: _cycle_links(document, relative, None))
        self.import_needs(*(cycle_need(row[0]) for row in CYCLE_REQUIREMENTS))
        for relative, document in held.items():
            links = _cycle_links(document, relative, None)
            self.edit(
                relative, lambda document, relative=relative, links=links: _cycle_links(document, relative, links)
            )
        writer_form(self.root)

    @property
    def project(self) -> SwcraftProject:
        return require_project(str(self.manifest))

    # -- the steps ---------------------------------------------------------

    def plan_import(self, *needs: dict[str, Any], baseline: str = "Baseline-1") -> ImportPlan:
        return plan_requirements_import(self.root, parse_needs_text(json.dumps(cycle_needs(*needs, baseline=baseline))))

    def import_needs(self, *needs: dict[str, Any], baseline: str = "Baseline-1") -> ImportPlan:
        plan = self.plan_import(*needs, baseline=baseline)
        apply_requirements_import(plan)
        return plan

    def search(self, *, discard_pending: bool = False) -> TraceRun:
        return run_trace(self.project, self.config, discard_pending=discard_pending)

    def apply(self, *, dry_run: bool = False) -> ApplyRun:
        return run_apply(self.project, self.proposals, dry_run=dry_run)

    # -- the proposals file ------------------------------------------------

    def read(self) -> dict[str, Any]:
        return json.loads(self.proposals.read_text(encoding="utf-8"))

    def decide(self, *decisions: tuple[str, TargetAddress, str]) -> None:
        """Write each ``(requirement, target, decision)`` into the row the search proposed, as the panel would."""
        document = self.read()
        for identifier, address, decision in decisions:
            row = next(
                row
                for entry in document["requirements"]
                if entry["id"] == identifier
                for row in entry["candidates"]
                if row["target"] == address.to_json()
            )
            row["decision"] = decision
        self.proposals.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")

    def entry(self, identifier: str, document: dict[str, Any] | None = None) -> dict[str, Any] | None:
        entries = (document or self.read())["requirements"]
        return next((entry for entry in entries if entry["id"] == identifier), None)

    # -- the model ---------------------------------------------------------

    def element(self, relative: str) -> dict[str, Any]:
        return load_yaml_document((self.root / relative).read_text(encoding="utf-8"))

    def edit(self, relative: str, change: Any) -> None:
        path = self.root / relative
        document = load_yaml_document(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    def satisfies(self, relative: str, *path: str | int) -> list[str]:
        """The ``satisfies`` of the element at *relative*, or of the member *path* names inside it."""
        holder: Any = self.element(relative)
        for step in path:
            holder = holder[step]
        return list(holder.get("satisfies", []))


#: The cycle model's files holding a link (``trace_models.cycle_model``), and where inside each.
_CYCLE_LINK_FILES: dict[str, tuple[str | int, ...]] = {
    "swc/app/Pump.yaml": (),
    "swc/app/Valve.yaml": ("ports", 0),
    "tasks/Task10ms.yaml": (),
}


def _cycle_links(document: dict[str, Any], relative: str, links: list[str] | None) -> list[str]:
    """Set *relative*'s link holder's ``satisfies`` in *document* to *links*, ``None`` removing it; the old links."""
    holder = _holder(document, _CYCLE_LINK_FILES[relative])
    old = list(holder.pop("satisfies", []))
    if links is not None:
        holder["satisfies"] = links
    return old


@pytest.fixture
def cycle(tmp_path: Path) -> Cycle:
    return Cycle(tmp_path)


def _counts(run: ApplyRun) -> dict[str, int]:
    """The ``Summary:`` line's counts by name, *undecided* left out: the rows nobody decided are the search's."""
    line = next(line for line in run.report.splitlines() if line.startswith("Summary: "))
    decisions, files = line.removeprefix("Summary: ").split("; ")
    counts = {name: int(count) for count, name in (part.split(" ", 1) for part in decisions.split(", "))}
    del counts["undecided"]
    return {**counts, "files": int(files.removesuffix(" files"))}


# ---------------------------------------------------------------------------
# The out-of-order table, one test per row
# ---------------------------------------------------------------------------


class TestOutOfOrder:
    def test_import_after_labelling_before_apply(self, cycle: Cycle) -> None:
        """An unchanged requirement's link is written; a changed or removed one is stale, named; a new one waits."""
        cycle.search()
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PL-3", VALVE, "accept"), ("PL-4", PLANT, "accept"))
        cycle.import_needs(
            *(cycle_need(row[0]) for row in CYCLE_REQUIREMENTS if row[0] not in {"PL-3", "PL-4"}),
            cycle_need("PL-3", content="The Valve shall limit the coolant flow to 650 l/h."),
            cycle_need("PL-4", status="rejected"),
            {
                "id": "PL-6",
                "title": "Pump stop",
                "content": "The Pump stops on a fault.",
                "type": "req",
                "document": "Plant Spec",
            },
        )
        run = cycle.apply()
        assert _counts(run) == {
            "written": 1,
            "confirmed": 0,
            "rejected": 0,
            "already done": 0,
            "stale": 2,
            "not written": 0,
            "files": 1,
        }
        assert cycle.satisfies("swc/app/Pump.yaml", "ports", 1) == ["PL-2@1"]
        assert "satisfies" not in cycle.element("swc/app/Valve.yaml")
        assert "satisfies" not in cycle.element("compositions/Plant.yaml")
        stale = {
            entry["id"]: row["stale"]
            for entry in cycle.read()["requirements"]
            for row in entry["candidates"]
            if "stale" in row
        }
        assert stale["PL-3"] == "PL-3 changed since the search (now version 2)"
        assert stale["PL-4"] == "PL-4 is no longer in the model"
        assert "PL-3 on SWC 'Valve': PL-3 changed since the search (now version 2); search again" in run.report
        assert "PL-4 on Composition 'Plant': PL-4 is no longer in the model; search again" in run.report
        assert cycle.entry("PL-6") is None  # a new requirement waits for the next search

        # The stale items are marked, so a new search is not refused and proposes PL-3 afresh, at its new version.
        cycle.search()
        renewed = cycle.entry("PL-3")
        assert renewed is not None and renewed["version"] == 2
        assert any(row["target"] == VALVE.to_json() for row in renewed["candidates"])
        assert cycle.entry("PL-6") is not None

    def test_import_after_labelling_and_apply(self, cycle: Cycle) -> None:
        """The links stay; the changed requirement's link awaits confirmation, and a confirm moves it."""
        cycle.search()
        cycle.decide(("PL-3", VALVE, "accept"), ("PL-2", P_SERVICE, "accept"))
        cycle.apply()
        imported = cycle.import_needs(
            *(cycle_need(row[0]) for row in CYCLE_REQUIREMENTS if row[0] != "PL-3"),
            cycle_need("PL-3", content="The Valve shall limit the coolant flow to 650 l/h."),
        )
        assert imported.links_needing_confirmation == ("SWC 'Valve' satisfies 'PL-3@1'",)
        assert cycle.satisfies("swc/app/Valve.yaml") == ["PL-3@1"]
        assert cycle.satisfies("swc/app/Pump.yaml", "ports", 1) == ["PL-2@1"]

        cycle.search()  # every earlier decision was applied: not refused
        entry = cycle.entry("PL-3")
        assert entry is not None
        (link,) = entry["linked"]
        assert link == {
            "target": VALVE.to_json(),
            "member": "PL-3@1",
            "version": 1,
            "confirmed": False,
            "decision": None,
        }
        document = cycle.read()
        cycle.entry("PL-3", document)["linked"][0]["decision"] = "confirm"  # type: ignore[index]
        cycle.proposals.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
        run = cycle.apply()
        assert "SWC 'Valve' satisfies 'PL-3@1' → 'PL-3@2'" in run.report
        assert cycle.satisfies("swc/app/Valve.yaml") == ["PL-3@2"]
        records = parse_decisions((cycle.dir / DECISIONS_FILE).read_text(encoding="utf-8")).decisions
        confirm = next(record for record in records if record.decision == "confirm")
        assert (confirm.requirement, confirm.version, confirm.from_) == ("PL-3", 2, "PL-3@1")

    def test_two_labellings_without_apply(self, cycle: Cycle) -> None:
        """The second search refuses with the pending count, and writes nothing."""
        cycle.search()
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PL-4", PLANT, "accept"), ("PF-3", RAM, "reject"))
        before = tree(cycle.dir)
        with pytest.raises(PendingDecisionsError) as caught:
            cycle.search()
        assert caught.value.count == 3
        assert str(caught.value) == (
            f"`{PROPOSALS}` holds 3 decisions not applied. "
            "Apply them with `--apply`, or search again with `--discard-pending`."
        )
        assert tree(cycle.dir) == before

    def test_two_labellings_with_apply_between(self, cycle: Cycle) -> None:
        """The second labelling puts the requirements still unlinked first, and a rejected pair is not proposed.

        Every requirement is listed; the ones to label first carry their reason.
        """
        first = cycle.search()
        assert {entry["id"] for entry in first.document["requirements"] if entry["chosenBecause"]} == {
            "PL-2", "PL-3", "PL-4", "PL-5", "PF-3", "PF-4", "PF-5",
        }  # fmt: skip
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PL-4", PLANT, "accept"), ("PF-3", RAM, "reject"))
        cycle.apply()
        second = cycle.search()
        listed = {entry["id"] for entry in second.document["requirements"] if entry["chosenBecause"]}
        assert listed == {"PL-3", "PL-5", "PF-3", "PF-4", "PF-5"}
        rejected = next(entry for entry in second.document["requirements"] if entry["id"] == "PF-3")
        assert rejected["rejectedEarlier"] == 1
        assert RAM.to_json() not in [row["target"] for row in rejected["candidates"]]
        assert "1 candidate rejected earlier, not proposed" in second.report_path.read_text(encoding="utf-8")

    def test_review_without_labelling(self, tmp_path: Path) -> None:
        """Allowed: a document no link reaches yet is reviewed, its requirements marked low accuracy."""
        cycle = Cycle(tmp_path, labelling=False)
        for relative, path in (("swc/app/Pump.yaml", ()), ("swc/app/Valve.yaml", ("ports", 0))):
            cycle.edit(relative, lambda d, path=path: _holder(d, path).pop("satisfies"))
        run = cycle.search()  # the Plant document holds no link: nothing has been labelled
        plant = [entry for entry in run.document["requirements"] if entry["document"] == "Plant Spec"]
        assert {entry["id"] for entry in plant} == {"PL-1", "PL-2", "PL-3", "PL-4", "PL-5"}
        assert {entry["pass"] for entry in plant} == {"review"}
        assert all(entry["lowAccuracy"] for entry in plant)
        lines = run.report_path.read_text(encoding="utf-8").splitlines()
        line = next(line for line in lines if line.startswith("- **") and "PL-2" in line)
        assert line.endswith(f"(version 1) — *{LOW_ACCURACY_NOTE}*")

    def test_an_element_renamed_or_deleted_between_the_search_and_apply(self, cycle: Cycle) -> None:
        """Neither link is written, and the report says why."""
        cycle.search()
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PL-5", RUN_VALVE, "accept"))
        cycle.edit("swc/app/Pump.yaml", lambda d: d["ports"][1].update(name="pServiceRenamed"))
        cycle.edit("swc/app/Valve.yaml", lambda d: d.update(runnables=[]))
        before = tree(cycle.root)
        run = cycle.apply()
        assert tree(cycle.root) == before
        assert _counts(run) == {
            "written": 0,
            "confirmed": 0,
            "rejected": 0,
            "already done": 0,
            "stale": 2,
            "not written": 0,
            "files": 0,
        }
        assert (
            "PL-2 on SWC 'Pump' (port 'pService'): port 'pService' of SWC 'Pump' is no longer in the model; "
            "search again"
        ) in run.report
        assert (
            "PL-5 on SWC 'Valve' (runnable 'Run_Valve'): runnable 'Run_Valve' of SWC 'Valve' is no longer in the "
            "model; search again"
        ) in run.report

    def test_apply_twice_over_one_file(self, cycle: Cycle) -> None:
        """The second Apply writes nothing."""
        cycle.search()
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PF-3", RAM, "reject"))
        assert cycle.apply().written == ("model/swc/app/Pump.yaml", DECISIONS_FILE, PROPOSALS)
        before = tree(cycle.dir)
        second = cycle.apply()
        assert second.written == ()
        assert tree(cycle.dir) == before
        assert _counts(second) == {
            "written": 0,
            "confirmed": 0,
            "rejected": 0,
            "already done": 2,
            "stale": 0,
            "not written": 0,
            "files": 0,
        }
        assert "No change." in second.report
        assert cycle.satisfies("swc/app/Pump.yaml", "ports", 1) == ["PL-2@1"]

    def test_a_proposals_file_broken_by_a_hand_edit(self, cycle: Cycle) -> None:
        """Apply refuses, naming the place, and writes nothing -- the decided items included."""
        cycle.search()
        cycle.decide(("PL-2", P_SERVICE, "accept"), ("PL-4", PLANT, "acept"))
        before = tree(cycle.dir)
        for dry_run in (True, False):
            with pytest.raises(TraceRecordsError) as caught:
                cycle.apply(dry_run=dry_run)
            index = next(i for i, entry in enumerate(cycle.read()["requirements"]) if entry["id"] == "PL-4")
            row = next(
                i
                for i, row in enumerate(cycle.read()["requirements"][index]["candidates"])
                if row["decision"] == "acept"
            )
            assert caught.value.pointer == f"/requirements/{index}/candidates/{row}/decision"
        assert tree(cycle.dir) == before


def _holder(document: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    holder: Any = document
    for step in path:
        holder = holder[step]
    return holder


# ---------------------------------------------------------------------------
# The ARXML oracle
# ---------------------------------------------------------------------------

#: An accept on every kind of target, each on a requirement the cycle proposes it for.
EVERY_KIND = (
    ("PL-2", PUMP),
    ("PL-2", P_SERVICE),
    ("PL-2", RUN_PUMP),
    ("PL-2", I_SERVICE),
    ("PL-3", VALVE),
    ("PL-3", RUN_VALVE),
    ("PL-4", PLANT),
    ("PL-5", I_FLOW),
    ("PL-5", P_FLOW),
    ("PF-3", RAM),
    ("PF-3", TASK),
    ("PF-4", CORE),
    ("PF-5", MCU),
    ("PF-5", PARTITION),
)


def _generate_arxml(cycle: Cycle, monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(cycle.manifest)])
    main()
    return (cycle.dir / "dist" / "model.arxml").read_bytes()


def _values(cycle: Cycle) -> dict[str, Any]:
    """Every element of the model, ``satisfies`` left out at every depth, by its layer and its file's order."""
    state = load_model_state(cycle.root)
    return {
        layer: [_without_satisfies(element.model_dump(mode="json")) for element in elements]
        for layer, elements in state.layers.items()
    }


def test_an_apply_on_every_kind_moves_no_arxml_byte_and_no_value_but_satisfies(
    cycle: Cycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert {address.kind for _identifier, address in EVERY_KIND} == {
        "swc", "port", "runnable", "interface", "composition", "task", "processor", "core", "memory", "partition",
    }  # fmt: skip
    before = _generate_arxml(cycle, monkeypatch)
    values = _values(cycle)
    cycle.search()
    cycle.decide(*((identifier, address, "accept") for identifier, address in EVERY_KIND))
    run = cycle.apply()
    assert _counts(run) == {
        "written": 14,
        "confirmed": 0,
        "rejected": 0,
        "already done": 0,
        "stale": 0,
        "not written": 0,
        "files": 10,
    }
    assert cycle.satisfies("interfaces/IService.yaml") == ["PL-2@1"]  # a client-server interface
    assert cycle.satisfies("interfaces/IFlow.yaml") == ["PL-5@1"]  # and a sender-receiver one
    assert cycle.satisfies("hardware/AppPart.yaml") == ["PF-5@1"]
    assert _generate_arxml(cycle, monkeypatch) == before
    assert not (cycle.dir / "dist" / "model_old.arxml").exists()
    assert _values(cycle) == values


# ---------------------------------------------------------------------------
# The import's report on the cycle's re-import
# ---------------------------------------------------------------------------


def test_the_reimports_report_states_what_changed_line_by_line(cycle: Cycle) -> None:
    cycle.search()
    cycle.decide(("PL-3", VALVE, "accept"))
    cycle.apply()
    plan = cycle.plan_import(
        *(cycle_need(row[0]) for row in CYCLE_REQUIREMENTS if row[0] != "PL-3"),
        cycle_need("PL-3", content="The Valve shall cap the flow to 650 l/h.", asil="B"),
    )
    report = render_report(plan, dry_run=True)
    changed = report.split("\n\n")[1]
    assert changed == (
        "Changed:\n"
        "  PL-3 v1 → v2 (description, asil)\n"
        "    text: numbers 600 → 650; terms added: cap; terms removed: coolant, limit\n"
        "    ASIL set: B"
    )
    assert "Links now needing confirmation:\n  SWC 'Valve' satisfies 'PL-3@1'\n" in report
