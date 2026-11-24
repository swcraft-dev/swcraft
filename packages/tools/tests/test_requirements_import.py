"""The requirements import's planner and its report (``swcraft_tools.requirements_import``).

Every case is a hand-written model tree before the import, a hand-written
interchange file, and the planned files written out **in full** below -- the
oracle is the expected text, never the planner's own serializer. The report is
compared as text. The planner writes nothing, so each case also holds the tree's
bytes unchanged after planning. The apply's cases inject a failure at each
staged write, at each rename and at each of two removals, and hold every file's
bytes to the tree before the run; one more fails a restore and holds the rollback
to finishing the rest and naming that path.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, make_empty_model_root, need, needs_document
from filesystem_support import case_sensitive_under
from swcraft_tools import requirements_import
from swcraft_tools.model_state import ElementParseError, ModelRootNotFoundError, RetiredStorageError
from swcraft_tools.requirements_import import (
    NO_DELIVERY_VERSION,
    DeliveryVersion,
    ImportPlan,
    Removed,
    RequirementsImportRefusal,
    Stub,
    apply_requirements_import,
    delivery_version,
    mapping_as_named,
    plan_requirements_import,
    render_report,
)
from swcraft_tools.requirements_interchange import Need, SelectedNeeds, parse_needs_text, read_needs_file
from swcraft_tools.requirements_mapping import parse_mapping_text
from swcraft_tools.schema import Requirement
from swcraft_tools.validator import check_requirement_traceability, check_unknown_keys
from swcraft_tools.writer.elements import write_element
from swcraft_tools.yaml_load import load_yaml_document
from symlink_support import symlink_or_skip

EXAMPLES = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "requirements-interchange" / "examples"
)

#: A value that would print a summary line of its own, were the report not to escape it.
INJECTED = "\nSummary: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates"

#: The summary line's grammar -- the fixed contract a reader of the report (the VS Code command) parses.
SUMMARY = re.compile(r"Summary: (\d+) added, (\d+) changed, (\d+) removed, (\d+) stubs created, (\d+) source updates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_empty_model_root(tmp_path)


def put(root: Path, target: str, text: str) -> None:
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def needs(*items: dict[str, Any], baseline: str = "SRS-1") -> SelectedNeeds:
    return parse_needs_text(json.dumps(needs_document(*items, baseline=baseline)))


def external(requirement_id: str, version: int = 1, *extra: str, baseline: str = "SRS-1") -> str:
    """The file text of an external requirement as the import itself would write it from :func:`need`."""
    lines = [
        f"id: {requirement_id}",
        f"title: Title of {requirement_id}",
        f"description: Text of {requirement_id}.",
        "req_type: req",
        f"version: {version}",
        "source: external",
        *extra,
        f"baseline: {baseline}",
    ]
    return "\n".join(lines) + "\n"


def architecture(requirement_id: str, version: int = 1, *extra: str) -> str:
    lines = [
        f"id: {requirement_id}",
        f"title: Title of {requirement_id}",
        f"description: Text of {requirement_id}.",
        "req_type: req",
        f"version: {version}",
        "source: architecture",
        *extra,
    ]
    return "\n".join(lines) + "\n"


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def plan(root: Path, selected: SelectedNeeds, *, baseline: DeliveryVersion | None = None) -> ImportPlan:
    """Plan, and hold the planner to writing nothing."""
    before = tree(root)
    result = plan_requirements_import(root, selected, baseline=baseline)
    assert tree(root) == before
    return result


def written(result: ImportPlan) -> dict[str, str]:
    return {planned.target: planned.text for planned in result.writes}


def refusal(root: Path, selected: SelectedNeeds) -> RequirementsImportRefusal:
    before = tree(root)
    with pytest.raises(RequirementsImportRefusal) as caught:
        plan_requirements_import(root, selected)
    assert tree(root) == before
    return caught.value


def section(report: str, title: str) -> list[str]:
    """The entries of one report section, or ``[]`` when the section is not printed.

    An entry's detail lines (indented four spaces, *Changed* only) are left out: :func:`details` reads them.
    """
    lines = report.splitlines()
    if f"{title}:" not in lines:
        return []
    entries = []
    for line in lines[lines.index(f"{title}:") + 1 :]:
        if not line.startswith("  "):
            break
        if not line.startswith("    "):
            entries.append(line[2:])
    return entries


def details(report: str, entry: str) -> list[str]:
    """The detail lines printed under *entry* of the *Changed* section, unindented."""
    lines = report.splitlines()
    found = []
    for line in lines[lines.index(f"  {entry}") + 1 :]:
        if not line.startswith("    "):
            break
        found.append(line[4:])
    return found


# ---------------------------------------------------------------------------
# A new requirement
# ---------------------------------------------------------------------------


class TestANewRequirement:
    def test_the_profile_example_is_written_at_version_one_with_its_parent_at_the_post_import_version(
        self, root: Path
    ) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 2))
        # the profile example, with SG-1 brought changed too, so the parent's post-import version is 3
        document = json.loads((EXAMPLES / "profile-example.json").read_text(encoding="utf-8"))
        document["versions"]["SRS-2.3"]["needs"]["SG-1"] = need("SG-1", title="Changed title")
        changed_parent = parse_needs_text(json.dumps(document))
        result = plan(root, changed_parent)
        assert written(result)["requirements/SWR-12.yaml"] == (
            "id: SWR-12\n"
            "title: Wheel speed plausibility\n"
            "description: The software shall reject a wheel speed above 400 km/h.\n"
            "req_type: req\n"
            "version: 1\n"
            "source: external\n"
            "derived_from:\n"
            "- SG-1@3\n"
            "asil: B(D)\n"
            "document: SRS Brake Control\n"
            "baseline: SRS-2.3\n"
            "revision: '7'\n"
        )
        assert result.added == (("SWR-12", 1),)
        assert [c.id for c in result.changed] == ["SG-1"]
        assert result.changed[0].new == 3

    def test_provenance_that_yaml_would_read_as_another_type_is_written_quoted(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-1", document="2026", revision="7"), baseline="2026-10-18"))
        text = written(result)["requirements/SWR-1.yaml"]
        assert "document: '2026'\n" in text
        assert "baseline: '2026-10-18'\n" in text
        assert "revision: '7'\n" in text

    def test_a_status_other_than_rejected_is_read_and_not_stored(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-1", status="approved")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1")}

    def test_the_architecture_parent_of_a_new_requirement_is_written_at_its_version(self, root: Path) -> None:
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 4))
        result = plan(root, needs(need("SWR-1", derived_from=["SYS-1"])))
        assert written(result) == {
            "requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SYS-1@4"),
        }


# ---------------------------------------------------------------------------
# What moves the version
# ---------------------------------------------------------------------------


class TestAChange:
    @pytest.mark.parametrize(
        ("key", "value", "field", "line"),
        [
            ("title", "New title", "title", "title: New title"),
            ("content", "New text.", "description", "description: New text."),
            ("type", "spec", "req_type", "req_type: spec"),
            ("asil", "B", "asil", None),
        ],
    )
    def test_each_compared_field_bumps_the_version_and_is_named(
        self, root: Path, key: str, value: str, field: str, line: str | None
    ) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 3))
        result = plan(root, needs(need("SWR-1", **{key: value})))
        text = written(result)["requirements/SWR-1.yaml"]
        assert "version: 4\n" in text
        if line is not None:
            assert f"{line}\n" in text
        else:
            assert "asil: B\n" in text
        assert [tuple(c)[:4] for c in result.changed] == [("SWR-1", 3, 4, (field,))]
        assert section(render_report(result, dry_run=False), "Changed") == [f"SWR-1 v3 → v4 ({field})"]

    def test_an_absent_asil_clears_the_level_and_bumps(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "asil: C"))
        result = plan(root, needs(need("SWR-1")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2)}
        assert section(render_report(result, dry_run=False), "Changed") == ["SWR-1 v1 → v2 (ASIL cleared)"]

    def test_a_parent_set_change_bumps(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1"))
        put(root, "requirements/SG-2.yaml", architecture("SG-2"))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@1"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1", "SG-2"])))
        assert written(result) == {
            "requirements/SWR-1.yaml": external("SWR-1", 2, "derived_from:", "- SG-1@1", "- SG-2@1"),
        }
        assert result.changed[0].fields == ("derived_from",)

    def test_the_parents_are_written_in_the_files_order(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1"))
        put(root, "requirements/SG-2.yaml", architecture("SG-2"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-2", "SG-1"])))
        assert written(result)["requirements/SWR-1.yaml"] == external(
            "SWR-1", 1, "derived_from:", "- SG-2@1", "- SG-1@1"
        )

    def test_a_parent_the_delivery_names_twice_is_written_once(self, root: Path) -> None:
        """``trace.duplicate-link`` (contract ``12.5.0``): the import never writes a ``derived_from`` repeat.

        The reader keeps each parent once (``requirements_interchange``'s
        ``parents``), so a delivery naming one parent twice writes it once, at its
        version, and the written model gives ``check-model`` no duplicate.
        """
        put(root, "requirements/SG-1.yaml", architecture("SG-1"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1", "SG-1"])))
        assert written(result)["requirements/SWR-1.yaml"] == external("SWR-1", 1, "derived_from:", "- SG-1@1")
        put(root, "requirements/SWR-1.yaml", written(result)["requirements/SWR-1.yaml"])
        records = check_requirement_traceability(str(root)).records
        assert [record for record in records if record.rule == "trace.duplicate-link"] == []

    def test_an_element_form_parent_in_the_model_makes_the_set_differ(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- hardware:CORE0"))
        result = plan(root, needs(need("SWR-1")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2)}
        assert result.changed[0].fields == ("derived_from",)

    @pytest.mark.parametrize("member", ["SG-1", "SG-1@x", "SG-1@1"])
    def test_a_bare_or_malformed_parent_reduces_to_the_same_set(self, root: Path, member: str) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1", 2))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 5, "derived_from:", f"- {member}"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 5, "derived_from:", "- SG-1@2")}
        assert result.changed == ()
        assert result.parent_versions_updated == (("SWR-1", ("SG-1@2",)),)

    def test_parents_differing_only_in_order_are_no_change(self, root: Path) -> None:
        put(root, "requirements/A.yaml", architecture("A", 1))
        put(root, "requirements/B.yaml", architecture("B", 1))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- A@1", "- B@1"))
        result = plan(root, needs(need("SWR-1", derived_from=["B", "A"])))
        assert result.writes == ()
        assert result.parent_versions_updated == ()
        assert "Parent versions updated:" not in render_report(result, dry_run=False).splitlines()

    def test_a_parent_listed_twice_in_the_model_is_written_once_and_reported(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1", 3))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@2", "- SG-1@3"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@3")}
        assert result.changed == ()
        assert result.parent_versions_updated == (("SWR-1", ("SG-1@3",)),)

    def test_the_same_parent_text_listed_twice_in_the_model_is_written_once_and_reported(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1", 3))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@3", "- SG-1@3"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@3")}
        assert result.changed == ()
        assert result.parent_versions_updated == (("SWR-1", ("SG-1@3",)),)
        assert not result.changes_nothing

    def test_a_parents_version_moving_alone_does_not_bump_the_child(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 1))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@1"))
        result = plan(root, needs(need("SG-1", content="Changed."), need("SWR-1", derived_from=["SG-1"])))
        assert written(result)["requirements/SWR-1.yaml"] == external("SWR-1", 1, "derived_from:", "- SG-1@2")
        assert [c.id for c in result.changed] == ["SG-1"]
        assert section(render_report(result, dry_run=False), "Parent versions updated") == ["SWR-1 (SG-1@2)"]


class TestProvenance:
    def test_an_absent_document_clears_it_without_a_bump(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2, "document: SRS"))
        result = plan(root, needs(need("SWR-1")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2)}
        assert result.changed == ()
        assert result.source_updated == (("SWR-1", ("document",)),)

    def test_a_changed_revision_alone_is_a_source_update(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2) + "revision: '3'\n")
        result = plan(root, needs(need("SWR-1", revision="4")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2) + "revision: '4'\n"}
        assert section(render_report(result, dry_run=False), "Source information updated") == ["SWR-1 (revision)"]

    def test_an_absent_revision_clears_it(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2) + "revision: '3'\n")
        result = plan(root, needs(need("SWR-1")))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2)}
        assert result.source_updated == (("SWR-1", ("revision",)),)

    def test_a_new_baseline_is_a_source_update(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        result = plan(root, needs(need("SWR-1"), baseline="SRS-2"))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 2, baseline="SRS-2")}
        assert result.source_updated == (("SWR-1", ("baseline",)),)

    def test_a_change_names_the_provenance_that_moved_with_it(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        result = plan(root, needs(need("SWR-1", title="New", revision="9")))
        assert result.changed[0].fields == ("title", "revision")
        assert result.source_updated == ()


class TestNothingToWrite:
    def test_an_identical_need_writes_nothing(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 7))
        result = plan(root, needs(need("SWR-1")))
        assert result.writes == () and result.removals == ()
        assert render_report(result, dry_run=False) == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates\n"
            "No change.\n"
        )

    def test_an_architecture_requirement_absent_from_the_file_is_untouched(self, root: Path) -> None:
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 3, "derived_from:", "- GONE-1@2"))
        result = plan(root, needs(need("SWR-1")))
        assert set(written(result)) == {"requirements/SWR-1.yaml"}
        assert result.removals == ()


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


class TestRejected:
    def test_a_rejected_requirement_is_removed_and_a_link_to_it_dangles(self, root: Path) -> None:
        put(root, "requirements/SWR-12.yaml", external("SWR-12", 2))
        put(root, "swc/example/ExampleSWC.yaml", "name: ExampleSWC\ntype: application\nsatisfies:\n- SWR-12@2\n")
        result = plan(root, needs(need("SWR-12", status="rejected")))
        assert result.writes == ()
        assert result.removals == ("requirements/SWR-12.yaml",)
        report = render_report(result, dry_run=False)
        assert section(report, "Removed") == ["SWR-12 (was v2; status 'rejected')"]
        assert section(report, "Links now naming no requirement") == ["SWC 'ExampleSWC' satisfies 'SWR-12@2'"]

    def test_a_rejected_id_the_model_lacks_is_listed(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-9", status="rejected")))
        assert result.changes_nothing
        report = render_report(result, dry_run=False)
        assert section(report, "Removed by status, not in the model") == ["SWR-9 (status 'rejected')"]
        assert "No change." in report.splitlines()

    def test_a_parent_this_run_removes_is_written_at_its_last_version_and_dangles(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 4))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@4"))
        result = plan(root, needs(need("SG-1", status="rejected"), need("SWR-1", derived_from=["SG-1"])))
        assert result.writes == ()  # the child is unchanged: SG-1@4 is its last version
        assert result.removals == ("requirements/SG-1.yaml",)
        assert section(render_report(result, dry_run=False), "Links now naming no requirement") == [
            "Requirement 'SWR-1' derives from 'SG-1@4'"
        ]

    def test_a_parent_the_file_rejects_and_the_model_lacks_is_written_bare_and_dangles(self, root: Path) -> None:
        result = plan(root, needs(need("SG-1", status="rejected"), need("SWR-1", derived_from=["SG-1"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1")}
        assert result.stubs == ()
        assert result.links_dangling == ("Requirement 'SWR-1' derives from 'SG-1'",)

    def test_a_held_requirement_the_file_leaves_alone_dangles_on_a_removed_parent(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 4))
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 1, "derived_from:", "- SG-1@4"))
        result = plan(root, needs(need("SG-1", status="rejected")))
        assert result.writes == ()
        assert result.links_dangling == ("Requirement 'SYS-1' derives from 'SG-1@4'",)

    def test_a_rejected_stub_is_removed(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        result = plan(root, needs(need("SG-9", status="rejected")))
        assert result.removals == ("requirements/SG-9.yaml",)


# ---------------------------------------------------------------------------
# A delivery is a complete set
# ---------------------------------------------------------------------------

#: A mapping that ignores ``test`` needs and removes by ``obsolete`` as well as ``rejected``.
COMPLETE_SET_MAPPING = parse_mapping_text("types:\n  req: req\n  test: ignore\nremoved_status: [rejected, obsolete]\n")


def delivered(*items: dict[str, Any]) -> SelectedNeeds:
    """The delivery *items* read through :data:`COMPLETE_SET_MAPPING`."""
    return parse_needs_text(json.dumps(needs_document(*items)), COMPLETE_SET_MAPPING)


def requirement_tree(root: Path) -> dict[str, str]:
    """Every requirement file's text by its model-root-relative POSIX path."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "requirements").glob("*.yaml")
    }


