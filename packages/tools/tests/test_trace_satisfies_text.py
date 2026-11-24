"""The trace Apply's text edit: only the ``satisfies`` lines change.

The golden pair under ``fixtures/trace_apply_text/`` is a hand-formatted file
and its edit; the ``before`` file is written by hand, the ``after`` file was
generated once by :func:`edit_satisfies` and read line by line -- a change
detector, never a correctness proof. The proofs are the cases below: what each
form gives, what reads back, and what is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from swcraft_tools.trace_satisfies_text import HolderChange, edit_satisfies, scalar_text
from swcraft_tools.yaml_load import load_yaml_document

FIXTURES = Path(__file__).parent / "fixtures" / "trace_apply_text"

_GOLDEN_CHANGES = [
    HolderChange(None, ["SR-7@2"]),
    HolderChange(("ports", "pBrakeTemp"), ["SR-2@1", "SR-3@4"]),
    HolderChange(("ports", "pFault"), ["SR-4@1"]),
    HolderChange(("runnables", "Supervise"), ["SR-5@1"], [("SR-1@1", "SR-1@2")]),
    HolderChange(("runnables", "Init"), ["SR-6@1"]),
]


#: What can stand before a document's first key, each ending in a mark.
_LEADS = {
    "line-break": "\n\ufeff",
    "blank-line": "\n\n\ufeff",
    "comment": "# c\n\ufeff",
    "comment-outside-the-bmp": "# \U0001f600\n\ufeff",
    "two-marks": "\n\ufeff\n\ufeff",
}
#: A holder's body, in the one spelling that composes with its first key on the mark's line:
#: everything else of the root mapping would stand left of that key.
_HOLDERS = {
    "block": (None, "satisfies:\n  - A@1  # why\n  - B@1\n"),
    "flow": (None, "satisfies: [A@1, B@1]  # why\n"),
    "nested": (("ports", "p"), "ports:\n - name: p\n   satisfies:\n   - A@1  # why\n   - B@1\n"),
}
_CHANGES = {
    "append": {"appends": ["C@1"]},
    "replace": {"confirms": [("A@1", "A@2")]},
    "deleting-confirm": {"confirms": [("A@1", "B@1")]},
}
#: Each holder with each change, and the refusal the pair ends in, the same with the mark and without.
_HOLDER_CHANGES = [
    pytest.param(
        holder,
        change,
        "a confirm that removes an item of a flow list"
        if (holder_id, change_id) == ("flow", "deleting-confirm")
        else None,
        id=f"{holder_id}-{change_id}",
    )
    for holder_id, holder in _HOLDERS.items()
    for change_id, change in _CHANGES.items()
]


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _edit(text: str, *changes: HolderChange) -> str:
    edited, refused = edit_satisfies(text, changes)
    assert refused == {}
    return edited


class TestTheGolden:
    def test_the_hand_formatted_file(self) -> None:
        before = _read("BrakeUnit.before.yaml")
        edited, refused = edit_satisfies(before, _GOLDEN_CHANGES)
        assert refused == {}
        assert edited == _read("BrakeUnit.after.yaml")

    def test_it_reads_back_as_the_old_document_plus_exactly_the_members(self) -> None:
        before: Any = load_yaml_document(_read("BrakeUnit.before.yaml"))
        after: Any = load_yaml_document(_read("BrakeUnit.after.yaml"))
        before["satisfies"] = ["SR-7@2"]
        before["ports"][0]["satisfies"] += ["SR-2@1", "SR-3@4"]
        before["ports"][1]["satisfies"] = ["SR-4@1"]
        before["runnables"][0]["satisfies"] = ["SR-1@2", "SR-5@1"]
        before["runnables"][1]["satisfies"] = ["SR-6@1"]
        assert after == before

    def test_every_line_of_the_file_stays_but_the_two_flow_lists(self) -> None:
        before = _read("BrakeUnit.before.yaml").splitlines()
        after = _read("BrakeUnit.after.yaml").splitlines()
        changed = [line for line in before if line not in after]
        assert changed == ["  satisfies: [SR-1@1]", "  satisfies: []"]


class TestTheForms:
    def test_an_existing_block_list_takes_its_last_items_prefix(self) -> None:
        text = "name: A\nsatisfies:\n  -   X@1  # kept\nother: 1\n"
        edited = _edit(text, HolderChange(None, ["Y@2"]))
        assert edited == "name: A\nsatisfies:\n  -   X@1  # kept\n  -   Y@2\nother: 1\n"

    def test_a_flow_list_and_an_empty_one(self) -> None:
        assert _edit("name: A\nsatisfies: [X@1]\n", HolderChange(None, ["Y@2", "Z@3"])) == (
            "name: A\nsatisfies: [X@1, Y@2, Z@3]\n"
        )
        assert _edit("name: A\nsatisfies: [ ]\n", HolderChange(None, ["Y@2"])) == "name: A\nsatisfies: [ Y@2]\n"

    def test_a_new_block_at_the_end_of_a_file_without_a_final_newline(self) -> None:
        assert _edit("name: A", HolderChange(None, ["Y@2"])) == "name: A\nsatisfies:\n- Y@2\n"

    def test_a_new_block_for_a_member_follows_its_name_line(self) -> None:
        text = "name: C\nports:\n- name: p  # first\n  direction: provided\n"
        assert _edit(text, HolderChange(("ports", "p"), ["Y@2"])) == (
            "name: C\nports:\n- name: p  # first\n  satisfies:\n  - Y@2\n  direction: provided\n"
        )

    def test_a_confirm_in_place_and_one_that_deletes_its_line(self) -> None:
        text = "name: A\nsatisfies:\n- X@1\n- X@2\n- Z@1\n"
        edited = _edit(text, HolderChange(None, confirms=[("Z@1", "Z@3")]))
        assert edited == "name: A\nsatisfies:\n- X@1\n- X@2\n- Z@3\n"
        assert _edit(text, HolderChange(None, confirms=[("X@1", "X@2")])) == "name: A\nsatisfies:\n- X@2\n- Z@1\n"

    def test_a_quoted_item_is_replaced_whole(self) -> None:
        text = "name: A\nsatisfies:\n- 'X@1'\n"
        assert _edit(text, HolderChange(None, ["Y@1"], [("X@1", "X@2")])) == "name: A\nsatisfies:\n- X@2\n- Y@1\n"

    def test_positions_count_characters_not_bytes(self) -> None:
        text = "name: Ä€ü\ndescription: 日本語のテキスト\nsatisfies: [X@1]\n"
        assert _edit(text, HolderChange(None, ["Y@1"])) == (
            "name: Ä€ü\ndescription: 日本語のテキスト\nsatisfies: [X@1, Y@1]\n"
        )

    @pytest.mark.parametrize(
        ("member", "written"),
        [("SR-1@2", "SR-1@2"), ("REQ_A.b-1@10", "REQ_A.b-1@10"), ("R: x@1", "'R: x@1'"), ("it's@1", "'it''s@1'")],
    )
    def test_the_scalar_written(self, member: str, written: str) -> None:
        assert scalar_text(member) == written
        assert load_yaml_document(f"- {written}\n") == [member]


class TestTheRefusals:
    @pytest.mark.parametrize(
        ("text", "change", "reason"),
        [
            ("name: A\nsatisfies: [\n  X@1]\n", HolderChange(None, ["Y@1"]), "a flow list over several lines"),
            ("name: A\nsatisfies: X@1\n", HolderChange(None, ["Y@1"]), "its satisfies is not a list"),
            ("name: A\nsatisfies:\n", HolderChange(None, ["Y@1"]), "its satisfies is not a list"),
            (
                "name: A\nsatisfies:\n- &a X@1\n",
                HolderChange(None, ["Y@1"]),
                "an item carries an anchor, a tag or more than its value on its line",
            ),
            (
                "name: A\nsatisfies:\n- [X@1]\n",
                HolderChange(None, ["Y@1"]),
                "an item is not a single value",
            ),
            (
                "name: A\nsatisfies: [X@1, X@1]\n",
                HolderChange(None, confirms=[("X@1", "X@2")]),
                "'X@1' is listed more than once",
            ),
            (
                "name: A\nsatisfies: [X@1, X@2]\n",
                HolderChange(None, confirms=[("X@1", "X@2")]),
                "a confirm that removes an item of a flow list",
            ),
            (
                "name: C\nports:\n- name: p\n- name: p\n",
                HolderChange(("ports", "p"), ["Y@1"]),
                "its ports name 'p' twice",
            ),
            ("{name: A}\n", HolderChange(None, ["Y@1"]), "the file is not a block mapping"),
            ("name: A\r\n", HolderChange(None, ["Y@1"]), "the file has carriage-return line ends"),
            ("name: A\n...\n", HolderChange(None, ["Y@1"]), "the file holds a document marker"),
            ("", HolderChange(None, ["Y@1"]), "the file is empty"),
            # Each of these would read back as something else than the edit.
            (
                "\ufeffname: A\nsatisfies: [X@1]\n",
                HolderChange(None, ["Y@1"]),
                "the file starts with a byte-order mark",
            ),
            (
                "name: C\nports:\n- name: p\n  satisfies: &s\n  - X@1\n- name: q\n  satisfies: *s\n",
                HolderChange(("ports", "q"), ["Y@1"]),
                "its satisfies is an alias, anchored or tagged",
            ),
            (
                "name: C\nports:\n- name: p\n  satisfies: &s\n  - X@1\n- name: q\n  satisfies: *s\n",
                HolderChange(("ports", "p"), ["Y@1"]),
                "its satisfies is an alias, anchored or tagged",
            ),
            (
                "name: A\nsatisfies: !!seq [X@1]\n",
                HolderChange(None, ["Y@1"]),
                "its satisfies is an alias, anchored or tagged",
            ),
            (
                "name: A\nsatisfies: [X@1]\nsatisfies: [Z@1]\n",
                HolderChange(None, ["Y@1"]),
                "its satisfies key is given twice",
            ),
        ],
    )
    def test_a_form_it_does_not_edit(self, text: str, change: HolderChange, reason: str) -> None:
        assert edit_satisfies(text, [change]) == (text, {0: reason})

    def test_a_removed_items_comment_stays_on_its_own_line(self) -> None:
        text = "name: A\nsatisfies:\n  - X@1 # rationale for this trace\n  - X@2\n"
        assert edit_satisfies(text, [HolderChange(None, confirms=[("X@1", "X@2")])]) == (
            "name: A\nsatisfies:\n  # rationale for this trace\n  - X@2\n",
            {},
        )

    @pytest.mark.parametrize("comment", ["# safety & performance", "# confirmed!", "# see *note*"])
    def test_a_comment_after_the_key_is_no_anchor(self, comment: str) -> None:
        text = f"name: A\nsatisfies: {comment}\n- X@1\n"
        assert edit_satisfies(text, [HolderChange(None, ["Y@1"])]) == (
            f"name: A\nsatisfies: {comment}\n- X@1\n- Y@1\n",
            {},
        )

    def test_a_comment_of_dashes_on_the_first_line_is_no_marker(self) -> None:
        text = "#-----\nname: A\nsatisfies: [X@1]\n"
        assert edit_satisfies(text, [HolderChange(None, ["Y@1"])]) == (
            "#-----\nname: A\nsatisfies: [X@1, Y@1]\n",
            {},
        )

    def test_a_header_marker_on_the_first_line_is_kept(self) -> None:
        text = "---\nname: A\nsatisfies: [X@1]\n"
        assert edit_satisfies(text, [HolderChange(None, ["Y@1"])]) == ("---\nname: A\nsatisfies: [X@1, Y@1]\n", {})

    def test_a_refused_holder_leaves_the_others_edited(self) -> None:
        text = "name: C\nsatisfies: [\n  X@1]\nports:\n- name: p\n"
        edited, refused = edit_satisfies(text, [HolderChange(None, ["Y@1"]), HolderChange(("ports", "p"), ["Z@1"])])
        assert refused == {0: "a flow list over several lines"}
        assert edited == "name: C\nsatisfies: [\n  X@1]\nports:\n- name: p\n  satisfies:\n  - Z@1\n"


class TestAByteOrderMarkPastTheStart:
    """A mark after a line break shifts nothing, so the holders edit as they do without it."""

    @pytest.mark.parametrize(("holder", "change", "reason"), _HOLDER_CHANGES)
    @pytest.mark.parametrize("own_line", [False, True], ids=["key-on-the-marks-line", "mark-alone-on-its-line"])
    @pytest.mark.parametrize("lead", _LEADS.values(), ids=_LEADS.keys())
    def test_a_marked_text_edits_as_the_unmarked_one(
        self,
        lead: str,
        own_line: bool,
        holder: tuple[tuple[str, str] | None, str],
        change: dict[str, Any],
        reason: str | None,
    ) -> None:
        member, body = holder
        text = lead + ("\n" if own_line else "") + body
        marks = text.count("\ufeff")
        holder_change = HolderChange(member, **change)
        edited, refused = edit_satisfies(text, [holder_change])
        expected, expected_refused = edit_satisfies(text.replace("\ufeff", ""), [holder_change])
        assert edited.replace("\ufeff", "") == expected
        assert edited.count("\ufeff") == marks
        assert refused == expected_refused
        assert "# why" in edited
        assert refused == ({} if reason is None else {0: reason})
        assert load_yaml_document(edited) == load_yaml_document(expected)

    @pytest.mark.parametrize(
        ("text", "change", "edited"),
        [
            (
                "id: X\n\ufeff# c\nsatisfies:\n- A@1\n- B@1\n",
                HolderChange(None, ["C@1"]),
                "id: X\n\ufeff# c\nsatisfies:\n- A@1\n- B@1\n- C@1\n",
            ),
            (
                "id: X\n\ufeff# c\nsatisfies:\n- A@1\n- B@1\n",
                HolderChange(None, confirms=[("A@1", "B@1")]),
                "id: X\n\ufeff# c\nsatisfies:\n- B@1\n",
            ),
            (
                "id: X\nsatisfies:\n  - A@1\n\ufeff  # c\n  - B@1\n",
                HolderChange(None, ["C@1"]),
                "id: X\nsatisfies:\n  - A@1\n\ufeff  # c\n  - B@1\n  - C@1\n",
            ),
            (
                "id: X\nsatisfies:\n  - A@1\n\ufeff  # c\n  - B@1\n",
                HolderChange(None, confirms=[("A@1", "B@1")]),
                "id: X\nsatisfies:\n\ufeff  # c\n  - B@1\n",
            ),
            (
                "id: X\nsatisfies:\n- A@1\n- B@1\n\ufeff# c\n",
                HolderChange(None, confirms=[("B@1", "A@1")]),
                "id: X\nsatisfies:\n- A@1\n\ufeff# c\n",
            ),
            (
                "id: X\nsatisfies:\n- A@1\n- B@1\n\ufeff# c\n",
                HolderChange(None, ["C@1"]),
                "id: X\nsatisfies:\n- A@1\n- B@1\n- C@1\n\ufeff# c\n",
            ),
            (
                "id: X\nsatisfies:\n- A@1\n\ufeff# c\nnext: 1\n",
                HolderChange(None, ["C@1"]),
                "id: X\nsatisfies:\n- A@1\n- C@1\n\ufeff# c\nnext: 1\n",
            ),
            (
                "id: X\n\ufeff# c\nsatisfies:\n- A@1\n- B@1\n",
                HolderChange(None, confirms=[("A@1", "A@2")]),
                "id: X\n\ufeff# c\nsatisfies:\n- A@2\n- B@1\n",
            ),
            (
                "id: X\nports:\n- name: p\n\ufeff# c\n- name: q\n",
                HolderChange(("ports", "p"), ["C@1"]),
                "id: X\nports:\n- name: p\n  satisfies:\n  - C@1\n\ufeff# c\n- name: q\n",
            ),
        ],
        ids=[
            "before-the-key-append",
            "before-the-key-deleting-confirm",
            "between-the-items-append",
            "between-the-items-deleting-confirm",
            "after-the-items-deleting-confirm",
            "after-the-items-append",
            "between-the-list-and-the-next-key-append",
            "before-the-key-replace",
            "between-the-holders-nested-append",
        ],
    )
    def test_a_marked_comment_in_mid_document_is_kept(self, text: str, change: HolderChange, edited: str) -> None:
        assert edit_satisfies(text, [change]) == (edited, {})

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("id: X\nsatisfies:\n\ufeff - A@1\n  - B@1\n", "an item does not stand on a line of its own"),
            ("id: X\nsatisfies:\n  - A@1\n\ufeff - B@1\n", "an item does not stand on a line of its own"),
            ("\n\ufeff{id: X}\n", "the file is not a block mapping"),
        ],
        ids=["first-dash", "last-dash", "flow-document"],
    )
    def test_a_marked_form_it_does_not_edit(self, text: str, reason: str) -> None:
        change = HolderChange(None, ["C@1"])
        assert edit_satisfies(text, [change]) == (text, {0: reason})

    @pytest.mark.parametrize(
        ("text", "edited", "ports"),
        [
            (
                "id: X\nports:\n\ufeff - name: p\n  - name: q\n",
                "id: X\nports:\n\ufeff - name: p\n    satisfies:\n    - Y@1\n  - name: q\n",
                [{"name": "p", "satisfies": ["Y@1"]}, {"name": "q"}],
            ),
            (
                "id: X\nports:\n- \n\ufeff name: p\n  direction: x\n",
                "id: X\nports:\n- \n\ufeff name: p\n  satisfies:\n  - Y@1\n  direction: x\n",
                [{"name": "p", "satisfies": ["Y@1"], "direction": "x"}],
            ),
            (
                "id: X\nports:\n  - direction: x\n\ufeff   name: p\n",
                "id: X\nports:\n  - direction: x\n\ufeff   name: p\n    satisfies:\n    - Y@1\n",
                [{"direction": "x", "name": "p", "satisfies": ["Y@1"]}],
            ),
        ],
        ids=["dash-on-the-marks-line", "key-under-a-bare-dash", "key-after-a-sibling"],
    )
    def test_the_column_behind_a_mark_is_the_one_the_next_line_needs(
        self, text: str, edited: str, ports: list[dict[str, Any]]
    ) -> None:
        assert edit_satisfies(text, [HolderChange(("ports", "p"), ["Y@1"])]) == (edited, {})
        assert load_yaml_document(edited) == {"id": "X", "ports": ports}

    @pytest.mark.parametrize(
        ("text", "appends", "confirms", "edited", "members"),
        [
            (
                "id: X\nports:\n- name: p\n\ufeff satisfies:\n  - A@1\n",
                ["C@1"],
                [],
                "id: X\nports:\n- name: p\n\ufeff satisfies:\n  - A@1\n  - C@1\n",
                ["A@1", "C@1"],
            ),
            (
                "id: X\nports:\n- name: p\n\ufeff satisfies:\n  - A@1\n  - B@1\n",
                [],
                [("A@1", "B@1")],
                "id: X\nports:\n- name: p\n\ufeff satisfies:\n  - B@1\n",
                ["B@1"],
            ),
            (
                "id: X\nports:\n- name: p\n\ufeff satisfies: [A@1]\n",
                ["C@1"],
                [],
                "id: X\nports:\n- name: p\n\ufeff satisfies: [A@1, C@1]\n",
                ["A@1", "C@1"],
            ),
            (
                "id: X\nports:\n- name: p\n\ufeff satisfies: []\n",
                ["C@1"],
                [],
                "id: X\nports:\n- name: p\n\ufeff satisfies: [C@1]\n",
                ["C@1"],
            ),
        ],
        ids=["block-append", "block-deleting-confirm", "flow-append", "empty-flow-append"],
    )
    def test_a_satisfies_at_the_marks_indent_is_edited_in_place(
        self, text: str, appends: list[str], confirms: list[tuple[str, str]], edited: str, members: list[str]
    ) -> None:
        assert edit_satisfies(text, [HolderChange(("ports", "p"), appends, confirms)]) == (edited, {})
        assert load_yaml_document(edited) == {"id": "X", "ports": [{"name": "p", "satisfies": members}]}

    def test_an_empty_satisfies_at_the_marks_indent_is_refused(self) -> None:
        text = "id: X\nports:\n- name: p\n\ufeff satisfies:\n"
        assert edit_satisfies(text, [HolderChange(("ports", "p"), ["C@1"])]) == (
            text,
            {0: "its satisfies is not a list"},
        )
