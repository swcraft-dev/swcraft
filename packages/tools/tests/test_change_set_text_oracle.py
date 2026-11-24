"""The oracles of a change set's line edit, each judging by something other than the edit.

**Oracle 1, value parity:** the edited file validates into what the whole ``write_element`` file validates into.
**Oracle 2, writer-form identity:** on a file in the writer's form, the edited bytes are the whole write's.
**Oracle 3, stability:** on a file restyled into a foreign layout, with comments and an undeclared key planted,
every line outside the changed subtrees survives in order; this is the **only** proof for comments and undeclared
keys, which the read-back cannot see. Every outcome is held to the closed list of ``text_oracle_support``: a
read-back mismatch, an unexpected exception or a new fallback fails. The digest case shows the same model gives the
same ARXML both ways, through the real engine.

The fast stage walks ``examples/`` (one element of each kind, every mutation, every layout) and every element of
four specimens: oracles 1 and 2 under every mutation, oracle 3 in every layout under **one mutation of each shape**.
A specimen's walk is cut into :data:`SHARDS` cases that copy and read it each on its own. The ``slow`` cases are
``large-bench``, left out but for its hub, and the digests.

What this does not reach: a layout :data:`~text_oracle_support.STYLES` does not generate, for which the read-back is
the only proof and proves values, not layout; a mutation the schema refuses; the showcase; and a shape the corpus
does not hold. :data:`~text_oracle_support.ALIAS_STYLE`, an anchor and its alias at the end of a list item, is
walked over ``examples/`` alone, and over no class that refuses the undeclared pair. No element of the corpus
reaches the writer's nested array or a value changed in kind, nor the fallback for it, so the closed list's first
entry is held by :class:`TestTheClosedList` alone, and so is its third, the match budget, which no element of the
corpus comes near; its second, the limit, is met on the hub, where more than 64 items of its longest list pair off.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pydantic
import pytest
import swcraft_tools.editing.staging as staging_mod
import text_oracle_support as oracle
from conftest import REPO_ROOT, copy_specimen
from swcraft_tools.auditor import AuditLog
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing.address import FieldStep, KeyStep, MemberStep
from swcraft_tools.editing.audit import AUDIT_FILENAME
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.delta import project_delta
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.model_registry import production_registry
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.elements import (
    READ_BACK_MISMATCH,
    UNEXPECTED_EXCEPTION,
    EditedText,
    edited_text,
    write_element,
)

EXAMPLES_MODEL = REPO_ROOT / "examples" / "basic" / "model"

#: How many cases each specimen's walk is cut into, moved by the walk's measured cost. An element is the unit,
#: so no case is cheaper than its specimen's largest element (the first component of ``passport-small``).
SHARDS = {"passport-small": 12, "benchmark-default": 12, "example-minimal": 1, "map-bound": 1}
_CASES = [(name, shard) for name, shards in SHARDS.items() for shard in range(shards)]

#: Each `ModelDiff` shape against what reaches it. A row's value is
#: `(mutation shape, layer, path, verb, last step's class)`, or the reason no mutation reaches it.
_NOT_AN_EDIT = "a new or a removed element is a whole file, and the line edit never sees it"
_SHAPE_TABLE: dict[str, Any] = {
    "`LayerDiff.added` item": _NOT_AN_EDIT,
    "`LayerDiff.removed` item": _NOT_AN_EDIT,
    "`ChangedItem.changed_fields` entry for a scalar field": (
        oracle.SCALAR_ALTERED,
        "tasks",
        ("priority",),
        "update",
        FieldStep,
    ),
    "`ChangedItem.changed_fields` entry for an **object-typed** field": (
        oracle.SCALAR_ALTERED,
        "connectors",
        ("from", "swc"),
        "update",
        FieldStep,
    ),
    "`CollectionChange.added` member (list)": (oracle.LAST_DUPLICATED, "swc", ("ports",), "create", MemberStep),
    "`CollectionChange.removed` member (list)": (oracle.LAST_DROPPED, "swc", ("ports",), "remove", MemberStep),
    "`CollectionChange.changed` member (list)": (
        oracle.SCALAR_ALTERED,
        "swc",
        ("ports", 0, "description"),
        "update",
        FieldStep,
    ),
    "`CollectionChange.added` / `.removed` key (map)": (oracle.ENTRY_ADDED, "swc", ("memory",), "create", KeyStep),
    "`CollectionChange.changed` entry (map)": (oracle.ENTRY_CHANGED, "swc", ("memory", "RAM0"), "update", KeyStep),
    "`CollectionChange.reordered`": (oracle.REVERSED, "compositions", ("members",), "update", FieldStep),
}


#: What each layout must show on the corpus, so a style that stops producing its form fails here
#: rather than leaving oracle 3 on the default layout. `None`: it differs from the default layout.
_STYLE_FORMS: dict[str, str | None] = {
    "reversed key order": None,
    "flow lists": r": \[.+\]",
    "multi-line flow lists": r": \[\r?\n",
    "indented lists": r"^  - ",
    "4-space": r"^    - ",
    "single-quoted strings": r"^- '|: '",
    "literal block scalars": r": \|-?\r?\n",
    "folded block scalars": r": >-\r?\n",
    "multi-line plain scalars": r"^ *\w+: [\w.,()/-]+\r?\n +[\w(]",
    "value on the next line": r":\r?\n# planted \d+\r?\n +[^-\s#]",
    "wide dash": r"^-   ",
    "CRLF": r"\r\n",
    "CRLF, 4-space, single-quoted": r"^    - [^\n]*\r\n|: '[^\n]*'\r\n",
    "comments alone": r"^# planted",
    "blank lines": r"^\n# planted",
    "blank lines after block scalars": r"^ \n\n# planted",
    "trailing comments": r"  # trailing ",
}


def _held(ledger: oracle.Ledger) -> None:
    """The ledger holds no failure, and the run proved something."""
    assert ledger.lines, "no mutation came out as a line edit"
    assert not ledger.failures, "\n".join(ledger.failures[:20]) + f"\n... {len(ledger.failures)} in all"


def _reaches_every_shape(ledger: oracle.Ledger) -> None:
    """The corpus still reaches each shape but the one no element reaches, the module's claim."""
    assert set(ledger.shapes) == set(oracle.SHAPES) - {oracle.KIND_CHANGED}