def applied(root: Path, selected: SelectedNeeds) -> tuple[ImportPlan, str]:
    """Plan *selected*, apply it, and return the plan and its report."""
    result = plan(root, selected)
    apply_requirements_import(result)
    return result, render_report(result, dry_run=False)


#: The element that satisfies ``OLD-1@3`` in the cases below.
SATISFIES_OLD_1 = "name: A\ntype: application\nsatisfies:\n- OLD-1@3\n"


class TestACompleteSet:
    def test_an_absent_requirement_with_an_element_link_is_removed_and_the_link_listed(self, root: Path) -> None:
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 3))
        put(root, "swc/A.yaml", SATISFIES_OLD_1)
        result, report = applied(root, delivered(need("SWR-1")))
        assert requirement_tree(root) == {"requirements/SWR-1.yaml": external("SWR-1")}
        assert (root / "swc/A.yaml").read_text(encoding="utf-8") == SATISFIES_OLD_1
        assert result.removed == (Removed("OLD-1", 3, "absent from this delivery"),)
        assert section(report, "Removed") == ["OLD-1 (was v3; absent from this delivery)"]
        assert section(report, "Links now naming no requirement") == ["SWC 'A' satisfies 'OLD-1@3'"]
        assert SUMMARY.search(report).groups() == ("1", "0", "1", "0", "0")

    def test_an_absent_requirement_with_no_link_is_removed(self, root: Path) -> None:
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 3, "asil: D"))
        result, report = applied(root, delivered(need("SWR-1")))
        assert requirement_tree(root) == {"requirements/SWR-1.yaml": external("SWR-1")}
        assert section(report, "Removed") == ["OLD-1 (was v3; absent from this delivery)"]
        assert result.links_dangling == ()

    def test_an_absent_requirement_a_delivery_need_derives_from_becomes_a_stub_one_up(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 2, "asil: D"))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@2"))
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SG-1@2\n")
        result, report = applied(root, delivered(need("SWR-1", derived_from=["SG-1"])))
        assert requirement_tree(root) == {
            "requirements/SG-1.yaml": _stub_text("SG-1", "SWR-1").replace("version: 1", "version: 3"),
            "requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@3"),
        }
        assert result.removals == ()
        assert section(report, "Changed") == ["SG-1 v2 → v3 (replaced by a stub: absent from this delivery)"]
        assert details(report, "SG-1 v2 → v3 (replaced by a stub: absent from this delivery)") == []
        assert section(report, "Parent versions updated") == ["SWR-1 (SG-1@3)"]
        assert section(report, "Links now needing confirmation") == ["SWC 'A' satisfies 'SG-1@2'"]
        assert SUMMARY.search(report).groups() == ("0", "1", "0", "0", "0")

    def test_an_absent_requirement_an_architecture_requirement_derives_from_becomes_a_stub(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 1))
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 1, "derived_from:", "- SG-1@1"))
        result, report = applied(root, delivered())
        assert requirement_tree(root) == {
            "requirements/SG-1.yaml": _stub_text("SG-1", "SYS-1").replace("version: 1", "version: 2"),
            "requirements/SYS-1.yaml": architecture("SYS-1", 1, "derived_from:", "- SG-1@1"),
        }
        assert section(report, "Links now needing confirmation") == ["Requirement 'SYS-1' derives from 'SG-1@1'"]

    def test_an_ignored_type_need_s_held_id_is_removed(self, root: Path) -> None:
        put(root, "requirements/TC-1.yaml", external("TC-1", 2))
        result, report = applied(root, delivered(need("TC-1", type="test"), need("SWR-1")))
        assert requirement_tree(root) == {"requirements/SWR-1.yaml": external("SWR-1")}
        assert section(report, "Removed") == ["TC-1 (was v2; type 'test' not imported)"]

    def test_an_ignored_type_need_s_held_id_a_delivery_need_derives_from_becomes_a_stub(self, root: Path) -> None:
        put(root, "requirements/TC-1.yaml", external("TC-1", 2))
        _, report = applied(root, delivered(need("TC-1", type="test"), need("SWR-1", derived_from=["TC-1"])))
        assert requirement_tree(root)["requirements/TC-1.yaml"] == _stub_text("TC-1", "SWR-1").replace(
            "version: 1", "version: 3"
        )
        assert section(report, "Changed") == ["TC-1 v2 → v3 (replaced by a stub: type 'test' not imported)"]

    def test_a_custom_removed_status_removes(self, root: Path) -> None:
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 3))
        _, report = applied(root, delivered(need("OLD-1", status="obsolete")))
        assert requirement_tree(root) == {}
        assert section(report, "Removed") == ["OLD-1 (was v3; status 'obsolete')"]

    def test_a_custom_removed_status_the_model_lacks_is_listed_with_its_status(self, root: Path) -> None:
        result, report = applied(root, delivered(need("OLD-1", status="obsolete")))
        assert result.changes_nothing
        assert section(report, "Removed by status, not in the model") == ["OLD-1 (status 'obsolete')"]

    def test_a_status_removed_requirement_a_delivery_need_names_is_removed_and_the_link_listed(
        self, root: Path
    ) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 4))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@4"))
        _, report = applied(root, delivered(need("SG-1", status="obsolete"), need("SWR-1", derived_from=["SG-1"])))
        assert requirement_tree(root) == {
            "requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@4"),
        }
        assert section(report, "Removed") == ["SG-1 (was v4; status 'obsolete')"]
        assert section(report, "Links now naming no requirement") == ["Requirement 'SWR-1' derives from 'SG-1@4'"]

    @pytest.mark.parametrize("named", [True, False], ids=["named", "unnamed"])
    def test_a_held_stub_whose_status_removes_it_is_removed_by_that_status(self, root: Path, named: bool) -> None:
        # Case 1 before case 4: a stub a surviving element still names is not kept when its status removes it.
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        if named:
            put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SG-9@1\n")
        result, report = applied(root, delivered(need("SG-9", status="obsolete")))
        assert requirement_tree(root) == {}
        assert result.removed == (Removed("SG-9", 1, "status 'obsolete'"),)
        assert section(report, "Links now naming no requirement") == (["SWC 'A' satisfies 'SG-9@1'"] if named else [])

    @pytest.mark.parametrize("stubbed", [False, True], ids=["removed", "turned-into-a-stub"])
    def test_a_link_from_a_requirement_the_run_removes_or_stubs_does_not_raise_a_new_stub_s_version(
        self, root: Path, stubbed: bool
    ) -> None:
        # Step 5 counts the highest @N over the links the run LEAVES: OLD-1's `SG-9@4` goes with it,
        # removed or turned into a stub (a stub names nothing), so the new stub SG-9 starts at 1.
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 2, "derived_from:", "- SG-9@4"))
        parents = ["SG-9", "OLD-1"] if stubbed else ["SG-9"]
        result = plan(root, delivered(need("SWR-1", derived_from=parents)))
        assert result.stubs == (Stub("SG-9", 1, ("SWR-1",)),)
        if stubbed:
            assert [entry.id for entry in result.changed] == ["OLD-1"]
            assert result.removed == ()
        else:
            assert result.removed == (Removed("OLD-1", 2, "absent from this delivery"),)

    def test_a_stub_an_architecture_requirement_names_is_kept(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SYS-1"))
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 1, "derived_from:", "- SG-9@1"))
        before = requirement_tree(root)
        result, report = applied(root, delivered())
        assert requirement_tree(root) == before
        assert result.changes_nothing and "No change." in report.splitlines()

    def test_a_stub_an_element_names_is_kept(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SG-9@1\n")
        result, _ = applied(root, delivered())
        assert requirement_tree(root) == {"requirements/SG-9.yaml": _stub_text("SG-9", "SWR-1")}
        assert result.removed == ()

    def test_a_stub_nothing_names_is_removed(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        _, report = applied(root, delivered(need("SWR-1")))
        assert requirement_tree(root) == {"requirements/SWR-1.yaml": external("SWR-1")}
        assert section(report, "Removed") == ["SG-9 (was v1; stub no longer named)"]

    def test_a_stub_whose_only_referrer_this_run_removes_is_removed(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2, "derived_from:", "- SG-9@1"))
        _, report = applied(root, delivered())
        assert requirement_tree(root) == {}
        assert section(report, "Removed") == [
            "SWR-1 (was v2; absent from this delivery)",
            "SG-9 (was v1; stub no longer named)",
        ]
        assert section(report, "Links now naming no requirement") == []

    def test_a_requirement_turned_into_a_stub_names_nothing_so_its_stub_parent_is_removed(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SG-1"))
        put(root, "requirements/SG-1.yaml", external("SG-1", 1, "derived_from:", "- SG-9@1"))
        _, report = applied(root, delivered(need("SWR-1", derived_from=["SG-1"])))
        assert requirement_tree(root) == {
            "requirements/SG-1.yaml": _stub_text("SG-1", "SWR-1").replace("version: 1", "version: 2"),
            "requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@2"),
        }
        assert section(report, "Removed") == ["SG-9 (was v1; stub no longer named)"]

    def test_a_run_that_writes_and_removes_nothing_prints_no_change(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        _, report = applied(root, delivered(need("SWR-1")))
        assert report.splitlines()[-1] == "No change."

    @pytest.mark.parametrize("extra", [{"title": "New"}, {"revision": "9"}], ids=["update", "source-update"])
    def test_a_run_that_adds_or_updates_without_removing_prints_no_no_change_line(
        self, root: Path, extra: dict[str, str]
    ) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        _, report = applied(root, delivered(need("SWR-1", **extra), need("SWR-2")))
        assert "No change." not in report.splitlines()
        assert section(report, "Removed") == []

    def test_a_removed_requirement_brought_back_is_added_above_every_version_still_named(self, root: Path) -> None:
        put(root, "requirements/SWR-3.yaml", external("SWR-3", 2))
        put(root, "requirements/SYS-1.yaml", architecture("SYS-1", 1, "derived_from:", "- SWR-3@1"))
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SWR-3@2\n")
        applied(root, delivered(need("SWR-3", status="rejected")))
        assert "requirements/SWR-3.yaml" not in requirement_tree(root)
        result, report = applied(root, delivered(need("SWR-3", content="Another text.")))
        assert requirement_tree(root)["requirements/SWR-3.yaml"] == external("SWR-3", 3).replace(
            "Text of SWR-3.", "Another text."
        )
        assert result.added == (("SWR-3", 3),)
        assert section(report, "Added") == ["SWR-3 (v3; links from before its removal stay unconfirmed)"]
        assert (result.links_confirmed, result.links_needing_confirmation, result.links_dangling) == ((), (), ())

    @pytest.mark.parametrize("pin", ["9", "9" * 5000], ids=["a-pin", "a-pin-past-int-digits"])
    @pytest.mark.parametrize("kept", [False, True], ids=["link-dropped", "link-kept"])
    def test_a_link_an_incoming_need_held_before_the_run_does_not_raise_its_parent(
        self, root: Path, pin: str, kept: bool
    ) -> None:
        # CHILD's held link is rewritten from the delivery, so the run does not leave it: PARENT,
        # new, starts at 1 whether the delivery drops the link or keeps it (written at PARENT@1).
        put(root, "requirements/CHILD.yaml", external("CHILD", 1, "derived_from:", f"- PARENT@{pin}"))
        child = need("CHILD", derived_from=["PARENT"]) if kept else need("CHILD")
        result, _ = applied(root, delivered(need("PARENT"), child))
        assert result.added == (("PARENT", 1),)
        assert requirement_tree(root)["requirements/PARENT.yaml"] == external("PARENT", 1)
        parents = ("derived_from:", "- PARENT@1") if kept else ()
        assert requirement_tree(root)["requirements/CHILD.yaml"] == external("CHILD", 1 if kept else 2, *parents)

    def test_a_parent_brought_back_with_a_child_left_unchanged_is_added_above_the_childs_link_and_rewrites_it(
        self, root: Path
    ) -> None:
        # The child's held link is not in step 5's names, and nothing of the child moved: at P@1 the
        # link would stay unwritten and read as confirmed against a text it was not made against.
        put(root, "requirements/P.yaml", external("P", 1))
        put(root, "requirements/C.yaml", external("C", 1, "derived_from:", "- P@1"))
        applied(root, delivered(need("P", status="rejected"), need("C", derived_from=["P"])))
        assert requirement_tree(root) == {"requirements/C.yaml": external("C", 1, "derived_from:", "- P@1")}
        result, report = applied(root, delivered(need("P", content="Another text."), need("C", derived_from=["P"])))
        assert requirement_tree(root) == {
            "requirements/P.yaml": external("P", 2).replace("Text of P.", "Another text."),
            "requirements/C.yaml": external("C", 1, "derived_from:", "- P@2"),
        }
        assert result.added == (("P", 2),)
        assert result.parent_versions_updated == (("C", ("P@2",)),)
        assert section(report, "Links confirmed by this import") == []
        assert (result.links_confirmed, result.links_needing_confirmation, result.links_dangling) == ((), (), ())

    def test_a_new_stub_a_child_left_unchanged_names_is_created_above_the_childs_link_and_rewrites_it(
        self, root: Path
    ) -> None:
        put(root, "requirements/C.yaml", external("C", 1, "derived_from:", "- S@1"))
        result, _ = applied(root, delivered(need("C", derived_from=["S"])))
        assert result.stubs == (Stub("S", 2, ("C",)),)
        assert requirement_tree(root) == {
            "requirements/C.yaml": external("C", 1, "derived_from:", "- S@2"),
            "requirements/S.yaml": _stub_text("S", "C").replace("version: 1", "version: 2"),
        }
        assert result.parent_versions_updated == (("C", ("S@2",)),)
        assert (result.links_confirmed, result.links_needing_confirmation, result.links_dangling) == ((), (), ())

    def test_a_new_id_skips_every_version_a_child_left_unchanged_names_it_at_and_nothing_cascades(
        self, root: Path
    ) -> None:
        # Two unchanged children hold P@1 and P@2, a third (its text moved, so rewritten anyway)
        # P@3: P is written at 3, the first version no link the run would leave names it at. The
        # skip reads no other id's version, so one pass settles it: every child is rewritten at
        # P@3, and only the moved one is raised.
        put(root, "requirements/C1.yaml", external("C1", 1, "derived_from:", "- P@1"))
        put(root, "requirements/C2.yaml", external("C2", 1, "derived_from:", "- P@2"))
        put(root, "requirements/C3.yaml", external("C3", 1, "derived_from:", "- P@3"))
        children = [need(child, derived_from=["P"]) for child in ("C1", "C2")]
        moved = need("C3", derived_from=["P"], content="Another text.")
        result, _ = applied(root, delivered(need("P"), *children, moved))
        assert result.added == (("P", 3),)
        tree = requirement_tree(root)
        assert tree["requirements/C1.yaml"] == external("C1", 1, "derived_from:", "- P@3")
        assert tree["requirements/C2.yaml"] == external("C2", 1, "derived_from:", "- P@3")
        assert tree["requirements/C3.yaml"] == external("C3", 2, "derived_from:", "- P@3").replace(
            "Text of C3.", "Another text."
        )

    def test_a_new_id_a_left_link_pins_past_the_writable_digits_is_refused(self, root: Path) -> None:
        put(root, "swc/A.yaml", f"name: A\ntype: application\nsatisfies:\n- SWR-3@{'9' * 640}\n")
        caught = refusal(root, delivered(need("SWR-3")))
        assert caught.problems == (
            "'SWR-3' would be written at a version of more than 640 digits, which no requirement file holds: "
            "correct the links in the model that name it at such a version",
        )

    def test_a_new_id_a_left_link_pins_just_under_the_writable_digits_is_written(self, root: Path) -> None:
        put(root, "swc/A.yaml", f"name: A\ntype: application\nsatisfies:\n- SWR-3@{'9' * 639}\n")
        result, _ = applied(root, delivered(need("SWR-3")))
        assert result.added == (("SWR-3", 10**639),)


class TestACompleteSetRefused:
    @pytest.mark.parametrize("status", ["rejected", "obsolete"])
    def test_a_status_removed_id_an_architecture_requirement_holds_is_refused(self, root: Path, status: str) -> None:
        put(root, "requirements/REQ-1.yaml", architecture("REQ-1"))
        before = tree(root)
        with pytest.raises(RequirementsImportRefusal) as caught:
            plan_requirements_import(root, delivered(need("REQ-1", status=status)))
        assert caught.value.problems == (
            "'REQ-1' is the id of an architecture requirement: the import changes only external requirements, "
            "and that requirement is left untouched",
        )
        assert tree(root) == before

    def test_an_absent_id_two_files_declare_is_refused_and_neither_file_changes(self, root: Path) -> None:
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 1))
        put(root, "requirements/OLD-1-copy.yaml", external("OLD-1", 2))
        caught = refusal(root, delivered(need("SWR-1")))
        assert caught.problems == (
            "'OLD-1' is declared by 2 files of the model (requirements/OLD-1-copy.yaml, requirements/OLD-1.yaml): "
            "the import would remove one and leave the other",
        )

    def test_an_outside_parent_two_files_declare_is_refused_and_neither_file_changes(self, root: Path) -> None:
        put(root, "requirements/SG-1-a.yaml", external("SG-1", 2))
        put(root, "requirements/SG-1-b.yaml", external("SG-1", 3))
        caught = refusal(root, delivered(need("SWR-1", derived_from=["SG-1"])))
        assert caught.problems == (
            "'SG-1' is declared by 2 files of the model (requirements/SG-1-a.yaml, requirements/SG-1-b.yaml): "
            "the import would turn one into a stub and leave the other",
        )

    def test_an_ignored_type_id_an_architecture_requirement_holds_is_not_refused_and_left_unchanged(
        self, root: Path
    ) -> None:
        put(root, "requirements/TC-1.yaml", architecture("TC-1", 4))
        result, _ = applied(root, delivered(need("TC-1", type="test"), need("SWR-1")))
        assert requirement_tree(root) == {
            "requirements/TC-1.yaml": architecture("TC-1", 4),
            "requirements/SWR-1.yaml": external("SWR-1"),
        }
        assert result.removed == () and result.changed == ()

    def test_an_incoming_id_an_architecture_requirement_holds_is_still_refused(self, root: Path) -> None:
        put(root, "requirements/REQ-1.yaml", architecture("REQ-1"))
        caught = refusal(root, delivered(need("REQ-1")))
        assert caught.problems[0].startswith("'REQ-1' is the id of an architecture requirement")

    def test_the_rollback_restores_an_absent_requirement_whose_removal_fails(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        put(root, "requirements/OLD-1.yaml", external("OLD-1", 3))
        put(root, "requirements/OLD-2.yaml", external("OLD-2", 1))
        result = plan(root, delivered(need("SWR-1")))
        assert result.removals == ("requirements/OLD-1.yaml", "requirements/OLD-2.yaml")
        before = tree(root)
        failing = root / "requirements/OLD-2.yaml"
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self == failing:
                raise PermissionError("injected removal failure")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError, match="injected removal failure"):
            apply_requirements_import(result)
        assert tree(root) == before


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize("status", [None, "rejected"])
    def test_an_id_an_architecture_requirement_holds_is_refused(self, root: Path, status: str | None) -> None:
        put(root, "requirements/REQ-1.yaml", architecture("REQ-1"))
        before = (root / "requirements/REQ-1.yaml").read_bytes()
        extra = {} if status is None else {"status": status}
        caught = refusal(root, needs(need("REQ-1", **extra), need("SWR-1")))
        assert caught.problems == (
            "'REQ-1' is the id of an architecture requirement: the import changes only external requirements, "
            "and that requirement is left untouched",
        )
        assert (root / "requirements/REQ-1.yaml").read_bytes() == before

    def test_an_id_two_files_declare_is_refused(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1"))
        put(root, "requirements/SWR-1-copy.yaml", external("SWR-1"))
        caught = refusal(root, needs(need("SWR-1")))
        assert caught.problems == (
            "'SWR-1' is declared by 2 files of the model (requirements/SWR-1-copy.yaml, requirements/SWR-1.yaml): "
            "the import would update one and leave the other",
        )

    def test_every_problem_is_named_in_one_refusal(self, root: Path) -> None:
        put(root, "requirements/A.yaml", architecture("A"))
        put(root, "requirements/B.yaml", architecture("B"))
        caught = refusal(root, needs(need("A"), need("B", status="rejected")))
        assert [problem.split(" ", 1)[0] for problem in caught.problems] == ["'A'", "'B'"]
        assert str(caught).startswith("the requirements import is refused and nothing was written:\n  'A' ")

    def test_a_lone_surrogate_is_refused_by_the_read_back_naming_the_need_and_its_key(self, root: Path) -> None:
        text = '{"current_version": "S", "versions": {"S": {"needs": {"SWR-1": {"id": "SWR-1", "title": "T", '
        text += '"content": "bad \\ud800 text", "type": "req"}}}}}'
        caught = refusal(root, parse_needs_text(text))
        assert len(caught.problems) == 1
        assert caught.problems[0].startswith("need 'SWR-1': SWCraft's field 'description' holds text")

    def test_an_id_the_store_cannot_hold_is_refused(self, root: Path) -> None:
        caught = refusal(root, needs(need("CON")))
        assert caught.problems[0].startswith("'CON' cannot be stored: ")

    def test_every_second_stage_problem_is_named_in_one_refusal(self, root: Path) -> None:
        long_id = "a" * 198
        put(root, f"requirements/{long_id}.yaml", external(long_id))
        caught = refusal(root, needs(need("CON"), need(long_id.upper()), need("SWR-1", content="bad \ud800 text")))
        assert len(caught.problems) == 3
        assert caught.problems[0].startswith("'CON' cannot be stored: ")
        assert caught.problems[1].startswith(f"the file name of '{long_id.upper()}' in layer 'requirements' is held")
        assert caught.problems[2].startswith("need 'SWR-1': SWCraft's field 'description' holds text the model's YAML")
        assert "collision suffix '~1' is 200 bytes long" in caught.problems[1]

    def test_a_stub_id_the_store_cannot_hold_is_refused(self, root: Path) -> None:
        caught = refusal(root, needs(need("SWR-1", derived_from=["CON"])))
        assert len(caught.problems) == 1
        assert caught.problems[0].startswith("'CON' cannot be stored: ")

    def test_a_stub_that_would_not_read_back_is_refused_as_a_stub(self, root: Path) -> None:
        # The reader refuses a lone surrogate in a parent id (the report prints it), so the need is
        # built past it: the planner's read-back holds for any caller that hands it a Need.
        built = Need("SWR-1", "Title of SWR-1", "Text of SWR-1.", "req", derived_from=("SG-\ud800",))
        caught = refusal(root, SelectedNeeds(baseline="SRS-1", in_file_order=(built,)))
        assert caught.problems[0] == (
            "need 'SWR-1': SWCraft's field 'derived_from' holds text the model's YAML reader cannot read back "
            "(a lone surrogate?)"
        )
        assert caught.problems[-1] == (
            "stub 'SG-\ud800': 'id' holds text the model's YAML reader cannot read back (a lone surrogate?)"
        )

    def test_a_need_text_that_would_not_read_back_names_swcraft_s_field_never_a_need_key(self, root: Path) -> None:
        # Under --mapping the description is read from the team's own key (`body`, say), which the
        # planner never sees: it names SWCraft's field, never the default source key `content`.
        text = '{"current_version": "S", "versions": {"S": {"needs": {"SWR-1": {"id": "SWR-1", "title": "T", '
        text += '"body": "bad \\ud800 text", "type": "req"}}}}}'
        caught = refusal(root, parse_needs_text(text, parse_mapping_text("fields:\n  description: body\n")))
        assert caught.problems == (
            "need 'SWR-1': SWCraft's field 'description' holds text the model's YAML reader cannot read back "
            "(a lone surrogate?)",
        )

    def test_a_rejected_id_two_files_declare_is_refused(self, root: Path) -> None:
        put(root, "requirements/x.yaml", external("SWR-1"))
        put(root, "requirements/y.yaml", external("SWR-1"))
        caught = refusal(root, needs(need("SWR-1", status="rejected")))
        assert caught.problems == (
            "'SWR-1' is declared by 2 files of the model (requirements/x.yaml, requirements/y.yaml): "
            "the import would update one and leave the other",
        )

    def test_an_unparseable_requirement_file_refuses_the_import_naming_it(self, root: Path) -> None:
        put(root, "requirements/Bad.yaml", "id: [\n")
        with pytest.raises(ElementParseError, match=r"requirements/Bad\.yaml"):
            plan_requirements_import(root, needs(need("SWR-1")))

    def test_a_missing_model_root_refuses_the_import(self, tmp_path: Path) -> None:
        with pytest.raises(ModelRootNotFoundError):
            plan_requirements_import(tmp_path / "absent", needs(need("SWR-1")))

    def test_a_model_on_retired_storage_refuses_the_import(self, root: Path) -> None:
        put(root, "mapping_sets/Old.yaml", "name: Old\n")
        with pytest.raises(RetiredStorageError):
            plan_requirements_import(root, needs(need("SWR-1")))

    def test_a_layer_the_loader_cannot_read_refuses_the_import(self, root: Path) -> None:
        put(root, "tasks/Broken.yaml", "name: Broken\nperiod: [\n")
        with pytest.raises(ValueError, match=r"tasks/Broken\.yaml"):
            plan_requirements_import(root, needs(need("SWR-1")))


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _stub_text(requirement_id: str, *children: str) -> str:
    return (
        f"id: {requirement_id}\n"
        f"title: {requirement_id}\n"
        f"description: Placeholder - named as a parent by {', '.join(children)}, not in the import.\n"
        "req_type: req\n"
        "version: 1\n"
        "source: external\n"
        "stub: true\n"
    )


class TestStubs:
    def test_a_parent_never_known_becomes_one_stub_naming_every_child(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-2", derived_from=["SG-9"]), need("SWR-1", derived_from=["SG-9"])))
        assert written(result) == {
            "requirements/SWR-2.yaml": external("SWR-2", 1, "derived_from:", "- SG-9@1"),
            "requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-9@1"),
            "requirements/SG-9.yaml": _stub_text("SG-9", "SWR-1", "SWR-2"),
        }
        assert [planned.target for planned in result.writes][-1] == "requirements/SG-9.yaml"
        assert section(render_report(result, dry_run=False), "Stubs created") == ["SG-9 (v1), named by SWR-1, SWR-2"]

    def test_an_import_bringing_the_real_requirement_replaces_the_stub(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        put(root, "requirements/SWR-1.yaml", architecture("SWR-1", 1, "derived_from:", "- SG-9@1"))
        put(root, "swc/ExampleSWC.yaml", "name: ExampleSWC\ntype: application\nsatisfies:\n- SG-9@1\n")
        result = plan(root, needs(need("SG-9", title="SG-9", content="The real text.")))
        assert written(result) == {
            "requirements/SG-9.yaml": (
                "id: SG-9\ntitle: SG-9\ndescription: The real text.\nreq_type: req\nversion: 2\n"
                "source: external\nbaseline: SRS-1\n"
            )
        }
        report = render_report(result, dry_run=False)
        assert section(report, "Changed") == ["SG-9 v1 → v2 (stub replaced)"]
        assert details(report, "SG-9 v1 → v2 (stub replaced)") == []
        assert section(report, "Links now needing confirmation") == [
            "SWC 'ExampleSWC' satisfies 'SG-9@1'",
            "Requirement 'SWR-1' derives from 'SG-9@1'",
        ]

    def test_a_stub_replaced_in_the_run_of_a_child_naming_it_is_written_at_its_new_version(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-9@1"))
        result = plan(root, needs(need("SG-9"), need("SWR-1", derived_from=["SG-9"])))
        assert written(result)["requirements/SWR-1.yaml"] == external("SWR-1", 1, "derived_from:", "- SG-9@2")
        assert result.parent_versions_updated == (("SWR-1", ("SG-9@2",)),)
        assert [tuple(c) for c in result.changed] == [("SG-9", 1, 2, ("stub replaced",), ())]

    def test_a_new_stub_whose_id_a_link_still_names_starts_above_that_version(self, root: Path) -> None:
        # A link naming an id the model does not hold may be one left behind by a removal: the new
        # stub is written above it, so that link is never read as confirmed against the placeholder.
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SG-9@1\n- SG-9@2\n")
        result = plan(root, needs(need("SWR-1", derived_from=["SG-9"])))
        assert result.stubs == (Stub("SG-9", 3, ("SWR-1",)),)
        assert written(result)["requirements/SG-9.yaml"] == _stub_text("SG-9", "SWR-1").replace(
            "version: 1", "version: 3"
        )
        assert written(result)["requirements/SWR-1.yaml"] == external("SWR-1", 1, "derived_from:", "- SG-9@3")
        report = render_report(result, dry_run=False)
        assert section(report, "Stubs created") == [
            "SG-9 (v3; links from before its removal stay unconfirmed), named by SWR-1"
        ]
        assert result.links_confirmed == ()

    def test_an_architecture_parent_two_files_declare_is_written_at_the_first_files_version(self, root: Path) -> None:
        # Not refused -- the run touches no architecture requirement -- and the first file in the
        # scan's order answers; check-model reports the duplicate itself.
        put(root, "requirements/SG-1-a.yaml", architecture("SG-1", 2))
        put(root, "requirements/SG-1-b.yaml", architecture("SG-1", 3))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-1@2")}

    def test_a_stub_the_file_does_not_bring_is_kept(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        result = plan(root, needs(need("SWR-1", derived_from=["SG-9"])))
        assert written(result) == {"requirements/SWR-1.yaml": external("SWR-1", 1, "derived_from:", "- SG-9@1")}
        assert result.stubs == () and result.removals == ()


# ---------------------------------------------------------------------------
# The link sections
# ---------------------------------------------------------------------------

_LINKED_MODEL = {
    "swc/ExampleSWC.yaml": (
        "name: ExampleSWC\ntype: application\nsatisfies:\n- SWR-1@1\n"
        "ports:\n- name: pOut\n  direction: provide\n  interface: IExample\n  satisfies:\n  - SWR-1@1\n"
        "runnables:\n- name: Run\n  satisfies:\n  - SWR-1@1\n"
    ),
    "interfaces/IExample.yaml": (
        "name: IExample\nkind: sender_receiver\n"
        "data_elements:\n- name: value\n  implementation_type: float32\n"
        "satisfies:\n- SWR-1@1\n"
    ),
    "compositions/Root.yaml": "name: Root\ntitle: Root\nroot: true\nsatisfies:\n- SWR-1@1\n",
    "tasks/T10.yaml": "name: T10\nperiod: 0.01\npriority: 1\npartition: P0\nsatisfies:\n- SWR-1@1\n",
    "hardware/CORE0.yaml": "name: CORE0\nkind: core\nthreshold: 0.7\nsatisfies:\n- SWR-1@1\n",
    "requirements/SYS-1.yaml": architecture("SYS-1", 1, "derived_from:", "- SWR-1@1"),
}


class TestLinks:
    def test_a_bump_lists_every_link_site_confirmed_for_the_old_version(self, root: Path) -> None:
        for target, text in _LINKED_MODEL.items():
            put(root, target, text)
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        result = plan(root, needs(need("SWR-1", content="Changed.")))
        assert result.links_needing_confirmation == (
            "SWC 'ExampleSWC' satisfies 'SWR-1@1'",
            "SWC 'ExampleSWC' (port 'pOut') satisfies 'SWR-1@1'",
            "SWC 'ExampleSWC' (runnable 'Run') satisfies 'SWR-1@1'",
            "Interface 'IExample' satisfies 'SWR-1@1'",
            "Composition 'Root' satisfies 'SWR-1@1'",
            "Task 'T10' satisfies 'SWR-1@1'",
            "Hardware element 'CORE0' satisfies 'SWR-1@1'",
            "Requirement 'SYS-1' derives from 'SWR-1@1'",
        )
        assert result.links_confirmed == () and result.links_dangling == ()

    def test_two_kinds_sharing_a_name_name_two_sites(self, root: Path) -> None:
        """A site is named as the validator's trace findings name it, its kind included."""
        put(root, "compositions/Same.yaml", "name: Same\ntitle: Same\nroot: true\nsatisfies:\n- SWR-1@1\n")
        put(root, "tasks/Same.yaml", "name: Same\nperiod: 0.01\npriority: 1\npartition: P0\nsatisfies:\n- SWR-1@1\n")
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        result = plan(root, needs(need("SWR-1", content="Changed.")))
        assert result.links_needing_confirmation == (
            "Composition 'Same' satisfies 'SWR-1@1'",
            "Task 'Same' satisfies 'SWR-1@1'",
        )

    def test_a_link_written_ahead_of_the_bump_is_confirmed_by_it(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SWR-1@2\n- SWR-1@1\n- SWR-1\n")
        result = plan(root, needs(need("SWR-1", content="Changed.")))
        assert result.links_confirmed == ("SWC 'A' satisfies 'SWR-1@2'",)
        assert result.links_needing_confirmation == ("SWC 'A' satisfies 'SWR-1@1'",)

    def test_a_link_naming_a_new_id_is_never_confirmed_by_it(self, root: Path) -> None:
        # A link written ahead and a link left behind by a removal read alike: the new id starts above both.
        put(root, "swc/A.yaml", "name: A\ntype: application\nsatisfies:\n- SWR-1@1\n- hardware:CORE0\n")
        result = plan(root, needs(need("SWR-1")))
        assert result.added == (("SWR-1", 2),)
        assert (result.links_needing_confirmation, result.links_confirmed, result.links_dangling) == ((), (), ())

    def test_an_incoming_child_the_run_leaves_unwritten_has_its_link_confirmed(self, root: Path) -> None:
        # SWR-1 already names SG-1@2 while SG-1 is at 1: the import brings SWR-1 unchanged (no
        # write) and bumps SG-1 to 2, so SWR-1's link is confirmed -- read from the model, as the
        # link of a requirement the file leaves out would be.
        put(root, "requirements/SG-1.yaml", external("SG-1", 1))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@2"))
        result = plan(root, needs(need("SG-1", content="Changed."), need("SWR-1", derived_from=["SG-1"])))
        assert [planned.target for planned in result.writes] == ["requirements/SG-1.yaml"]
        assert result.links_confirmed == ("Requirement 'SWR-1' derives from 'SG-1@2'",)
        assert result.links_needing_confirmation == ()

    def test_a_rewritten_child_names_its_parent_s_new_version_and_is_not_a_link_section(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", external("SG-1", 1))
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1, "derived_from:", "- SG-1@1"))
        result = plan(root, needs(need("SG-1", content="Changed."), need("SWR-1", derived_from=["SG-1"])))
        assert result.parent_versions_updated == (("SWR-1", ("SG-1@2",)),)
        assert (result.links_needing_confirmation, result.links_confirmed) == ((), ())

    def test_an_unchanged_requirement_moves_no_link(self, root: Path) -> None:
        for target, text in _LINKED_MODEL.items():
            put(root, target, text)
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        result = plan(root, needs(need("SWR-1")))
        assert (result.links_needing_confirmation, result.links_confirmed, result.links_dangling) == ((), (), ())


# ---------------------------------------------------------------------------
# Where the files go
# ---------------------------------------------------------------------------


class TestStorage:
    def test_two_incoming_case_variant_ids_are_two_requirements(self, root: Path) -> None:
        result = plan(root, needs(need("Swr-1"), need("SWR-1")))
        assert [planned.target for planned in result.writes] == [
            "requirements/Swr-1.yaml",
            "requirements/SWR-1~1.yaml",
        ]

    def test_a_new_id_case_colliding_with_a_held_file_takes_the_suffix(self, root: Path) -> None:
        put(root, "requirements/swr-1.yaml", external("swr-1", 2))
        result = plan(root, needs(need("SWR-1")))
        assert [planned.target for planned in result.writes] == ["requirements/SWR-1~1.yaml"]
        assert result.added == (("SWR-1", 1),)

    def test_a_held_requirement_is_written_to_the_file_that_declares_it(self, root: Path) -> None:
        put(root, "requirements/renamed.yaml", external("SWR-1", 2))
        result = plan(root, needs(need("SWR-1", title="New")))
        assert [planned.target for planned in result.writes] == ["requirements/renamed.yaml"]


# ---------------------------------------------------------------------------
# The delivery's version: --baseline, else the file's, else refused
# ---------------------------------------------------------------------------

#: A mapping that ignores two need types, for the header's *Not imported* line.
IGNORING = parse_mapping_text("types:\n  req: req\n  test: ignore\n  review: ignore\n")


class TestTheDeliveryVersion:
    def test_the_given_value_wins_over_the_file(self) -> None:
        assert delivery_version(needs(baseline="SRS-1"), "abc123") == DeliveryVersion("abc123", given=True)

    def test_the_file_s_version_without_a_given_value(self) -> None:
        assert delivery_version(needs(baseline="SRS-1")) == DeliveryVersion("SRS-1", given=False)

    def test_neither_is_none(self) -> None:
        assert delivery_version(read_needs_file(EXAMPLES / "empty-current-version.json")) is None

    @pytest.mark.parametrize("blank", [" ", "  \t"])
    def test_a_blank_current_version_is_no_version_and_refused(self, tmp_path: Path, blank: str) -> None:
        # As a blank --baseline and the panel's box: a name of spaces records nothing.
        selected = needs(need("SWR-1"), baseline=blank)
        assert selected.baseline is None
        assert delivery_version(selected) is None
        with pytest.raises(RequirementsImportRefusal) as caught:
            plan_requirements_import(tmp_path / "absent", selected)
        assert caught.value.problems == (NO_DELIVERY_VERSION,)

    def test_an_empty_current_version_without_a_given_value_is_refused_before_the_model_is_read(
        self, tmp_path: Path
    ) -> None:
        # The model root does not exist: the refusal comes first, so it is the one raised.
        with pytest.raises(RequirementsImportRefusal) as caught:
            plan_requirements_import(tmp_path / "absent", read_needs_file(EXAMPLES / "empty-current-version.json"))
        assert caught.value.problems == (NO_DELIVERY_VERSION,)

    def test_an_empty_current_version_with_a_given_value_writes_it(self, root: Path) -> None:
        given = DeliveryVersion("abc123", given=True)
        result = plan(root, read_needs_file(EXAMPLES / "empty-current-version.json"), baseline=given)
        assert written(result) == {
            "requirements/SWR-1.yaml": (
                "id: SWR-1\ntitle: Title of SWR-1\ndescription: The software shall do what SWR-1 says.\n"
                "req_type: req\nversion: 1\nsource: external\nbaseline: abc123\n"
            )
        }
        assert render_report(result, dry_run=False).splitlines()[0] == ("Requirements import: baseline abc123")

    def test_a_given_value_overrides_a_non_empty_current_version_in_every_requirement_the_delivery_holds(
        self, root: Path
    ) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 3))
        put(root, "requirements/SWR-2.yaml", external("SWR-2", 1))
        given = DeliveryVersion("abc123", given=True)
        result = plan(root, needs(need("SWR-1"), need("SWR-2", title="New"), need("SWR-3")), baseline=given)
        assert written(result) == {
            "requirements/SWR-1.yaml": external("SWR-1", 3, baseline="abc123"),
            "requirements/SWR-2.yaml": external("SWR-2", 2, baseline="abc123").replace(
                "title: Title of SWR-2", "title: New"
            ),
            "requirements/SWR-3.yaml": external("SWR-3", 1, baseline="abc123"),
        }
        assert result.baseline == given

    def test_a_baseline_only_move_is_a_source_update_without_a_bump(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 3))
        result = plan(root, needs(need("SWR-1")), baseline=DeliveryVersion("abc123", given=True))
        assert result.changed == ()
        assert result.source_updated == (("SWR-1", ("baseline",)),)
        assert section(render_report(result, dry_run=False), "Source information updated") == ["SWR-1 (baseline)"]

    def test_a_stub_carries_no_baseline(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-1", derived_from=["SG-1"])), baseline=DeliveryVersion("abc", given=True))
        assert "baseline" not in written(result)["requirements/SG-1.yaml"]


class TestTheHeader:
    def test_the_file_s_version_and_no_mapping(self, root: Path) -> None:
        lines = render_report(plan(root, needs(need("SWR-1"))), dry_run=True).splitlines()
        assert lines[:4] == [
            "Requirements import: baseline SRS-1 (from the file)",
            "Mapping: none (SWCraft's own names)",
            "Dry run: nothing was written.",
            "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates",
        ]

    def test_the_mapping_as_spelled_and_the_needs_it_ignores(self, root: Path) -> None:
        selected = parse_needs_text(
            json.dumps(
                needs_document(
                    need("TC-2", type="test"), need("SWR-1"), need("RV-1", type="review"), need("TC-1", type="test")
                )
            ),
            IGNORING,
        )
        result = plan(root, selected, baseline=DeliveryVersion("abc", given=True))
        assert result.ignored_types == ("test", "review", "test")
        assert render_report(result, dry_run=False, mapping="../team.needs-mapping.yaml") == (
            "Requirements import: baseline abc\n"
            "Mapping: ../team.needs-mapping.yaml\n"
            "Not imported: 3 needs of types the mapping ignores (review, test)\n"
            "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates\n"
            "\n"
            "Added:\n"
            "  SWR-1 (v1)\n"
        )

    def test_two_needs_of_one_ignored_type_name_it_in_the_singular(self, root: Path) -> None:
        selected = parse_needs_text(
            json.dumps(needs_document(need("TC-1", type="test"), need("TC-2", type="test"))), IGNORING
        )
        report = render_report(plan(root, selected), dry_run=False, mapping="m.needs-mapping.yaml")
        assert "Not imported: 2 needs of a type the mapping ignores (test)" in report.splitlines()

    def test_no_not_imported_line_when_the_mapping_ignored_nothing(self, root: Path) -> None:
        selected = parse_needs_text(json.dumps(needs_document(need("SWR-1"))), IGNORING)
        report = render_report(plan(root, selected), dry_run=False, mapping="m.needs-mapping.yaml")
        assert not any(line.startswith("Not imported:") for line in report.splitlines())

    def test_a_mapping_spelled_with_a_control_or_an_undecodable_byte_prints_on_one_line_as_utf8(
        self, root: Path
    ) -> None:
        report = render_report(plan(root, needs()), dry_run=False, mapping="a\nb\udcff.yaml")
        assert report.splitlines()[1] == "Mapping: a\\nb\\udcff.yaml"
        report.encode("utf-8")

    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    def test_the_summary_line_is_the_header_s_last_and_its_grammar_unchanged(self, root: Path, dry_run: bool) -> None:
        selected = parse_needs_text(json.dumps(needs_document(need("TC-1", type="test"), need("SWR-1"))), IGNORING)
        report = render_report(plan(root, selected), dry_run=dry_run, mapping="m.needs-mapping.yaml")
        header = report.split("\n\n", 1)[0].splitlines()
        assert header[-1] == "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates"
        assert SUMMARY.fullmatch(header[-1]) is not None
        assert [line for line in report.splitlines() if line.startswith("Summary:")] == [header[-1]]


# ---------------------------------------------------------------------------
# The report's text
# ---------------------------------------------------------------------------


class TestTheMappingAsNamed:
    """``mapping_as_named``: the report's mapping inside the project relative to it, else as spelled."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        (project / "conf").mkdir(parents=True)
        (project / "conf" / "team.needs-mapping.yaml").write_text("{}\n", encoding="utf-8", newline="\n")
        return project

    def test_an_absolute_spelling_inside_the_project_is_named_relative_to_it(self, project: Path) -> None:
        spelled = str(project / "conf" / "team.needs-mapping.yaml")
        assert mapping_as_named(spelled, project) == "conf/team.needs-mapping.yaml"

    def test_a_relative_spelling_inside_the_project_is_named_relative_to_the_project(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project / "conf")
        assert mapping_as_named("../conf/team.needs-mapping.yaml", project) == "conf/team.needs-mapping.yaml"

    def test_a_project_root_spelled_through_a_symlink_is_the_same_directory(
        self, project: Path, tmp_path: Path
    ) -> None:
        link = tmp_path / "link"
        symlink_or_skip(link, project)
        spelled = str(project / "conf" / "team.needs-mapping.yaml")
        assert mapping_as_named(spelled, link) == "conf/team.needs-mapping.yaml"

    def test_a_mapping_outside_the_project_is_named_as_spelled(self, project: Path, tmp_path: Path) -> None:
        outside = tmp_path / "team.needs-mapping.yaml"
        outside.write_text("{}\n", encoding="utf-8", newline="\n")
        assert mapping_as_named(str(outside), project) == str(outside)

    def test_a_case_differing_spelling_is_inside_where_the_filesystem_folds_case(
        self, project: Path, tmp_path: Path
    ) -> None:
        if case_sensitive_under(tmp_path):
            pytest.skip("the file system under tmp_path does not fold case")
        spelled = str(tmp_path / "PROJECT" / "conf" / "team.needs-mapping.yaml")
        assert mapping_as_named(spelled, project) == "conf/team.needs-mapping.yaml"

    def test_an_os_error_from_the_identity_check_names_the_mapping_as_spelled(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(first: object, second: object) -> bool:
            raise PermissionError("denied")

        monkeypatch.setattr(requirements_import.os.path, "samefile", refuse)
        spelled = str(project / "conf" / "team.needs-mapping.yaml")
        assert mapping_as_named(spelled, project) == spelled

    def test_a_spelling_through_a_missing_folder_is_named_by_its_resolved_path(
        self, project: Path, tmp_path: Path
    ) -> None:
        """The spelled path's check fails on the missing folder, and the resolved one is tried."""
        spelled = str(tmp_path / "missing" / ".." / "project" / "conf" / "team.needs-mapping.yaml")
        assert not (tmp_path / "missing").exists()
        assert mapping_as_named(spelled, project) == "conf/team.needs-mapping.yaml"

    def test_the_report_still_escapes_what_the_helper_returns(self, root: Path) -> None:
        report = render_report(plan(root, needs()), dry_run=False, mapping=mapping_as_named("a\nb.yaml", root))
        assert report.splitlines()[1] == "Mapping: a\\nb.yaml"


class TestTheReport:
    def _every_section(self, root: Path) -> ImportPlan:
        put(root, "requirements/CHG-1.yaml", external("CHG-1", 1))
        put(root, "requirements/SRC-1.yaml", external("SRC-1", 1))
        put(root, "requirements/PAR-1.yaml", external("PAR-1", 1, "derived_from:", "- CHG-1@1"))
        put(root, "requirements/DEL-1.yaml", external("DEL-1", 2))
        put(
            root,
            "swc/A.yaml",
            "name: A\ntype: application\nsatisfies:\n- CHG-1@1\n- CHG-1@2\n- DEL-1@2\n",
        )
        return plan(
            root,
            needs(
                need("NEW-1", derived_from=["SG-9"]),
                need("CHG-1", title="Changed"),
                need("DEL-1", status="rejected"),
                need("GONE-1", status="rejected"),
                need("SRC-1", document="SRS"),
                need("PAR-1", derived_from=["CHG-1"]),
            ),
        )

    def test_every_section_in_order(self, root: Path) -> None:
        assert render_report(self._every_section(root), dry_run=False) == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 1 added, 1 changed, 1 removed, 1 stubs created, 1 source updates\n"
            "\n"
            "Added:\n"
            "  NEW-1 (v1)\n"
            "\n"
            "Stubs created:\n"
            "  SG-9 (v1), named by NEW-1\n"
            "\n"
            "Changed:\n"
            "  CHG-1 v1 → v2 (title)\n"
            "    text: numbers removed 1; terms added: changed; terms removed: chg, title\n"
            "\n"
            "Removed:\n"
            "  DEL-1 (was v2; status 'rejected')\n"
            "\n"
            "Removed by status, not in the model:\n"
            "  GONE-1 (status 'rejected')\n"
            "\n"
            "Source information updated:\n"
            "  SRC-1 (document)\n"
            "\n"
            "Parent versions updated:\n"
            "  PAR-1 (CHG-1@2)\n"
            "\n"
            "Links now needing confirmation:\n"
            "  SWC 'A' satisfies 'CHG-1@1'\n"
            "\n"
            "Links confirmed by this import:\n"
            "  SWC 'A' satisfies 'CHG-1@2'\n"
            "\n"
            "Links now naming no requirement:\n"
            "  SWC 'A' satisfies 'DEL-1@2'\n"
        )

    def test_the_dry_run_header(self, root: Path) -> None:
        lines = render_report(self._every_section(root), dry_run=True).splitlines()
        assert lines[:4] == [
            "Requirements import: baseline SRS-1 (from the file)",
            "Mapping: none (SWCraft's own names)",
            "Dry run: nothing was written.",
            "Summary: 1 added, 1 changed, 1 removed, 1 stubs created, 1 source updates",
        ]

    def test_the_summary_grammar_with_every_count_non_zero(self, root: Path) -> None:
        put(root, "requirements/X-1.yaml", external("X-1", 1))
        put(root, "requirements/Y-1.yaml", external("Y-1", 1))
        put(root, "requirements/Z-1.yaml", external("Z-1", 1))
        result = plan(
            root,
            needs(
                need("A-1", derived_from=["S-1", "S-2"]),
                need("A-2"),
                need("X-1", title="T"),
                need("Y-1", status="rejected"),
                need("Z-1", revision="2"),
            ),
        )
        for dry_run in (False, True):
            report = render_report(result, dry_run=dry_run)
            summaries = [line for line in report.splitlines() if line.startswith("Summary:")]
            assert len(summaries) == 1
            match = SUMMARY.fullmatch(summaries[0])
            assert match is not None and match.groups() == ("2", "1", "1", "2", "1")
            assert summaries[0].isascii()
            header = report.split("\n\n", 1)[0].splitlines()
            assert header[-1] == summaries[0]

    def test_the_summary_grammar_on_a_no_change_run(self, root: Path) -> None:
        result = plan(root, needs())
        for dry_run in (False, True):
            lines = render_report(result, dry_run=dry_run).splitlines()
            assert SUMMARY.fullmatch(lines[-2]) is not None
            assert SUMMARY.fullmatch(lines[-2]).groups() == ("0", "0", "0", "0", "0")
            assert lines[-1] == "No change."

    def test_a_dry_run_report_of_nothing(self, root: Path) -> None:
        assert render_report(plan(root, needs()), dry_run=True) == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Dry run: nothing was written.\n"
            "Summary: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates\n"
            "No change.\n"
        )

    def test_a_baseline_holding_a_summary_line_prints_one_summary(self, root: Path) -> None:
        # Guards the injected line printing ahead of the real one.
        result = plan(root, needs(need("SWR-1"), baseline=f"SRS-1{INJECTED}"))
        for dry_run in (False, True):
            report = render_report(result, dry_run=dry_run)
            assert [line for line in report.splitlines() if SUMMARY.match(line)] == [
                "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates"
            ]
            assert report.splitlines()[0] == f"Requirements import: baseline SRS-1\\n{INJECTED[1:]} (from the file)"

    @pytest.mark.parametrize("field", ["title", "document"])
    def test_a_title_or_a_provenance_value_holding_a_summary_line_prints_one_summary(
        self, root: Path, field: str
    ) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        result = plan(root, needs(need("SWR-1", **{field: f"T{INJECTED}"}), need("SWR-2", **{field: f"T{INJECTED}"})))
        counts = "1 added, 1 changed" if field == "title" else "1 added, 0 changed"
        sources = 0 if field == "title" else 1
        report = render_report(result, dry_run=True)
        assert [line for line in report.splitlines() if SUMMARY.match(line)] == [
            f"Summary: {counts}, 0 removed, 0 stubs created, {sources} source updates"
        ]

    def test_every_line_break_and_control_in_a_value_is_a_visible_escape(self, root: Path) -> None:
        breaks = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\t\x1b\x7f\x9f"
        result = plan(root, needs(need("GONE-1\nX", status="rejected"), baseline=f"B{breaks}"))
        report = render_report(result, dry_run=False)
        assert report.splitlines() == report.split("\n")[:-1]
        assert section(report, "Removed by status, not in the model") == ["GONE-1\\nX (status 'rejected')"]
        assert report.splitlines()[0] == (
            "Requirements import: baseline B\\n\\r\\x0b\\x0c\\x1c\\x1d\\x1e\\x85\\u2028\\u2029\\t\\x1b\\x7f\\x9f"
            " (from the file)"
        )

    def test_the_report_names_no_storage(self, root: Path) -> None:
        put(root, "requirements/swr-1.yaml", external("swr-1"))
        report = render_report(plan(root, needs(need("SWR-1"), need("swr-1", status="rejected"))), dry_run=False)
        assert ".yaml" not in report and str(root) not in report


# ---------------------------------------------------------------------------
# What changed, printed under each entry of *Changed*
# ---------------------------------------------------------------------------


def worded(requirement_id: str, version: int, description: str, *extra: str) -> str:
    """:func:`external`'s file with *description* in place of the default text."""
    return external(requirement_id, version, *extra).replace(
        f"description: Text of {requirement_id}.\n", f"description: {description}\n"
    )


#: The ``text:`` line of a text whose words and numbers did not move.
WORDING_ONLY = (
    "text: wording only (the same terms and numbers; order, case, spacing, punctuation or common words changed)"
)


class TestTheChangedDetail:
    def _detail(self, root: Path, held: str, incoming: dict[str, Any]) -> list[str]:
        put(root, "requirements/SWR-1.yaml", held)
        report = render_report(plan(root, needs(incoming)), dry_run=False)
        (entry,) = section(report, "Changed")
        return details(report, entry)

    def test_the_planned_example_prints_the_text_and_the_level_under_the_entry(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", worded("SWR-1", 3, "Limit the temperature to 600 degC.", "asil: B"))
        result = plan(root, needs(need("SWR-1", content="Limit the overheat to 650 degC.", asil="D")))
        assert result.changed[0].detail == (
            "text: numbers 600 → 650; terms added: overheat; terms removed: temperature",
            "ASIL: B → D",
        )
        assert render_report(result, dry_run=False).split("\n\n")[1] == (
            "Changed:\n"
            "  SWR-1 v3 → v4 (description, asil)\n"
            "    text: numbers 600 → 650; terms added: overheat; terms removed: temperature\n"
            "    ASIL: B → D\n"
        )

    def test_a_replaced_run_of_unequal_length(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Limits 10 and 20 apply.")
        assert self._detail(root, held, need("SWR-1", content="Limits 15 apply.")) == ["text: numbers 10, 20 → 15"]

    def test_a_replaced_run_of_equal_length_prints_pairs(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Limits 10 and 20 apply.")
        assert self._detail(root, held, need("SWR-1", content="Limits 11 and 21 apply.")) == [
            "text: numbers 10 → 11, 20 → 21"
        ]

    def test_removed_and_added_numbers(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Hold 5 s, then 7 s.")
        assert self._detail(root, held, need("SWR-1", content="Hold 7 s, then 9 s.")) == [
            "text: numbers removed 5; added 9"
        ]

    def test_terms_added_and_removed_are_sorted(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Brake pressure monitored.")
        assert self._detail(root, held, need("SWR-1", content="Valve torque actuator monitored.")) == [
            "text: terms added: actuator, torque, valve; terms removed: brake, pressure"
        ]

    def test_a_title_change_is_read_with_the_description_as_one_text(self, root: Path) -> None:
        # The title's word moved into the description: one text, so no word moved.
        held = worded("SWR-1", 1, "Monitor it.")
        incoming = need("SWR-1", title="Title of SWR-1 Monitor", content="It.")
        assert self._detail(root, held, incoming) == [WORDING_ONLY]

    def test_wording_only_is_the_same_words_in_another_order_case_spacing_or_punctuation(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Brake pressure is monitored.")
        assert self._detail(root, held, need("SWR-1", content="Monitored is  the BRAKE-pressure;")) == [WORDING_ONLY]
        assert self._detail(root, held, need("SWR-1", content="Is monitored:  BRAKE-pressure")) == [WORDING_ONLY]

    @pytest.mark.parametrize(
        ("old", "new", "line"),
        [
            ("The brake shall be applied.", "The brake shall not be applied.", "text: terms added: not"),
            (
                "The brake shall be applied.",
                "The brake may be applied.",
                "text: terms added: may; terms removed: shall",
            ),
            (
                "Brake pressure is monitored.",
                "Brake pressure shall be monitored.",
                "text: terms added: shall",
            ),
            ("Only the brake is applied.", "The brake is applied.", "text: terms removed: only"),
            ("The brake shall be applied.", "The brake shall never be applied.", "text: terms added: never"),
            ("The brake can be applied.", "The brake must be applied.", "text: terms added: must; terms removed: can"),
        ],
        ids=["negation", "modal", "is-to-shall-be", "only", "never", "can-to-must"],
    )
    def test_a_stop_word_that_moved_is_named(self, root: Path, old: str, new: str, line: str) -> None:
        """A negation, a modal or a restriction changes the meaning: never *wording only*."""
        assert self._detail(root, worded("SWR-1", 1, old), need("SWR-1", content=new)) == [line]

    def test_common_words_and_a_unit_letter_are_not_named(self, root: Path) -> None:
        """The showcase's line ``terms added: at, for, most, s``: only ``most`` is a term.

        ``at most`` against ``at least`` changes the bound, so ``most`` is named; ``at``, ``for`` and the
        ``s`` of ``60 s`` are not, and the numbers stay on their own piece.
        """
        held = worded("SWR-1", 1, "The pump stops on a fault.")
        incoming = need("SWR-1", content="The pump stops on a fault for at most 60 s.")
        assert self._detail(root, held, incoming) == ["text: numbers added 60; terms added: most"]

    def test_only_common_words_moved_is_wording_only(self, root: Path) -> None:
        held = worded("SWR-1", 1, "The pump stops on a fault.")
        assert self._detail(root, held, need("SWR-1", content="The pump stops at the fault.")) == [WORDING_ONLY]

    def test_a_single_digit_glued_to_letters_is_still_named(self, root: Path) -> None:
        """Only a single *letter* is left unnamed: ``SR2`` -> ``SR3`` is a change."""
        held = worded("SWR-1", 1, "Limit SR2 to the cap.")
        assert self._detail(root, held, need("SWR-1", content="Limit SR3 to the cap.")) == [
            "text: terms added: 3; terms removed: 2"
        ]

    def test_a_number_glued_to_letters_is_a_term_that_moved(self, root: Path) -> None:
        """``SR600`` holds no number by the number rule, so its digits are reported as terms, never as no change."""
        held = worded("SWR-1", 1, "Limit SR600 to the cap.")
        assert self._detail(root, held, need("SWR-1", content="Limit SR650 to the cap.")) == [
            "text: terms added: 650; terms removed: 600"
        ]

    def test_digits_of_a_number_that_moved_are_not_reported_twice(self, root: Path) -> None:
        held = worded("SWR-1", 1, "Hold 5 s at 600 kPa.")
        assert self._detail(root, held, need("SWR-1", content="Hold 5 s at 650 kPa.")) == ["text: numbers 600 → 650"]

    def test_an_identifier_holding_a_number_that_moved_too_is_still_a_term(self, root: Path) -> None:
        """The number rule's digits are taken away once per number, so the ``SR600`` beside a ``600`` still counts."""
        held = worded("SWR-1", 1, "Limit SR600 to 600.")
        assert self._detail(root, held, need("SWR-1", content="Limit SR700 to 700.")) == [
            "text: numbers 600 → 700; terms added: 700; terms removed: 600"
        ]

    def test_the_type(self, root: Path) -> None:
        assert self._detail(root, external("SWR-1", 1), need("SWR-1", type="spec")) == ["type: req → spec"]

    @pytest.mark.parametrize(
        ("held", "incoming", "line"),
        [
            ("B", "D", "ASIL: B → D"),
            (None, "D", "ASIL set: D"),
            ("B", None, "ASIL cleared (was B)"),
        ],
    )
    def test_the_three_asil_lines(self, root: Path, held: str | None, incoming: str | None, line: str) -> None:
        extra = () if held is None else (f"asil: {held}",)
        fields = {} if incoming is None else {"asil": incoming}
        assert self._detail(root, external("SWR-1", 1, *extra), need("SWR-1", **fields)) == [line]

    def test_parents_added_and_removed(self, root: Path) -> None:
        for parent in ("SG-1", "SG-2", "SG-3"):
            put(root, f"requirements/{parent}.yaml", architecture(parent))
        held = external("SWR-1", 1, "derived_from:", "- SG-1@1", "- SG-2@1")
        assert self._detail(root, held, need("SWR-1", derived_from=["SG-3", "SG-1"])) == [
            "parents added: SG-3; removed: SG-2"
        ]

    def test_parents_removed_alone(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1"))
        held = external("SWR-1", 1, "derived_from:", "- SG-1@1")
        assert self._detail(root, held, need("SWR-1")) == ["parents removed: SG-1"]

    def test_every_line_in_its_order(self, root: Path) -> None:
        put(root, "requirements/SG-1.yaml", architecture("SG-1"))
        held = worded("SWR-1", 1, "Hold 5 s.", "asil: A")
        incoming = need("SWR-1", content="Hold 6 s.", type="spec", asil="C", derived_from=["SG-1"])
        assert self._detail(root, held, incoming) == [
            "text: numbers 5 → 6",
            "type: req → spec",
            "ASIL: A → C",
            "parents added: SG-1",
        ]

    def test_a_control_character_in_a_detail_value_is_printed_escaped(self, root: Path) -> None:
        # A term or a number holds letters and digits only, so the value that can carry a
        # control character into a detail line is a parent member held in the element form.
        held = external("SWR-1", 1, "derived_from:", '- "hardware:Core\\e0"')
        report_lines = self._detail(root, held, need("SWR-1"))
        assert report_lines == ["parents removed: hardware:Core\\x1b0"]

    def test_a_replaced_stub_prints_no_detail(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        result = plan(root, needs(need("SG-9", content="The real text.")))
        assert result.changed[0].detail == ()
        report = render_report(result, dry_run=False)
        assert details(report, "SG-9 v1 → v2 (stub replaced)") == []

    def test_a_change_that_moves_only_provenance_prints_no_changed_entry(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        report = render_report(plan(root, needs(need("SWR-1", document="SRS"))), dry_run=False)
        assert section(report, "Changed") == [] and "    " not in report


# ---------------------------------------------------------------------------
# The apply -- staged, all or nothing, no audit record
# ---------------------------------------------------------------------------


def _mixed_run(root: Path) -> ImportPlan:
    """A plan that adds two files, changes one and removes one -- every kind of step the apply takes."""
    put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
    put(root, "requirements/SWR-2.yaml", external("SWR-2", 1))
    result = plan(
        root,
        needs(
            need("SWR-1", title="New title"),
            need("SWR-2", status="rejected"),
            need("SWR-3", derived_from=["SG-9"]),
        ),
    )
    assert [planned.target for planned in result.writes] == [
        "requirements/SWR-1.yaml",
        "requirements/SWR-3.yaml",
        "requirements/SG-9.yaml",
    ]
    assert result.removals == ("requirements/SWR-2.yaml",)
    return result


class TestTheApply:
    def test_an_applied_run_writes_the_planned_texts_and_removes_the_rejected_file(self, root: Path) -> None:
        result = _mixed_run(root)
        before = tree(root)
        touched = apply_requirements_import(result)
        assert touched == [
            "requirements/SWR-1.yaml",
            "requirements/SWR-3.yaml",
            "requirements/SG-9.yaml",
            "requirements/SWR-2.yaml",
        ]
        after = tree(root)
        assert after == {
            **{target: data for target, data in before.items() if target != "requirements/SWR-2.yaml"},
            **{planned.target: planned.text.encode("utf-8") for planned in result.writes},
        }
        assert after["requirements/SWR-1.yaml"] == external("SWR-1", 3).replace(
            "title: Title of SWR-1", "title: New title"
        ).encode("utf-8")

    def test_every_touched_path_is_under_requirements(self, root: Path) -> None:
        put(root, "swc/example/ExampleSWC.yaml", "name: ExampleSWC\ntype: application\nsatisfies:\n- SWR-1@2\n")
        result = _mixed_run(root)
        before = tree(root)
        apply_requirements_import(result)
        after = tree(root)
        moved = {target for target in before.keys() | after.keys() if before.get(target) != after.get(target)}
        assert moved == {
            "requirements/SWR-1.yaml",
            "requirements/SWR-2.yaml",
            "requirements/SWR-3.yaml",
            "requirements/SG-9.yaml",
        }

    @pytest.mark.parametrize("example", ["profile-example.json", "two-versions.json", "minimal.json"])
    def test_no_applied_file_holds_an_undeclared_key(self, root: Path, example: str) -> None:
        """`import requirements` writes no undeclared key.

        The interchange examples (every field the import writes, a parent the run
        brings changed, a stub) and the mixed run (a change, a removal, a stub).
        What this does not reach: a need shape none of them holds.
        """
        result = plan(root, read_needs_file(EXAMPLES / example))
        assert result.writes
        apply_requirements_import(result)
        assert check_unknown_keys(str(root)).issues == []

    def test_no_file_of_a_mixed_run_holds_an_undeclared_key(self, root: Path) -> None:
        apply_requirements_import(_mixed_run(root))
        assert check_unknown_keys(str(root)).issues == []

    def test_an_imported_file_is_the_bytes_write_element_writes(self, root: Path, tmp_path: Path) -> None:
        result = plan(root, read_needs_file(EXAMPLES / "profile-example.json"))
        apply_requirements_import(result)
        other = make_empty_model_root(tmp_path / "other")
        for planned in result.writes:
            element = Requirement.model_validate(_loaded(root / planned.target))
            write_element(str(other), planned.target, element, create=True)
            assert (other / planned.target).read_bytes() == (root / planned.target).read_bytes()

    def test_the_provenance_is_written_quoted_and_loads_back_as_strings(self, root: Path) -> None:
        result = plan(root, needs(need("SWR-1", document="7", revision="7"), baseline="2026-10-18"))
        apply_requirements_import(result)
        text = (root / "requirements/SWR-1.yaml").read_text(encoding="utf-8")
        assert "document: '7'\n" in text
        assert "baseline: '2026-10-18'\n" in text
        assert "revision: '7'\n" in text
        loaded = Requirement.model_validate(_loaded(root / "requirements/SWR-1.yaml"))
        assert (loaded.document, loaded.baseline, loaded.revision) == ("7", "2026-10-18", "7")

    def test_a_replaced_stub_is_written_without_the_stub_flag(self, root: Path) -> None:
        put(root, "requirements/SG-9.yaml", _stub_text("SG-9", "SWR-1"))
        apply_requirements_import(plan(root, needs(need("SG-9"))))
        text = (root / "requirements/SG-9.yaml").read_text(encoding="utf-8")
        assert "stub" not in text
        assert "version: 2\n" in text

    def test_a_run_that_changes_nothing_touches_nothing(self, root: Path) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        result = plan(root, needs(need("SWR-1")))
        before = tree(root)
        assert apply_requirements_import(result) == []
        assert tree(root) == before

    def test_a_staging_name_is_this_call_s_own(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = plan(root, needs(need("SWR-1")))
        (root / "requirements").mkdir()
        foreign = root / "requirements/SWR-1.yaml.tmp"  # the fixed name an earlier apply used
        foreign.write_text("another process's\n", encoding="utf-8", newline="\n")
        real_replace = os.replace
        sources: list[str] = []

        def replace(src: Any, dst: Any) -> None:
            sources.append(Path(src).name)
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        apply_requirements_import(result)
        assert len(sources) == 1 and re.fullmatch(r"SWR-1\.yaml\.[0-9a-f]{16}\.tmp", sources[0]), sources
        assert foreign.read_text(encoding="utf-8") == "another process's\n"

    def test_two_interleaved_applies_never_take_each_other_s_staging(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second import runs whole between the first one's staged write and its rename; both complete.

        With a fixed ``.tmp`` name the second would overwrite and rename the first's staged
        file, and the first's rename would then find nothing. The targets themselves still
        race: the last rename wins.
        """
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 1))
        first = plan(root, needs(need("SWR-1", title="First")))
        second = plan(root, needs(need("SWR-1", title="Second"), need("SWR-2")))
        real_replace = os.replace
        interleaved: list[bool] = []

        def replace(src: Any, dst: Any) -> None:
            if not interleaved:
                interleaved.append(True)
                apply_requirements_import(second)
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        apply_requirements_import(first)
        assert "title: First\n" in (root / "requirements/SWR-1.yaml").read_text(encoding="utf-8")
        assert (root / "requirements/SWR-2.yaml").exists()
        assert not list(root.rglob("*.tmp"))

    @pytest.mark.parametrize("failing", [1, 2, 3])
    def test_a_failure_at_the_kth_rename_restores_every_file(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, failing: int
    ) -> None:
        result = _mixed_run(root)
        before = tree(root)
        real_replace = os.replace
        calls = []

        def replace(src: Any, dst: Any) -> None:
            calls.append(dst)
            if len(calls) == failing:
                raise OSError("injected rename failure")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(OSError, match="injected rename failure"):
            apply_requirements_import(result)
        assert tree(root) == before
        assert not list(root.rglob("*.tmp"))

    def test_a_failure_at_a_removal_restores_every_written_file(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _mixed_run(root)
        before = tree(root)
        removal = root / "requirements/SWR-2.yaml"
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self == removal:
                raise PermissionError("injected removal failure")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError, match="injected removal failure"):
            apply_requirements_import(result)
        monkeypatch.undo()
        assert tree(root) == before
        assert not list(root.rglob("*.tmp"))

    def test_a_failure_at_the_second_removal_restores_the_first(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        put(root, "requirements/SWR-1.yaml", external("SWR-1", 2))
        put(root, "requirements/SWR-2.yaml", external("SWR-2", 1))
        result = plan(root, needs(need("SWR-1", status="rejected"), need("SWR-2", status="rejected")))
        assert result.removals == ("requirements/SWR-1.yaml", "requirements/SWR-2.yaml")
        before = tree(root)
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self == root / "requirements/SWR-2.yaml":
                raise PermissionError("injected removal failure")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError, match="injected removal failure"):
            apply_requirements_import(result)
        monkeypatch.undo()
        assert tree(root) == before

    @pytest.mark.parametrize("failing", [1, 2, 3])
    def test_a_failure_at_the_kth_staged_write_restores_everything(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, failing: int
    ) -> None:
        result = plan(root, needs(need("SWR-1"), need("SWR-2"), need("SWR-3")))
        before = tree(root)
        directories = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
        real_write_text = Path.write_text
        calls = []

        def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            calls.append(self)
            if len(calls) == failing:
                real_write_text(self, "partial", encoding="utf-8", newline="\n")
                raise OSError("injected write failure")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        with pytest.raises(OSError, match="injected write failure"):
            apply_requirements_import(result)
        monkeypatch.undo()
        assert tree(root) == before
        assert not list(root.rglob("*.tmp"))
        assert not (root / "requirements").exists()
        assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()) == directories

    def test_a_restore_that_fails_does_not_stop_the_rollback_and_is_named(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _mixed_run(root)
        before = tree(root)
        real_unlink, real_write_bytes = Path.unlink, Path.write_bytes

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self == root / "requirements/SWR-2.yaml":
                raise PermissionError("injected removal failure")
            real_unlink(self, missing_ok=missing_ok)

        def write_bytes(self: Path, data: Any) -> int:
            if self == root / "requirements/SWR-1.yaml":
                raise PermissionError("injected restore failure")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "unlink", unlink)
        monkeypatch.setattr(Path, "write_bytes", write_bytes)
        with pytest.raises(PermissionError, match="injected removal failure") as caught:
            apply_requirements_import(result)
        monkeypatch.undo()
        assert caught.value.__notes__ == ["the rollback could not restore: requirements/SWR-1.yaml"]
        after = tree(root)
        # every other path is back: the created files gone, the removal never happened, no .tmp left
        assert {target: data for target, data in after.items() if target != "requirements/SWR-1.yaml"} == {
            target: data for target, data in before.items() if target != "requirements/SWR-1.yaml"
        }
        assert not list(root.rglob("*.tmp"))

    def test_a_failure_removes_the_directory_the_staging_created(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not (root / "requirements").exists()  # the empty model root has no requirements directory
        result = plan(root, needs(need("SWR-1"), need("SWR-2")))
        directories = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())

        def replace(src: Any, dst: Any) -> None:
            raise OSError("injected rename failure")

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(OSError, match="injected rename failure"):
            apply_requirements_import(result)
        assert not (root / "requirements").exists()
        assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()) == directories

    def test_the_module_names_no_audit_log(self) -> None:
        """No audit record on any run: the module imports, names and calls nothing of the audit log.

        The CLI's subprocess tests hold the same claim on disk (no ``audit.jsonl``) for an applied run.
        """
        tree_ = ast.parse(Path(requirements_import.__file__).read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree_) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree_) if isinstance(node, ast.Attribute)}
        names |= {alias.name for node in ast.walk(tree_) if isinstance(node, ast.ImportFrom) for alias in node.names}
        modules = {node.module or "" for node in ast.walk(tree_) if isinstance(node, ast.ImportFrom)}
        modules |= {alias.name for node in ast.walk(tree_) if isinstance(node, ast.Import) for alias in node.names}
        assert not {name for name in names if "audit" in name.lower()}
        assert not {module for module in modules if "audit" in module}


def _loaded(path: Path) -> Any:
    return load_yaml_document(path.read_text(encoding="utf-8"))