def _first_of(layer: str, path: tuple[Any, ...], shape: str) -> tuple[oracle.Stored, oracle.Mutation]:
    for element in oracle.stored_elements(EXAMPLES_MODEL):
        if element.layer == layer:
            for mutation in oracle.mutations(element.model):
                if mutation.shape == shape and mutation.path[: len(path)] == path:
                    return element, mutation
    raise AssertionError(f"examples/ holds no {shape} of {layer}/{path}")


class TestTheProtocolShapes:
    """Every verb and shape of the delta projection against the mutation that reaches it."""

    @pytest.mark.parametrize("row", [row for row, reach in _SHAPE_TABLE.items() if not isinstance(reach, str)])
    def test_a_rows_mutation_reaches_its_shape(self, row: str, tmp_path: Path) -> None:
        shape, layer, path, verb, step = _SHAPE_TABLE[row]
        element, mutation = _first_of(layer, path, shape)
        model_root = tmp_path / "model"
        shutil.copytree(EXAMPLES_MODEL, model_root)
        baseline = load_snapshot(str(model_root))
        write_element(str(model_root), element.target, mutation.model)
        staged = load_snapshot(str(model_root))

        entries = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)

        assert entries[0].operation == verb
        assert isinstance(entries[0].address.path[-1], step)
        if shape == oracle.SCALAR_ALTERED and len(path) == 3:
            assert isinstance(entries[0].address.path[1], MemberStep), "a changed member recurses"

    def test_the_map_rows_other_half_a_removed_key(self, tmp_path: Path) -> None:
        element, mutation = _first_of("swc", ("memory",), oracle.ENTRY_REMOVED)
        model_root = tmp_path / "model"
        shutil.copytree(EXAMPLES_MODEL, model_root)
        baseline = load_snapshot(str(model_root))
        write_element(str(model_root), element.target, mutation.model)
        staged = load_snapshot(str(model_root))

        entries = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)

        assert entries[0].operation == "remove"
        assert isinstance(entries[0].address.path[-1], KeyStep)


@pytest.fixture(scope="module")
def elements() -> list[oracle.Stored]:
    return oracle.one_of_each_kind(oracle.stored_elements(EXAMPLES_MODEL))


class TestTheExamples:
    """The fast stage: one element of each kind, every mutation."""

    def test_the_corpus_holds_the_kinds(self, elements: list[oracle.Stored]) -> None:
        assert {element.layer for element in elements} >= {"swc", "interfaces", "connectors", "tasks", "compositions"}

    def test_parity_and_writer_form_identity(self, elements: list[oracle.Stored], tmp_path: Path) -> None:
        ledger = oracle.run_parity_and_identity(EXAMPLES_MODEL, tmp_path, elements, oracle.every_mutation)
        _held(ledger)
        _reaches_every_shape(ledger)
        assert not ledger.fallbacks

    def test_stability_in_every_layout(self, elements: list[oracle.Stored], tmp_path: Path) -> None:
        ledger = oracle.run_stability(tmp_path, elements, oracle.every_mutation)
        _held(ledger)
        _reaches_every_shape(ledger)
        assert not ledger.fallbacks

    def test_the_schema_refuses_no_more_than_it_takes(self, elements: list[oracle.Stored]) -> None:
        refused: Counter[str] = Counter()
        kept = sum(len(list(oracle.mutations(element.model, refused=refused))) for element in elements)
        # A change of kind the schema refuses is what that shape is for, so it does not count.
        del refused[oracle.KIND_CHANGED]
        assert refused.total() <= kept, refused

    def test_every_layout_makes_its_form(self, elements: list[oracle.Stored]) -> None:
        assert sorted(_STYLE_FORMS) == sorted(style.name for style in oracle.STYLES)
        ledger = oracle.Ledger()

        def corpus(style: oracle.Style) -> str:
            return "\n".join(
                oracle.restyled(oracle.writer_dict(element.model), style, type(element.model), ledger)
                for element in elements
            )

        default = corpus(oracle.Style("default"))
        for style in oracle.STYLES:
            form = _STYLE_FORMS[style.name]
            if form is None:
                assert corpus(style) != default, style.name
            else:
                assert re.search(form, corpus(style), re.MULTILINE), style.name

    def test_an_edit_keeps_the_planted_key_and_every_comment_of_an_open_class(
        self, elements: list[oracle.Stored], tmp_path: Path
    ) -> None:
        swc = next(element for element in elements if element.layer == "swc")
        target = tmp_path / swc.target
        target.parent.mkdir(parents=True)
        mutation = next(m for m in oracle.mutations(swc.model) if m.path == ("description",))
        ledger = oracle.Ledger()
        old = oracle.writer_dict(swc.model)
        for style in oracle.STYLES:
            text = oracle.restyled(old, style, type(swc.model), ledger)
            target.write_bytes(text.encode("utf-8"))

            outcome = edited_text(str(tmp_path), swc.target, mutation.model, lambda: swc.model)

            assert outcome.text is not None, style.name
            assert (oracle.PLANTED_KEY in text) is style.plants_keys, style.name
            assert outcome.text.count(oracle.PLANTED_KEY) == text.count(oracle.PLANTED_KEY), style.name
            assert outcome.text.count("# planted") == text.count("# planted"), style.name
        assert not ledger.skipped_plants

    def test_a_carried_rename_keeps_every_line_but_the_identity_line(
        self, elements: list[oracle.Stored], tmp_path: Path
    ) -> None:
        # Oracle 3 for a rename's create: the removed element's text, in
        # every layout, comments and the undeclared key planted, is edited into the new element, and
        # only the identity's line moves. No pass D runs here, so the key is planted as anywhere else.
        registry = production_registry()
        ledger = oracle.Ledger()
        for element in oracle.one_of_each_kind(elements):
            field_name = registry.layer(element.layer).identity_field
            renamed = element.model.model_copy(update={field_name: f"{element.identity}Renamed"})
            old = oracle.writer_dict(element.model)
            for style in oracle.STYLES:
                text = oracle.restyled(old, style, type(element.model), ledger)
                where = (element.layer, element.identity, style.name)

                outcome = edited_text(str(tmp_path), "unread.yaml", renamed, lambda: element.model, source_text=text)

                assert outcome.text is not None, (*where, outcome.reason)
                before, after = text.splitlines(keepends=True), outcome.text.splitlines(keepends=True)
                matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
                moved = [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]
                assert len(moved) == 1, (*where, moved)
                # One run of lines moves, the identity's: the scalar is written in the writer's form, so a
                # quoted or block identity comes out plain, and the run is held to naming the old identity
                # on the left and the new one on the right.
                _tag, old_start, old_end, new_start, new_end = moved[0]
                assert element.identity in "".join(before[old_start:old_end]), where
                assert f"{element.identity}Renamed" in "".join(after[new_start:new_end]), where
                assert outcome.text.count(oracle.PLANTED_KEY) == text.count(oracle.PLANTED_KEY), where

    def test_an_alias_among_the_undeclared_keys_ends_whole_and_never_moves(
        self, elements: list[oracle.Stored], tmp_path: Path
    ) -> None:
        ledger = oracle.run_alias_stability(tmp_path, elements, oracle.every_mutation)
        _held(ledger)
        assert ledger.fallbacks, "no edit met the pair"
        assert set(ledger.fallbacks) == {"the file writes a value through an alias, an anchor or a tag"}

    def test_every_kind_lands_as_lines_through_the_engine(self, tmp_path: Path) -> None:
        # The production route of `prior`: the baseline's dictionary, placement stripped, per kind,
        # and a `form` no value can stand in for, because a fallback would read back as well.
        project = tmp_path / "project"
        shutil.copytree(EXAMPLES_MODEL.parent, project, ignore=shutil.ignore_patterns("dist"))
        kinds = oracle.one_of_each_kind(oracle.stored_elements(project / "model"))
        for number, element in enumerate(kinds):
            path = project / "model" / element.target
            path.write_bytes(_restyled(element, number).encode("utf-8"))
            assert _first_commit(project, element) is not None, (element.layer, element.identity)
            assert _form_of(project, element) == "lines", (element.layer, element.identity)


class TestTheOraclesHaveTeeth:
    """Each oracle against an edit defective the way it is there to catch."""

    @pytest.fixture
    def swc(self) -> oracle.Stored:
        return next(element for element in oracle.stored_elements(EXAMPLES_MODEL) if element.layer == "swc")

    def test_oracle_1_sees_another_value(
        self, swc: oracle.Stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def wrong(model_root: str, target: str, element: Any, prior: Any) -> EditedText:
            return EditedText(Path(model_root, target).read_text(encoding="utf-8"), None)

        monkeypatch.setattr(oracle, "edited_text", wrong)
        ledger = oracle.run_parity_and_identity(EXAMPLES_MODEL, tmp_path, [swc], oracle.each_shape_once)
        assert any("oracle 1" in failure and "another value" in failure for failure in ledger.failures)

    def test_oracle_2_sees_other_bytes(
        self, swc: oracle.Stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def padded(model_root: str, target: str, element: Any, prior: Any) -> EditedText:
            return EditedText(edited_text(model_root, target, element, prior).text + "# a stray line\n", None)

        monkeypatch.setattr(oracle, "edited_text", padded)
        ledger = oracle.run_parity_and_identity(EXAMPLES_MODEL, tmp_path, [swc], oracle.each_shape_once)
        assert any("oracle 2" in failure and "differ" in failure for failure in ledger.failures)

    def test_oracle_3_sees_a_whole_write_lose_the_comments(
        self, swc: oracle.Stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def whole(model_root: str, target: str, element: Any, prior: Any) -> EditedText:
            write_element(model_root, target, element)
            return EditedText(Path(model_root, target).read_text(encoding="utf-8"), None)

        monkeypatch.setattr(oracle, "edited_text", whole)
        ledger = oracle.run_stability(tmp_path, [swc], oracle.each_shape_once, oracle.STYLES[:1])
        assert any("a line outside the change is gone" in failure for failure in ledger.failures)

    def test_oracle_3_sees_one_comment_lost_to_a_scalar_edit(self) -> None:
        text = "a: 1\n# note\nb: 2\n"
        changed = "a: 1\nb: 3\n"
        marks = oracle.changed_lines(text, {"a": 1, "b": 2}, {"a": 1, "b": 3})
        assert marks == {2}
        assert oracle.survives(text, changed, marks) == "# note"

    def test_oracle_3_sees_two_lines_swapped(self) -> None:
        text = "a: 1\nb: 2\nc: 3\n"
        marks = oracle.changed_lines(text, {"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 2, "c": 4})
        assert oracle.survives(text, "b: 2\na: 1\nc: 4\n", marks) == "b: 2"

    @pytest.mark.parametrize(
        ("changed", "added"),
        [
            ("a: 2\n# note\n# note\nb: 1\n", "# note"),
            ("a: 2\n# note\nb: 1\nzz_undeclared: kept\nzz_undeclared: kept\n", "zz_undeclared: kept"),
            ("a: 2\n# note\n\nb: 1\n", ""),
            ("a: 2  # a stranger\n# note\nb: 1\n", "a: 2  # a stranger"),
        ],
        ids=["comment-doubled", "planted-key-copied", "gap-put-back", "comment-invented"],
    )
    def test_oracle_3_sees_a_line_nothing_explains(self, changed: str, added: str) -> None:
        text = "a: 1\n# note\nb: 1\nzz_undeclared: kept\n"
        assert oracle.stray(text, changed, "a: 2\nb: 1\n") == added

    def test_oracle_3_lets_the_writers_lines_and_a_kept_comment_through(self) -> None:
        text = "a: 1  # why\n- b: 1\n"
        assert oracle.stray(text, "a: 2  # why\n-   b: 3\n", "a: 2\n- b: 3\n") is None

    def test_oracle_3_sees_another_value(
        self, swc: oracle.Stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def wrong(model_root: str, target: str, element: Any, prior: Any) -> EditedText:
            return EditedText(Path(model_root, target).read_bytes().decode("utf-8"), None)

        monkeypatch.setattr(oracle, "edited_text", wrong)
        ledger = oracle.run_stability(tmp_path, [swc], oracle.each_shape_once, oracle.STYLES[:1])
        assert any("another value" in failure for failure in ledger.failures)

    @pytest.mark.parametrize(
        "reason",
        [READ_BACK_MISMATCH, UNEXPECTED_EXCEPTION.format("KeyError"), "a sentence nobody listed"],
    )
    def test_a_fallback_off_the_closed_list_is_a_failure(self, swc: oracle.Stored, reason: str) -> None:
        mutation = next(oracle.mutations(swc.model))
        ledger = oracle.Ledger()
        old = oracle.validated_dict(type(swc.model), (EXAMPLES_MODEL / swc.target).read_text(encoding="utf-8"))
        assert ledger.judge(swc, mutation, EditedText(None, reason), "oracle 1", old) is None
        assert len(ledger.failures) == 1
        assert ledger.fallbacks == {reason: 1}


class _Nested(pydantic.BaseModel):
    """A class of the test's own: the writer's nested array and a long list, which the corpus lacks."""

    values: list[list[int]] = pydantic.Field(default_factory=list)
    items: list[str] = pydantic.Field(default_factory=list)


class TestTheClosedList:
    """The closed list's three entries, each on the case that may take it and refused on any other."""

    def _outcome(self, tmp_path: Path, text: str, old: _Nested, new: _Nested) -> EditedText:
        (tmp_path / "file.yaml").write_text(text, encoding="utf-8", newline="\n")
        return edited_text(str(tmp_path), "file.yaml", new, lambda: old)

    def _judged(self, old: _Nested, new: _Nested, outcome: EditedText) -> oracle.Ledger:
        ledger = oracle.Ledger()
        element = oracle.Stored("layer", "identity", "file.yaml", old)
        mutation = oracle.Mutation(oracle.SCALAR_ALTERED, ("values",), new)
        ledger.judge(element, mutation, outcome, "closed list", old.model_dump())
        return ledger

    def test_the_writers_nested_array_is_listed(self, tmp_path: Path) -> None:
        old, new = _Nested(values=[[1, 2], [3]]), _Nested(values=[[1, 2], [4]])
        outcome = self._outcome(tmp_path, "values:\n- - 1\n  - 2\n- - 3\n", old, new)
        assert outcome.reason == "a list item does not stand on a line of its own"
        assert not self._judged(old, new, outcome).failures

    def test_a_list_past_the_limit_is_listed(self, tmp_path: Path) -> None:
        items = [f"item{index}" for index in range(100)]
        old, new = _Nested(items=items), _Nested(items=[f"{item}x" for item in items])
        text = "items:\n" + "".join(f"- {item}\n" for item in items)
        outcome = self._outcome(tmp_path, text, old, new)
        assert outcome.reason == "the change touches too many places in the file"
        assert not self._judged(old, new, outcome).failures

    def test_a_list_past_the_match_budget_is_listed(self, tmp_path: Path) -> None:
        items = [f"item{index}" for index in range(4001)]
        old, new = _Nested(items=items), _Nested(items=items[::-1])
        text = "items:\n" + "".join(f"- {item}\n" for item in items)
        outcome = self._outcome(tmp_path, text, old, new)
        assert outcome.reason == "a list changed along too many items to match them"
        assert not self._judged(old, new, outcome).failures

    def test_a_file_past_the_recompose_budget_is_listed(self, tmp_path: Path) -> None:
        items = [f"{index:04}{'x' * 300}" for index in range(960)]
        changed = [f"{item}y" if index % 15 == 0 else item for index, item in enumerate(items)]
        old, new = _Nested(items=items), _Nested(items=changed)
        text = "items:\n" + "".join(f"- {item}\n" for item in items)
        outcome = self._outcome(tmp_path, text, old, new)
        assert outcome.reason == "the change touches too many places for a file of this size"
        assert not self._judged(old, new, outcome).failures

    def test_a_listed_reason_on_another_case_is_a_failure(self) -> None:
        old, new = _Nested(items=["a"]), _Nested(items=["b"])
        outcome = EditedText(None, "a list item does not stand on a line of its own")
        assert self._judged(old, new, outcome).failures
        outcome = EditedText(None, "the change touches too many places in the file")
        assert self._judged(old, new, outcome).failures
        outcome = EditedText(None, "a list changed along too many items to match them")
        assert self._judged(old, new, outcome).failures
        outcome = EditedText(None, "the change touches too many places for a file of this size")
        assert self._judged(old, new, outcome).failures


def _shard(elements: list[oracle.Stored], name: str, shard: int) -> list[oracle.Stored]:
    """Case *shard* of *name*'s walk: every ``SHARDS[name]``-th element of an order the read does not set.

    Sorted, because each case reads its own copy on its own worker: the cases agree on who walks
    what only if the order is the elements', never the reader's. Taken in steps, because the costly
    elements stand together: neighbours land in different cases.
    """
    ordered = sorted(elements, key=lambda element: (element.layer, element.identity))
    return ordered[shard :: SHARDS[name]]


class TestTheShards:
    """The cut neither drops nor repeats an element, which the walks' own cases cannot see."""

    @pytest.mark.parametrize("name", SHARDS)
    def test_the_shards_of_a_specimen_are_its_elements_once_each(self, name: str, tmp_path: Path) -> None:
        elements = oracle.stored_elements(copy_specimen(name, tmp_path / "specimen") / "model")
        shards = [_shard(elements, name, shard) for shard in range(SHARDS[name])]
        walked = [(element.layer, element.identity) for shard in shards for element in shard]
        assert sorted(walked) == sorted((element.layer, element.identity) for element in elements)
        assert len(set(walked)) == len(walked)
        assert all(shards)

    def test_the_order_is_not_the_readers(self, tmp_path: Path) -> None:
        elements = oracle.stored_elements(copy_specimen("map-bound", tmp_path / "specimen") / "model")
        assert _shard(elements[::-1], "map-bound", 0) == _shard(elements, "map-bound", 0)


@pytest.mark.parametrize(("name", "shard"), _CASES, ids=[f"{name}-{shard}" for name, shard in _CASES])
class TestTheSpecimens:
    """The four specimens: every element, a shard to a case."""

    def test_parity_and_writer_form_identity(self, name: str, shard: int, tmp_path: Path) -> None:
        model_root = copy_specimen(name, tmp_path / "specimen") / "model"
        elements = _shard(oracle.stored_elements(model_root), name, shard)
        ledger = oracle.run_parity_and_identity(model_root, tmp_path / "work", elements, oracle.every_mutation)
        _held(ledger)
        print(name, shard, len(elements), "elements", ledger.mutations, dict(ledger.fallbacks))

    def test_stability_in_every_layout(self, name: str, shard: int, tmp_path: Path) -> None:
        model_root = copy_specimen(name, tmp_path / "specimen") / "model"
        elements = _shard(oracle.stored_elements(model_root), name, shard)
        ledger = oracle.run_stability(tmp_path / "work", elements, oracle.each_shape_once)
        _held(ledger)
        print(name, shard, len(elements), "elements", ledger.mutations, dict(ledger.fallbacks))


@pytest.mark.slow
class TestTheHubOfTheLargeBench:
    """The largest element of ``large-bench``, one mutation of each shape."""

    def test_the_hub_in_each_oracle(self, tmp_path: Path) -> None:
        model_root = copy_specimen("large-bench", tmp_path / "specimen") / "model"
        hub_file = max(
            sorted(model_root.rglob("*.yaml"), key=lambda path: path.as_posix()), key=lambda p: p.stat().st_size
        )
        hub = next(
            element
            for element in oracle.stored_elements(model_root)
            if element.target == hub_file.relative_to(model_root).as_posix()
        )

        def sample(element: oracle.Stored) -> list[oracle.Mutation]:
            return [*oracle.each_shape_once(element), *oracle.longest_list_changed(element)]

        parity = oracle.run_parity_and_identity(model_root, tmp_path / "parity", [hub], sample)
        stability = oracle.run_stability(tmp_path / "stability", [hub], sample)
        _held(parity)
        _held(stability)
        assert parity.fallbacks == {"the change touches too many places in the file": 2}, "the limit is reached"
        print("hub", hub.identity, parity.mutations, stability.mutations, dict(parity.fallbacks + stability.fallbacks))


def _restyled(element: oracle.Stored, number: int) -> str:
    """*element*'s file in a foreign layout, a different one for each *number*, with comments and no undeclared key.

    These files go through the engine, whose pass D refuses every change set while any file holds an
    undeclared key. The key's survival is decided by ``edited_text``
    alone, where :class:`TestTheExamples` plants it with no pass D.
    """
    style = oracle.comments_only(oracle.STYLES[number % len(oracle.STYLES)])
    return oracle.restyled(oracle.writer_dict(element.model), style, type(element.model), oracle.Ledger())


def _form_of(project: Path, element: oracle.Stored) -> str:
    """The ``form`` of the last writer entry for *element*: what no read of the file can tell."""
    entries = AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
    written = [
        entry["args"]
        for entry in entries
        if entry["operation"] == "write_element" and entry["args"].get("identity") == element.identity
    ]
    return str(written[-1]["form"])


def _first_commit(project: Path, element: oracle.Stored) -> tuple[str, Any] | None:
    """The first of *element*'s top-level updates that commits and moves its file, or ``None``."""
    for field_name, value in _field_updates(element):
        stored = (project / "model" / element.target).read_bytes()
        response = _committed(project, element, field_name, value)
        if response.get("status") == "committed" and (project / "model" / element.target).read_bytes() != stored:
            return field_name, value
    return None


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _digest(project: Path, name: str) -> str:
    output = project / "dist" / name
    generate_arxml(str(project / "model"), str(output))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def _committed(project: Path, element: oracle.Stored, field_name: str, value: Any) -> dict[str, Any]:
    request = {
        "operation": "change_set",
        "baseline": baseline_token(project / "model"),
        "intent": "the digest oracle",
        "operations": [
            {
                "operation": "update",
                "address": {"layer": element.layer, "element": element.identity, "path": [{"field": field_name}]},
                "value": value,
            }
        ],
    }
    return handle_request(request, project=project, approved_scope=None, review=_approve)


def _field_updates(element: oracle.Stored) -> list[tuple[str, Any]]:
    """Each top-level field an update can state for *element*, numbers first, which reach the ARXML likeliest."""
    old = oracle.writer_dict(element.model)
    found = []
    for mutation in oracle.mutations(element.model):
        if mutation.shape in (oracle.SCALAR_ALTERED, oracle.KEY_ADDED) and len(mutation.path) <= 1:
            new = oracle.writer_dict(mutation.model)
            found.extend((name, new[name]) for name in new if name not in old or new[name] != old[name])
    return sorted(found, key=lambda pair: isinstance(pair[1], str))


@pytest.mark.slow
class TestTheDigests:
    """One change set per kind applied both ways, through the real engine, the ARXML bytes equal.

    Every file of the line-edit side is restyled first, so the two sides differ in their bytes and
    the audit's ``form`` says each was written the way it claims: equal digests over two trees of
    one layout would prove little. How much they prove is bounded by ``moved``: a change that does
    not reach the ARXML is equal both ways trivially.
    """

    def test_the_arxml_is_the_same_either_way(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lines = copy_specimen("benchmark-default", tmp_path / "lines")
        whole = copy_specimen("benchmark-default", tmp_path / "whole")
        base = _digest(lines, "base.arxml")
        assert base == _digest(whole, "base.arxml")
        kinds = oracle.one_of_each_kind(oracle.stored_elements(lines / "model"))
        committed = moved = 0
        for number, element in enumerate(kinds):
            (lines / "model" / element.target).write_bytes(_restyled(element, number).encode("utf-8"))
            update = _first_commit(lines, element)
            assert update is not None, (element.layer, element.identity)
            field_name, value = update
            with monkeypatch.context() as forced:
                forced.setattr(staging_mod, "edited_text", lambda *_args: EditedText(None, "forced whole"))
                response = _committed(whole, element, field_name, value)
            assert response["status"] == "committed", response
            where = (element.layer, element.identity, field_name)
            assert _form_of(lines, element) == "lines", where
            assert _form_of(whole, element) == "whole", where
            edited, written = (root / "model" / element.target for root in (lines, whole))
            assert edited.read_bytes() != written.read_bytes(), where
            digest = _digest(lines, f"{number}.arxml")
            assert digest == _digest(whole, f"{number}.arxml"), where
            committed += 1
            moved += digest != base
            base = digest
        print("digests", committed, "kinds of", len(kinds), "moved the ARXML", moved)
        assert committed == len(kinds)
        assert moved
