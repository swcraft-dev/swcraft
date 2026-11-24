"""The line-preserving edit of a YAML file's text: what a position means, what is refused, and the edit itself."""

from __future__ import annotations

import ast
import difflib
from itertools import chain
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.yaml_text_edit as leaf
import yaml
from conftest import at_most_steps
from swcraft_tools.yaml_load import compose_yaml_document, load_yaml_document
from swcraft_tools.yaml_text_edit import (
    FormRefused,
    TextEdit,
    composed_root,
    edit_element_text,
    find_key,
    is_indirect,
    last_end,
    line_end,
    line_start,
    same_value,
)


class _Counted(int):
    """A scalar counting what a dictionary keyed on it would do to it: the cost a clock would only suggest."""

    hashes = 0
    comparisons = 0

    def __hash__(self) -> int:
        _Counted.hashes += 1
        return int.__hash__(self)

    def __eq__(self, other: object) -> bool:
        _Counted.comparisons += 1
        return int.__eq__(self, other) is True


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every :class:`_Counted` the walk reads: what re-reading a value costs, which a count of calls cannot see."""
    seen: list[int] = []
    key = leaf._scalar_key

    def counted(value: Any) -> tuple[str, str]:
        if isinstance(value, _Counted):
            seen.append(value)
        return key(value)

    monkeypatch.setattr(leaf, "_scalar_key", counted)
    return seen


def _value(text: str, name: str) -> yaml.Node:
    root = compose_yaml_document(text)
    assert isinstance(root, yaml.MappingNode)
    found = find_key(root, name)
    assert found is not None
    return found[1]


class TestTheLeaf:
    def test_it_imports_only_the_yaml_reader_from_the_package(self) -> None:
        tree = ast.parse(Path(leaf.__file__).read_text(encoding="utf-8"))
        names = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert {name for name in names if name.startswith("swcraft_tools")} == {"swcraft_tools.yaml_load"}

    def test_form_refused_is_the_one_apply_reports(self) -> None:
        from swcraft_tools import trace_satisfies_text

        assert trace_satisfies_text.FormRefused is FormRefused


class TestLineHelpers:
    def test_line_start_of_the_first_line_and_of_a_later_one(self) -> None:
        assert line_start("ab\ncd\n", 1) == 0
        assert line_start("ab\ncd\n", 4) == 3

    def test_line_end_is_past_the_break(self) -> None:
        assert line_end("ab\ncd\n", 0) == 3

    def test_line_end_of_a_last_line_without_a_break_is_the_length(self) -> None:
        assert line_end("ab\ncd", 3) == 5

    def test_a_text_edit_holds_a_span_and_its_replacement(self) -> None:
        assert TextEdit(1, 2, "x") == TextEdit(start=1, end=2, text="x")


class TestFindKey:
    def test_it_returns_the_key_and_the_value_nodes(self) -> None:
        root = compose_yaml_document("a: 1\nb: 2\n")
        assert isinstance(root, yaml.MappingNode)
        found = find_key(root, "b")
        assert found is not None
        assert (found[0].value, found[1].value) == ("b", "2")

    def test_a_name_the_mapping_does_not_hold_is_none(self) -> None:
        root = compose_yaml_document("a: 1\n")
        assert isinstance(root, yaml.MappingNode)
        assert find_key(root, "b") is None

    def test_a_key_that_is_no_scalar_is_passed_over(self) -> None:
        root = compose_yaml_document("? [a]\n: 1\nb: 2\n")
        assert isinstance(root, yaml.MappingNode)
        found = find_key(root, "b")
        assert found is not None
        assert found[1].value == "2"


class TestLastEnd:
    """A block collection's ``end_mark`` lies at the next token; the end of its value is its last descendant's."""

    def test_a_comment_after_a_block_list_is_outside_it(self) -> None:
        text = "a:\n  - x\n  - y\n# kept\n\nb: 1\n"
        value = _value(text, "a")
        assert value.end_mark.index > text.index("# kept")  # the mark this function exists to correct
        assert text[: last_end(value)] == "a:\n  - x\n  - y"

    def test_a_comment_after_an_indentless_list_is_outside_it(self) -> None:
        text = "a:\n- x\n# kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n- x"

    def test_a_comment_after_a_block_mapping_is_outside_it(self) -> None:
        text = "a:\n  k: v\n# kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n  k: v"

    def test_it_descends_through_a_list_of_mappings_of_lists(self) -> None:
        text = "a:\n- n: 1\n  m:\n  - x\n  - y\n# kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n- n: 1\n  m:\n  - x\n  - y"

    def test_a_block_scalar_among_the_descendants_keeps_its_trailing_breaks(self) -> None:
        text = "a:\n- n: |\n    line\n\n# kept\nb: 1\n"
        end = last_end(_value(text, "a"))
        assert text[:end] == "a:\n- n: |\n    line\n\n"
        assert text[end:].startswith("# kept")  # already a line start: line_end here would take the comment

    def test_a_last_block_scalar_without_a_break_ends_at_the_text_end(self) -> None:
        text = "a: |\n  x"
        assert last_end(_value(text, "a")) == len(text)

    def test_a_flow_collection_ends_after_its_bracket(self) -> None:
        text = "a: [1,\n  2] # kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a: [1,\n  2]"

    def test_a_flow_collection_last_in_a_block_list_ends_after_its_bracket(self) -> None:
        text = "a:\n- [1, 2]  # kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n- [1, 2]"

    def test_an_empty_last_value_ends_after_its_colon_not_at_the_next_token(self) -> None:
        text = "a:\n  k:\n# kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n  k:"

    def test_an_empty_last_item_ends_after_its_dash(self) -> None:
        text = "a:\n- x\n-\n# kept\nb: 1\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n- x\n-"

    def test_a_scalar_ends_where_it_ends(self) -> None:
        text = "a: 'x'  # kept\n"
        assert text[: last_end(_value(text, "a"))] == "a: 'x'"

    @pytest.mark.parametrize(
        "text",
        [
            "a:\n  k: &x kept\n  l: *x\n",
            "a:\n- &x kept\n- *x\n",
            "z: &x kept\na:\n- *x\n",
            "a:\n- &p 1\n- &q 2\n- *p\n- *q\n",
        ],
        ids=["before-its-key", "before-the-sibling", "before-the-parent", "in-the-anchors-order"],
    )
    def test_an_alias_has_no_end_in_the_text(self, text: str) -> None:
        with pytest.raises(FormRefused) as refused:
            last_end(_value(text, "a"))
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_an_anchor_is_an_end_like_any_other(self) -> None:
        text = "a:\n  k: &x kept\nb: *x\n"
        assert text[: last_end(_value(text, "a"))] == "a:\n  k: &x kept"

    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("a:\n  k: 'x'\n  # comment\nb: 1\n", "a:\n  k: 'x'"),
            (
                "a:\n- k: |\n    x\n\n# comment\n- [1,\n  2]\n- l:\n  - x\n# comment\n- m:\n    n:\n- o\n",
                "a:\n- k: |\n    x\n\n# comment\n- [1,\n  2]\n- l:\n  - x\n# comment\n- m:\n    n:\n- o",
            ),
            ("a: &x\n  k: 1\n  l: !!str x\n", "a: &x\n  k: 1\n  l: !!str x"),
        ],
        ids=["comment-after", "every-form-in-a-row", "anchored-and-tagged"],
    )
    def test_a_collection_with_no_alias_is_read_to_its_end(self, text: str, kept: str) -> None:
        assert text[: last_end(_value(text, "a"))] == kept

    @pytest.mark.parametrize(
        "text",
        ["a: &j\n- *j\n", "a: &j\n  k: *j\n", "a: &j\n- x\n- k:\n  - *j\n"],
        ids=["list", "mapping", "deep"],
    )
    def test_a_collection_ending_in_an_alias_of_itself_is_refused_and_never_walked_for_ever(self, text: str) -> None:
        with at_most_steps(), pytest.raises(FormRefused) as refused:
            last_end(_value(text, "a"))
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"


class TestComposedRoot:
    def test_it_returns_the_composed_root(self) -> None:
        root = composed_root("a: 1\n")
        assert isinstance(root, yaml.MappingNode)

    def test_a_leading_document_marker_is_not_a_second_document(self) -> None:
        assert isinstance(composed_root("---\na: 1\n"), yaml.MappingNode)

    @pytest.mark.parametrize(
        ("text", "sentence"),
        [
            ("a: 1\r\n", "the file has carriage-return line ends"),
            ("\ufeffa: 1\n", "the file starts with a byte-order mark"),
            ("a: 1\n---\nb: 2\n", "the file holds a document marker"),
            ("a: 1\n...\n", "the file holds a document marker"),
            ("a: [\n", "the file cannot be read as YAML"),
            ("", "the file is empty"),
            ("# nothing\n", "the file is empty"),
        ],
        ids=["carriage-return", "byte-order-mark", "second-document", "document-end", "not-yaml", "empty", "comment"],
    )
    def test_each_refusal_says_which(self, text: str, sentence: str) -> None:
        with pytest.raises(FormRefused) as refused:
            composed_root(text)
        assert str(refused.value) == sentence


def _scalars(node: yaml.Node) -> list[yaml.ScalarNode]:
    """Every scalar under *node*, keys included: a collection's ``value`` is nodes, so it has no slice to compare."""
    if isinstance(node, yaml.ScalarNode):
        return [node]
    children = chain.from_iterable(node.value) if isinstance(node, yaml.MappingNode) else node.value
    return [scalar for child in children for scalar in _scalars(child)]


class TestAByteOrderMarksPosition:
    """A byte-order mark shifts the indexes at the very start of a text and nowhere else."""

    @pytest.mark.parametrize(
        "text",
        [
            "\n\ufeffsatisfies:\n  - A\n  - B\n",
            "\n\n\ufeffsatisfies:\n  - A\n  - B\n",
            "# c\n\ufeffsatisfies:\n  - A\n  - B\n",
            "# \U0001f600\n\ufeffsatisfies:\n  - A\n  - B\n",
            "\n\ufeff\n\ufeffsatisfies:\n  - A\n  - B\n",
            "id: X\nports:\n\ufeff - name: p\n  - name: q\n",
            "a: 1\n\ufeff# c\nb: 2\n",
            "%YAML 1.1\n---\n\ufeffa: bc\n",
            "\u0085\ufeffa: bc\n",
        ],
        ids=[
            "line-break",
            "blank-line",
            "comment",
            "comment-outside-the-bmp",
            "two-marks",
            "indent-space",
            "on-a-comment",
            "behind-a-directive-and-a-document-marker",
            "behind-a-next-line",
        ],
    )
    def test_after_a_line_break_every_slice_is_its_value(self, text: str) -> None:
        root = compose_yaml_document(text)
        assert root is not None
        scalars = _scalars(root)
        assert len(scalars) >= 2
        assert [text[node.start_mark.index : node.end_mark.index] for node in scalars] == [
            node.value for node in scalars
        ]

    def test_it_is_one_column_of_its_line(self) -> None:
        assert _value("\n\ufeffa: 1\n", "a").start_mark.column == 4

    @pytest.mark.parametrize("text", ["\ufeffa: bc\n", "\ufeff\n{id: X}\n"], ids=["block", "flow"])
    def test_at_the_very_start_every_index_is_one_short(self, text: str) -> None:
        root = compose_yaml_document(text)
        assert root is not None
        for node in _scalars(root):
            assert text[node.start_mark.index + 1 : node.end_mark.index + 1] == node.value
            assert text[node.start_mark.index : node.end_mark.index] != node.value


class TestIsIndirect:
    @staticmethod
    def _verdict(text: str, name: str) -> bool:
        root = compose_yaml_document(text)
        assert isinstance(root, yaml.MappingNode)
        found = find_key(root, name)
        assert found is not None
        key, value = found
        return is_indirect(text, key.end_mark.index, value)

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("a: &x 1\n", "a"),
            ("a: &x\n- 1\n", "a"),
            ("a: &x [1]\n", "a"),
            ("a: !!str 1\n", "a"),
            ("a: !!seq\n- 1\n", "a"),
            ("a: &x [1]\nb: *x\n", "b"),
            ("a: # note\n  &x\n  k: 1\n", "a"),
        ],
        ids=["anchored", "anchored-block", "anchored-flow", "tagged", "tagged-block", "alias", "after-a-comment"],
    )
    def test_an_anchor_a_tag_or_an_alias_is_indirect(self, text: str, name: str) -> None:
        assert self._verdict(text, name) is True

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("a: 1\n", "a"),
            ("a: 'x&y'\n", "a"),
            ("a: # see & and * and !\n  k: 1\n", "a"),
            ("a: [1]\n", "a"),
            ("a:\n- 1\n", "a"),
            ("a:\n", "a"),
        ],
        ids=["plain", "marks-inside-a-string", "marks-in-a-comment", "flow", "block", "implicit-null"],
    )
    def test_a_value_written_where_it_stands_is_not(self, text: str, name: str) -> None:
        assert self._verdict(text, name) is False


def _edited(text: str, old: dict[str, Any], new: dict[str, Any]) -> str:
    """*text* edited from *old* to *new*, with ``written`` the writer's own dump of *new*."""
    return edit_element_text(text, old, new, yaml.safe_dump(new, default_flow_style=False, sort_keys=False))


def _dump(data: dict[str, Any]) -> str:
    """*data* in the writer's block form."""
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


class TestTheWalk:
    def test_a_value_that_did_not_change_leaves_its_foreign_form_alone(self) -> None:
        text = "b: 'x'   # kept\na: 1\n"
        assert _edited(text, {"a": 1, "b": "x"}, {"a": 2, "b": "x"}) == "b: 'x'   # kept\na: 2\n"

    @pytest.mark.parametrize(
        ("before", "after", "line"),
        [(1, 1.0, "a: 1.0\n"), (1, True, "a: true\n"), (0, False, "a: false\n"), ("1", 1, "a: 1\n")],
        ids=["int-to-float", "int-to-bool", "zero-to-false", "str-to-int"],
    )
    def test_equality_is_strict_about_the_type(self, before: Any, after: Any, line: str) -> None:
        text = f"a: {before!r}\n".replace("'", '"')
        assert _edited(text, {"a": before}, {"a": after}) == line

    def test_a_nested_mapping_is_walked_so_only_its_leaf_moves(self) -> None:
        text = "top:\n    keep: 'x'  # one\n    leaf: 1  # two\n"
        old = {"top": {"keep": "x", "leaf": 1}}
        new = {"top": {"keep": "x", "leaf": 2}}
        assert _edited(text, old, new) == "top:\n    keep: 'x'  # one\n    leaf: 2  # two\n"

    def test_several_changes_all_land(self) -> None:
        text = "a: 1\nb: 2\nc: 3\n"
        new = {"a": 10, "c": 30, "d": 40}
        assert _edited(text, {"a": 1, "b": 2, "c": 3}, new) == "a: 10\nc: 30\nd: 40\n"

    def test_a_mapping_can_exchange_its_only_key(self) -> None:
        assert _edited("a: 1\n", {"a": 1}, {"b": 2}) == "b: 2\n"

    def test_a_key_after_the_one_replaced_lands_before_the_removal(self) -> None:
        assert _edited("a: 1\nz: 9\n", {"a": 1, "z": 9}, {"b": 2, "z": 9}) == "b: 2\nz: 9\n"

    def test_exactly_the_limit_is_edited(self) -> None:
        old = {f"k{index}": 0 for index in range(leaf._EDIT_LIMIT)}
        new = {name: 1 for name in old}
        text = "".join(f"{name}: 0\n" for name in old)
        assert _edited(text, old, new) == "".join(f"{name}: 1\n" for name in old)

    def test_one_past_the_limit_is_refused(self) -> None:
        old = {f"k{index}": 0 for index in range(leaf._EDIT_LIMIT + 1)}
        text = "".join(f"{name}: 0\n" for name in old)
        with pytest.raises(FormRefused) as refused:
            _edited(text, old, {name: 1 for name in old})
        assert str(refused.value) == "the change touches too many places in the file"

    def test_additions_past_the_limit_are_refused_during_the_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Each addition copies the names before it, so the walk must stop at the limit, not after.
        new = {"a": 1, **{f"k{index}": 0 for index in range(1000)}}
        walked: list[int] = []
        original = leaf._Change

        def counted(*args: Any, **kwargs: Any) -> Any:
            walked.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(leaf, "_Change", counted)
        with pytest.raises(FormRefused) as refused:
            _edited("a: 1\n", {"a": 1}, new)
        assert str(refused.value) == "the change touches too many places in the file"
        assert len(walked) == leaf._EDIT_LIMIT

    def test_the_mapping_walk_reads_each_mapping_a_fixed_number_of_times(self) -> None:
        class CountedPairs(list[Any]):
            reads = 0

            def __iter__(self) -> Any:
                type(self).reads += 1
                return super().__iter__()

        old = {f"k{index}": index for index in range(2000)}
        new = {**old, "k1999": -1}
        root = composed_root(_dump(old))
        root.value = CountedPairs(root.value)

        changes = leaf._changes(_dump(old), root, old, new, (), (), leaf._Walk())

        assert len(changes) == 1
        assert CountedPairs.reads == 2, "the refusal's pass and the index's, whatever the number of keys"

    def test_a_change_at_the_recompose_budget_is_edited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        text = "a: 0\nb: 0\n"
        monkeypatch.setattr(leaf, "_RECOMPOSE_BUDGET", 2 * len(text))
        assert _edited(text, {"a": 0, "b": 0}, {"a": 1, "b": 1}) == "a: 1\nb: 1\n"

    def test_past_the_recompose_budget_it_refuses_before_the_first_recomposition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = "a: 0\nb: 0\n"
        monkeypatch.setattr(leaf, "_RECOMPOSE_BUDGET", 2 * len(text) - 1)
        composed: list[str] = []
        original = leaf.composed_root

        def counted(source: str) -> yaml.Node:
            composed.append(source)
            return original(source)

        monkeypatch.setattr(leaf, "composed_root", counted)
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"a": 0, "b": 0}, {"a": 1, "b": 1})
        assert str(refused.value) == "the change touches too many places for a file of this size"
        assert composed == [text], "the walk's one composing and nothing after it"

    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            ("a: 0\nb: 0\n", {"a": 0, "b": 0}, {"a": 1, "b": 1, "c": "x" * 100}),
            (f"a: 0\nb: 0\nc: {'x' * 100}\n", {"a": 0, "b": 0, "c": "x" * 100}, {"a": 1, "b": 1}),
        ],
        ids=["written-the-larger", "text-the-larger"],
    )
    def test_the_recompose_budget_reads_the_larger_side(
        self, text: str, old: dict[str, Any], new: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cost = 3 * max(len(text), len(_dump(new)))
        monkeypatch.setattr(leaf, "_RECOMPOSE_BUDGET", cost)
        assert yaml.safe_load(_edited(text, old, new)) == new
        monkeypatch.setattr(leaf, "_RECOMPOSE_BUDGET", cost - 1)
        with pytest.raises(FormRefused) as refused:
            _edited(text, old, new)
        assert str(refused.value) == "the change touches too many places for a file of this size"

    def test_a_hubs_worst_stays_under_the_recompose_budget(self) -> None:
        assert leaf._EDIT_LIMIT * 80_000 < leaf._RECOMPOSE_BUDGET

    def test_a_list_at_the_match_budget_is_matched(self) -> None:
        old = list(range(4000))
        assert 4000 * (4000 + 4000) == leaf._MATCH_BUDGET
        text = "x:\n" + "".join(f"- {item}\n" for item in old)
        new = old[::-1]
        assert load_yaml_document(_edited(text, {"x": old}, {"x": new})) == {"x": new}

    def test_a_list_past_the_match_budget_is_refused_before_the_matcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_matcher(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the refusal comes before the matcher's cost")

        monkeypatch.setattr(leaf, "SequenceMatcher", no_matcher)
        old = list(range(4001))
        text = "x:\n" + "".join(f"- {item}\n" for item in old)
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"x": old}, {"x": old[::-1]})
        assert str(refused.value) == "a list changed along too many items to match them"

    def test_equal_values_changed_at_both_ends_refuse_before_the_matcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_matcher(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the refusal comes before the matcher's cost")

        monkeypatch.setattr(leaf, "SequenceMatcher", no_matcher)
        old = {"init": [1, *[0] * 20_000, 2]}
        new = {"init": [3, *[0] * 20_000, 4]}
        text = _dump(old)
        with pytest.raises(FormRefused) as refused:
            _edited(text, old, new)
        assert str(refused.value) == "a list changed along too many items to match them"

    @pytest.mark.parametrize(
        ("old", "new", "matches"),
        [
            pytest.param(
                {f"k{at}": list(range(632)) for at in range(41)},
                {f"k{at}": list(range(631, -1, -1)) for at in range(41)},
                40,
                id="side-by-side",
            ),
            pytest.param(
                {"x": [{"v": list(range(632))} for _ in range(41)]},
                {"x": [{"v": [at, *range(631, -1, -1)]} for at in range(41)]},
                41,
                id="under-one-list",
            ),
        ],
    )
    def test_lists_under_the_budget_alone_refuse_together(
        self, monkeypatch: pytest.MonkeyPatch, old: dict[str, Any], new: dict[str, Any], matches: int
    ) -> None:
        # Each costs about 800 000; the budget stops the walk at the list that would pass it.
        made = []

        def counted(*args: Any, **kwargs: Any) -> difflib.SequenceMatcher[Any]:
            made.append(args)
            return difflib.SequenceMatcher(*args, **kwargs)

        monkeypatch.setattr(leaf, "SequenceMatcher", counted)
        with pytest.raises(FormRefused) as refused:
            _edited(_dump(old), old, new)
        assert str(refused.value) == "a list changed along too many items to match them"
        assert len(made) == matches

    def test_the_match_bound_reads_the_lengths_past_the_common_ends(self) -> None:
        old = {"init": [0] * 500}
        new = {"init": [*[0] * 250, 1, *[0] * 249]}
        assert _edited(_dump(old), old, new) == _dump(new)

    @pytest.mark.parametrize(
        ("old", "new", "same"),
        [
            (1, 1.0, False),
            (1, True, False),
            (1, "1", False),
            ([], {}, False),
            ([1, 2], [2, 1], False),
            ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
            ({"a": [1]}, {"a": [1.0]}, False),
            ([[True]], [[1]], False),
            ([{"a": [1, {"b": None}]}], [{"a": [1, {"b": None}]}], True),
            ([{"a": 1}], [{"a": 2}], False),
            (1.0, True, False),
            (0, False, False),
            (0.0, -0.0, False),
            (-0.0, -0.0, True),
            (float("nan"), float("nan"), True),
            (float("inf"), float("-inf"), False),
            (1, "0x1", False),
            (1.0, "1.0", False),
            (-0.0, "-0.0", False),
            ("True", True, False),
            ("None", None, False),
            (0.1 + 0.2, 0.3, False),
            ({1: "a"}, {1.0: "a"}, False),
            ({1: "a"}, {True: "a"}, False),
            ({1: "a"}, {"1": "a"}, False),
            ([1, 23], [12, 3], False),
            ([[1], [2]], [[1, 2]], False),
            ([{"a": 1, "b": 2}], [{"a": 2, "b": 1}], False),
            ([1, 2], {1: 2}, False),
        ],
    )
    def test_equality_is_type_strict_at_every_depth_and_blind_to_key_order(
        self, old: Any, new: Any, same: bool
    ) -> None:
        assert same_value(old, new) is same

    def test_a_zero_changed_to_a_negative_zero_moves_its_line(self) -> None:
        assert _edited("a: 0.0\n", {"a": 0.0}, {"a": -0.0}) == "a: -0.0\n"

    @pytest.mark.parametrize(
        "changed",
        [
            pytest.param(lambda floods: {"a": 1, "init": floods}, id="beside-the-change"),
            pytest.param(lambda floods: {"a": 0, "init": [*floods, 0]}, id="in-the-list"),
        ],
    )
    def test_colliding_integers_cost_no_comparison(self, monkeypatch: pytest.MonkeyPatch, changed: Any) -> None:
        # Past the walk, 300 items would cost 44 850 comparisons.
        monkeypatch.setattr(_Counted, "hashes", 0)
        monkeypatch.setattr(_Counted, "comparisons", 0)
        plain = [at * (2**61 - 1) for at in range(1, 301)]
        assert len({hash(value) for value in plain}) == 1
        old = {"a": 0, "init": [_Counted(value) for value in plain]}
        new = changed([_Counted(value) for value in plain])
        written = _dump(changed(plain))

        edited = edit_element_text(_dump({"a": 0, "init": plain}), old, new, written)

        assert edited == written
        assert same_value(old, new) is False
        assert (_Counted.hashes, _Counted.comparisons) == (0, 0)

    def test_every_key_of_a_walk_is_two_strings_whatever_it_read(self) -> None:
        walk = leaf._Walk()
        walk.form({"a": [1, 1.0, True, None, "1", {"b": [[2.5]]}], "c": {}})
        assert walk._numbers
        assert all(type(part) is str for key in walk._numbers for part in key)
        assert all(type(key) is tuple and len(key) == 2 for key in walk._numbers)

    def test_an_integer_past_the_limit_on_decimal_digits_has_a_key(self) -> None:
        # `0x` and 5 000 digits is a valid YAML integer, and `repr` of it raises.
        huge = 1 << 20_000
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(huge)
        assert same_value(huge, 1 << 20_000) is True
        assert same_value(huge, huge + 1) is False
        assert same_value(huge, -huge) is False

    def test_the_matcher_steps_over_numbers_so_an_item_is_read_once(self, reads: list[int]) -> None:
        def items(scalar: type[int]) -> list[list[int]]:
            return [[scalar(at), *map(scalar, range(30))] for at in range(40)]

        plain = items(int)
        old, new = items(_Counted), items(_Counted)[1::2]
        text = "init:\n" + "".join(f"- {item}\n" for item in plain)
        kept = plain[1::2]

        edited = edit_element_text(text, {"init": old}, {"init": new}, _dump({"init": kept}))

        assert load_yaml_document(edited) == {"init": kept}
        assert len(reads) == sum(map(len, old)) + sum(map(len, new))

    @pytest.mark.parametrize(
        "nest",
        [lambda value: {"a": value}, lambda value: {"a": [value]}],
        ids=["mappings", "mappings-in-lists"],
    )
    def test_a_deep_walk_reads_a_value_beside_the_change_once(self, reads: list[int], nest: Any) -> None:
        def deep(key: int, scalar: type[int]) -> dict[str, Any]:
            value: dict[str, Any] = {"k": key, "z": [scalar(at) for at in range(300)]}
            for _ in range(30):
                value = nest(value)
            return value

        written = _dump(deep(1, int))

        edited = edit_element_text(_dump(deep(0, int)), deep(0, _Counted), deep(1, _Counted), written)

        assert edited == written
        assert len(reads) == 2 * 300

    def test_a_change_with_nothing_to_edit_is_refused_so_the_caller_writes_whole(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("a: 1\n", {"a": 1}, {"a": 1})
        assert str(refused.value) == "the line edit found no line to change"


class TestAScalarReplaced:
    @pytest.mark.parametrize(
        ("text", "result"),
        [
            ("a: old\nb: 1\n", "a: new\nb: 1\n"),
            ("a: 'old'\nb: 1\n", "a: new\nb: 1\n"),
            ('a: "old"\nb: 1\n', "a: new\nb: 1\n"),
            ("a: old  # kept\nb: 1\n", "a: new  # kept\nb: 1\n"),
            ("a: 'old'  # kept\nb: 1\n", "a: new  # kept\nb: 1\n"),
            ("a: first\n  second\nb: 1\n", "a: new\nb: 1\n"),
            ("a: 'first\n  second'  # kept\nb: 1\n", "a: new  # kept\nb: 1\n"),
            ("a:\n  old\nb: 1\n", "a:\n  new\nb: 1\n"),
        ],
        ids=[
            "plain",
            "single",
            "double",
            "plain-comment",
            "quoted-comment",
            "multi-plain",
            "multi-quoted",
            "next-line",
        ],
    )
    def test_the_span_goes_and_the_rest_of_the_line_stays(self, text: str, result: str) -> None:
        assert _edited(text, {"a": "old", "b": 1}, {"a": "new", "b": 1}) == result

    @pytest.mark.parametrize(
        ("text", "result"),
        [
            ("a: |\n  one\n  two\nb: 1\n", "a: new\nb: 1\n"),
            ("a: >-\n  one\n  two\nb: 1\n", "a: new\nb: 1\n"),
            ("a: |\n  one\n\n# kept\nb: 1\n", "a: new\n\n# kept\nb: 1\n"),
            ("b: 1\na: |\n  one\n", "b: 1\na: new\n"),
            ("b: 1\na: |\n  one", "b: 1\na: new"),
        ],
        ids=["literal", "folded", "blank-and-comment-after", "last", "last-without-a-break"],
    )
    def test_a_block_scalars_end_never_takes_the_next_line(self, text: str, result: str) -> None:
        assert _edited(text, {"a": "x", "b": 1}, {"a": "new", "b": 1}) == result

    @pytest.mark.parametrize(
        ("text", "result"),
        [
            ("a: |-\n  one\n  \n# kept\nb: 1\n", "a: new\n  \n# kept\nb: 1\n"),
            ("a: |-\n  one\n  \n\n \n# kept\nb: 1\n", "a: new\n  \n\n \n# kept\nb: 1\n"),
            ("a: >-\n  one\n  \n# kept\nb: 1\n", "a: new\n  \n# kept\nb: 1\n"),
            ("a: |\n  one  \n  \nb: 1\n", "a: new\n  \nb: 1\n"),
            ("a: |+\n  one\n  \n# kept\nb: 1\n", "a: new\n  \n# kept\nb: 1\n"),
            ("a: |-\n\n  \n# kept\nb: 1\n", "a: new\n\n  \n# kept\nb: 1\n"),
        ],
        ids=["one", "a-run-of-them", "folded", "after-a-line-with-trailing-spaces", "keep-chomped", "no-content"],
    )
    def test_a_line_of_spaces_after_a_block_scalar_stays(self, text: str, result: str) -> None:
        assert _edited(text, {"a": "x", "b": 1}, {"a": "new", "b": 1}) == result

    @pytest.mark.parametrize(
        ("text", "result"),
        [
            ("a: |-\n  one\n     \n# kept\nb: 1\n", "a: new\n# kept\nb: 1\n"),
            ("a: >-\n  one\n     \n# kept\nb: 1\n", "a: new\n# kept\nb: 1\n"),
            ("a: |2-\n    one\n     \n# kept\nb: 1\n", "a: new\n# kept\nb: 1\n"),
        ],
        ids=["literal", "folded", "stated-indentation"],
    )
    def test_a_line_of_spaces_that_is_the_value_goes_with_it(self, text: str, result: str) -> None:
        assert yaml.safe_load(text)["a"].endswith("   ")
        assert _edited(text, {"a": "x", "b": 1}, {"a": "new", "b": 1}) == result

    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("a: |+\n  one\n  \nb: 1\n", "one\n\n", "new\n\n", "a: |+\n  new\n  \nb: 1\n"),
            ("a: |+\n  one\n\nb: 1\n", "one\n\n", "new\n\n", "a: |+\n  new\n\nb: 1\n"),
        ],
        ids=["a-line-of-spaces", "an-empty-line"],
    )
    def test_a_keep_chomped_scalar_keeps_its_value(self, text: str, old: str, new: str, result: str) -> None:
        edited = edit_element_text(text, {"a": old, "b": 1}, {"a": new, "b": 1}, "a: |+\n  new\n\nb: 1\n")
        assert edited == result
        assert yaml.safe_load(edited)["a"] == new

    def test_a_keep_chomped_scalar_turned_into_a_quoted_one_keeps_the_value(self) -> None:
        edited = _edited("a: |+\n  one\n  \nb: 1\n", {"a": "one\n\n", "b": 1}, {"a": "new\n\n", "b": 1})
        assert yaml.safe_load(edited)["a"] == "new\n\n"

    def test_a_block_scalar_closing_a_value_keeps_the_line_of_spaces_too(self) -> None:
        text = "a:\n  b: |-\n    one\n    \n# kept\nc: 1\n"
        edited = _edited(text, {"a": {"b": "x"}, "c": 1}, {"a": {"b": "new"}, "c": 1})
        assert edited == "a:\n  b: new\n    \n# kept\nc: 1\n"

    def test_a_removed_key_leaves_the_line_of_spaces(self) -> None:
        text = "a: 1\nb: |-\n  one\n  \n# kept\nc: 1\n"
        assert _edited(text, {"a": 1, "b": "x", "c": 1}, {"a": 1, "c": 1}) == "a: 1\n  \n# kept\nc: 1\n"

    def test_a_removed_list_item_leaves_the_line_of_spaces(self) -> None:
        text = "x:\n- |-\n  one\n  \n# kept\n- b\n"
        assert _edited(text, {"x": ["one", "b"]}, {"x": ["b"]}) == "x:\n  \n# kept\n- b\n"

    @pytest.mark.parametrize(
        ("text", "result"),
        [
            ("a:\nb: 1\n", "a: new\nb: 1\n"),
            ("a:   # kept\nb: 1\n", "a: new   # kept\nb: 1\n"),
            ("b: 1\na:", "b: 1\na: new"),
        ],
        ids=["bare", "comment", "last-without-a-break"],
    )
    def test_an_implicit_null_takes_a_leading_space(self, text: str, result: str) -> None:
        assert _edited(text, {"a": None, "b": 1}, {"a": "new", "b": 1}) == result

    def test_a_long_value_comes_in_wrapped_as_the_writer_wraps_it(self) -> None:
        long = " ".join(["word"] * 40)
        new = {"a": long, "b": 1}
        written = yaml.safe_dump(new, default_flow_style=False, sort_keys=False)
        assert edit_element_text("a: old\nb: 1\n", {"a": "old", "b": 1}, new, written) == written

    def test_a_wrapped_value_shifts_by_the_column_of_a_file_indented_otherwise(self) -> None:
        long = " ".join(["word"] * 40)
        new = {"top": {"a": long}}
        result = _edited("top:\n      a: old\n", {"top": {"a": "old"}}, new)
        written_lines = yaml.safe_dump(new, default_flow_style=False, sort_keys=False).splitlines()
        assert result.splitlines()[1:] == ["    " + line for line in written_lines[1:]]
        assert load_yaml_document(result) == new

    def test_a_shift_the_leading_spaces_cannot_pay_for_is_refused(self) -> None:
        new = {"top": {"a": "x\ny"}}
        written = 'top:\n  a: "x\n\ny"\n'  # a continuation at column 0, which no shift left can pay for
        with pytest.raises(FormRefused) as refused:
            edit_element_text("top:\n a: old\n", {"top": {"a": "old"}}, new, written)
        assert str(refused.value) == "the file's indentation cannot hold the new text"


class TestAKeyAdded:
    def test_it_lands_after_the_nearest_key_before_it_in_the_writers_order(self) -> None:
        text = "a: 1  # one\n# two\nc: 3\n"
        new = {"a": 1, "b": 2, "c": 3}
        assert _edited(text, {"a": 1, "c": 3}, new) == "a: 1  # one\nb: 2\n# two\nc: 3\n"

    def test_it_lands_before_the_first_key_when_none_is_before_it(self) -> None:
        text = "# head\nb: 1\n"
        assert _edited(text, {"b": 1}, {"a": 0, "b": 1}) == "# head\na: 0\nb: 1\n"

    def test_two_added_keys_keep_the_writers_order(self) -> None:
        new = {"a": 1, "b": 2, "c": 3, "d": 4}
        assert _edited("a: 1\nd: 4\n", {"a": 1, "d": 4}, new) == "a: 1\nb: 2\nc: 3\nd: 4\n"

    def test_a_file_in_another_order_is_not_reordered(self) -> None:
        new = {"a": 1, "b": 2, "c": 3}
        assert _edited("c: 3\na: 1\n", {"a": 1, "c": 3}, new) == "c: 3\na: 1\nb: 2\n"

    def test_it_lands_after_the_whole_of_a_block_value(self) -> None:
        text = "a:\n  - 1\n  - 2\n# kept\nc: 3\n"
        new = {"a": [1, 2], "b": 9, "c": 3}
        assert _edited(text, {"a": [1, 2], "c": 3}, new) == "a:\n  - 1\n  - 2\nb: 9\n# kept\nc: 3\n"

    def test_it_lands_after_a_block_scalar_without_taking_a_line(self) -> None:
        text = "a: |+\n  x\n\nc: 3\n"
        new = {"a": "x\n\n", "b": 9, "c": 3}
        assert _edited(text, {"a": "x\n\n", "c": 3}, new) == "a: |+\n  x\n\nb: 9\nc: 3\n"

    def test_a_last_line_without_a_break_keeps_having_none(self) -> None:
        assert _edited("a: 1", {"a": 1}, {"a": 1, "b": 2}) == "a: 1\nb: 2"

    def test_the_fragment_is_reindented_to_the_mappings_column(self) -> None:
        text = "top:\n    a: 1\n"
        new = {"top": {"a": 1, "b": {"c": [1, 2]}}}
        assert _edited(text, {"top": {"a": 1}}, new) == "top:\n    a: 1\n    b:\n      c:\n      - 1\n      - 2\n"

    def test_a_key_the_old_side_holds_but_the_file_omits_is_added_when_it_changes(self) -> None:
        assert _edited("a: 1\n", {"a": 1, "b": 2}, {"a": 1, "b": 3}) == "a: 1\nb: 3\n"

    def test_a_key_the_old_side_holds_but_the_file_omits_is_left_out_when_it_does_not(self) -> None:
        assert _edited("a: 1\n", {"a": 1, "b": 2}, {"a": 5, "b": 2}) == "a: 5\n"

    def test_a_key_the_old_side_omits_but_the_file_states_is_replaced_where_it_stands(self) -> None:
        text = "a: 1\nb: 0  # kept\nc: 3\n"
        assert _edited(text, {"a": 1, "c": 3}, {"a": 1, "b": 7, "c": 3}) == "a: 1\nb: 7  # kept\nc: 3\n"

    def test_a_key_neither_side_holds_stays_when_the_file_states_it(self) -> None:
        assert _edited("a: 1\nmine: x\n", {"a": 1}, {"a": 2}) == "a: 2\nmine: x\n"


class TestAKeyRemoved:
    def test_its_line_goes(self) -> None:
        assert _edited("a: 1\nb: 2  # goes\nc: 3\n", {"a": 1, "b": 2, "c": 3}, {"a": 1, "c": 3}) == "a: 1\nc: 3\n"

    def test_a_comment_above_it_stays(self) -> None:
        assert _edited("a: 1\n# stays\nb: 2\n", {"a": 1, "b": 2}, {"a": 1}) == "a: 1\n# stays\n"

    def test_its_block_value_goes_to_the_last_descendant_and_no_further(self) -> None:
        text = "a: 1\nb:\n  c:\n  - 1\n  - 2\n# stays\nd: 3\n"
        old = {"a": 1, "b": {"c": [1, 2]}, "d": 3}
        assert _edited(text, old, {"a": 1, "d": 3}) == "a: 1\n# stays\nd: 3\n"

    def test_a_block_scalar_goes_without_the_next_line(self) -> None:
        assert _edited("a: |\n  x\nb: 2\n", {"a": "x\n", "b": 2}, {"b": 2}) == "b: 2\n"

    def test_a_last_line_without_a_break(self) -> None:
        assert _edited("a: 1\nb: 2", {"a": 1, "b": 2}, {"a": 1}) == "a: 1\n"

    def test_a_key_the_file_omits_is_nothing_to_remove(self) -> None:
        assert _edited("a: 1\n", {"a": 1, "b": 2}, {"a": 3}) == "a: 3\n"


class TestABlockListItem:
    """Each item a line of its own, the rest of the list left where it stands."""

    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("x:\n- 1  # one\n- 2  # two\n", [1, 2], [1, 3], "x:\n- 1  # one\n- 3  # two\n"),
            ("x:\n- 1\n- 3\n", [1, 3], [1, 2, 3], "x:\n- 1\n- 2\n- 3\n"),
            ("x:\n- 2\n- 3\n", [2, 3], [1, 2, 3], "x:\n- 1\n- 2\n- 3\n"),
            ("x:\n- 1\n- 2\n", [1, 2], [1, 2, 3], "x:\n- 1\n- 2\n- 3\n"),
            ("x:\n- 1\n- 2\n- 3\n", [1, 2, 3], [1, 3], "x:\n- 1\n- 3\n"),
            ("x:\n- 1\n- 2\n- 3\n", [1, 2, 3], [2, 3], "x:\n- 2\n- 3\n"),
            ("x:\n- 1\n- 2\n- 3\n", [1, 2, 3], [1, 2], "x:\n- 1\n- 2\n"),
            ("x:\n- 1\n- 2\n- 3\n", [1, 2, 3], [3, 2, 1], "x:\n- 3\n- 2\n- 1\n"),
            ("x:\n- 1\n# kept\ny: 2\n", [1], [], "x: []\n# kept\ny: 2\n"),
            ("x: []\ny: 2\n", [], [1, 2], "x:\n- 1\n- 2\ny: 2\n"),
        ],
        ids=[
            "changed",
            "added-inside",
            "added-first",
            "added-last",
            "removed-inside",
            "removed-first",
            "removed-last",
            "reordered",
            "emptied",
            "filled",
        ],
    )
    def test_an_indentless_list(self, text: str, old: Any, new: Any, result: str) -> None:
        tail = {"y": 2} if "y:" in text else {}
        assert _edited(text, {"x": old, **tail}, {"x": new, **tail}) == result

    @pytest.mark.parametrize(
        ("old", "new", "result"),
        [
            ([1, 2], [1, 3], "top:\n  x:\n    - 1\n    - 3\n  y: 2\n"),
            ([1, 2], [1, 2, 3], "top:\n  x:\n    - 1\n    - 2\n    - 3\n  y: 2\n"),
            ([1, 2], [0, 1, 2], "top:\n  x:\n    - 0\n    - 1\n    - 2\n  y: 2\n"),
            ([1, 2], [2], "top:\n  x:\n    - 2\n  y: 2\n"),
            ([1, 2], [], "top:\n  x: []\n  y: 2\n"),
        ],
        ids=["changed", "added-last", "added-first", "removed", "emptied"],
    )
    def test_an_indented_list(self, old: Any, new: Any, result: str) -> None:
        text = "top:\n  x:\n    - 1\n    - 2\n  y: 2\n"
        assert _edited(text, {"top": {"x": old, "y": 2}}, {"top": {"x": new, "y": 2}}) == result

    def test_an_item_of_mappings_changes_only_its_field(self) -> None:
        text = "x:\n-   name: a  # one\n    n: 1  # two\n-   name: b  # three\n    n: 2  # four\n"
        old = {"x": [{"name": "a", "n": 1}, {"name": "b", "n": 2}]}
        new = {"x": [{"name": "a", "n": 1}, {"name": "b", "n": 9}]}
        assert _edited(text, old, new) == text.replace("n: 2", "n: 9")

    def test_a_list_inside_an_item_is_walked(self) -> None:
        text = "x:\n- name: a\n  sub:\n  - 1\n  - 2  # kept\n  - 3\n"
        old = {"x": [{"name": "a", "sub": [1, 2, 3]}]}
        new = {"x": [{"name": "a", "sub": [1, 2, 4]}]}
        assert _edited(text, old, new) == text.replace("- 3", "- 4")

    def test_a_new_item_stands_at_the_lists_column_in_the_writers_form(self) -> None:
        text = "x:\n    -   name: a\n        n: 1\ny: 2\n"
        old = {"x": [{"name": "a", "n": 1}], "y": 2}
        new = {"x": [{"name": "a", "n": 1}, {"name": "b", "n": 2}], "y": 2}
        assert _edited(text, old, new) == "x:\n    -   name: a\n        n: 1\n    - name: b\n      n: 2\ny: 2\n"

    def test_a_removed_item_goes_through_its_last_descendant(self) -> None:
        text = "x:\n- name: a\n  sub:\n  - 1\n- name: b\n"
        old = {"x": [{"name": "a", "sub": [1]}, {"name": "b"}]}
        assert _edited(text, old, {"x": [{"name": "b"}]}) == "x:\n- name: b\n"

    def test_a_comment_between_items_is_kept_when_one_changes(self) -> None:
        text = "x:\n- 1\n# between\n- 2\n"
        assert _edited(text, {"x": [1, 2]}, {"x": [1, 3]}) == "x:\n- 1\n# between\n- 3\n"

    def test_a_comment_between_items_is_kept_when_an_item_goes_either_side_of_it(self) -> None:
        text = "x:\n- 1\n# between\n- 2\n- 3\n"
        assert _edited(text, {"x": [1, 2, 3]}, {"x": [1, 3]}) == "x:\n- 1\n# between\n- 3\n"
        assert _edited(text, {"x": [1, 2, 3]}, {"x": [2, 3]}) == "x:\n# between\n- 2\n- 3\n"

    def test_one_change_of_many_items_is_one_pass_over_the_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        items = list(range(1000))
        text = "x:\n" + "".join(f"- {item}\n" for item in items)
        passes: list[int] = []
        original = leaf._segments

        def counted(source: str, edits: list[TextEdit]) -> list[str]:
            segments = original(source, edits)
            passes.append(len(segments))
            return segments

        monkeypatch.setattr(leaf, "_segments", counted)
        assert _edited(text, {"x": items}, {"x": items[:1]}) == "x:\n- 0\n"
        assert passes == [2 * 999 + 1], "one join over 999 edits, and no text built anew for each"

    @pytest.mark.parametrize(
        ("text", "new", "expected"),
        [
            ("x:\n- 0\n- 1 # one\n# a\n- 2\n\n# b\n- 3\n", [0], "x:\n- 0\n# a\n\n# b\n"),
            ("x:\n- 0\n- 1\n# a\n- 2\n# b\n- 3", [0, 8], "x:\n- 0\n- 8\n# a\n# b\n"),
            ("x:\n- 0\n# a\n- 1\n- 2\ny: 1\n", [7, 8, 9, 2], "x:\n- 7\n- 8\n- 9\n# a\n- 2\ny: 1\n"),
        ],
        ids=["items-out", "one-for-three-past-a-last-line-without-a-break", "more-in-than-out"],
    )
    def test_comments_between_the_items_of_one_change_stay(self, text: str, new: list[int], expected: str) -> None:
        old = yaml.safe_load(text)["x"]
        tail = {"y": 1} if "y: 1" in text else {}
        assert _edited(text, {"x": old, **tail}, {"x": new, **tail}) == expected

    def test_a_comment_between_two_removed_items_stays(self) -> None:
        text = "x:\n- 1\n- 2\n# between\n- 3\n- 4\n"
        assert _edited(text, {"x": [1, 2, 3, 4]}, {"x": [1, 4]}) == "x:\n- 1\n# between\n- 4\n"

    def test_an_added_item_lands_above_a_comment_that_belongs_to_the_next(self) -> None:
        text = "x:\n- 1\n# of three\n- 3\n"
        assert _edited(text, {"x": [1, 3]}, {"x": [1, 2, 3]}) == "x:\n- 1\n- 2\n# of three\n- 3\n"

    def test_an_item_after_a_block_scalar_lands_past_its_breaks(self) -> None:
        text = "x:\n- |+\n  a\n\n- b\n"
        assert _edited(text, {"x": ["a\n\n", "b"]}, {"x": ["a\n\n", "n", "b"]}) == "x:\n- |+\n  a\n\n- n\n- b\n"

    def test_a_removed_block_scalar_item_keeps_the_next_line(self) -> None:
        assert _edited("x:\n- |\n  a\n- b\n", {"x": ["a\n", "b"]}, {"x": ["b"]}) == "x:\n- b\n"

    def test_a_last_item_without_a_break_keeps_having_none(self) -> None:
        assert _edited("x:\n- 1", {"x": [1]}, {"x": [1, 2]}) == "x:\n- 1\n- 2"
        assert _edited("x:\n- 1\n- 2", {"x": [1, 2]}, {"x": [1, 5, 6]}) == "x:\n- 1\n- 5\n- 6"

    def test_an_empty_item_is_filled(self) -> None:
        assert _edited("x:\n-\n- b\n", {"x": [None, "b"]}, {"x": ["a", "b"]}) == "x:\n- a\n- b\n"

    def test_an_item_changing_kind_is_replaced_by_its_lines(self) -> None:
        text = "x:\n- 1\n- 2\n"
        assert _edited(text, {"x": [1, 2]}, {"x": [1, {"a": 1, "b": 2}]}) == "x:\n- 1\n- a: 1\n  b: 2\n"

    def test_a_replaced_group_of_another_size_is_lines_out_and_lines_in(self) -> None:
        text = "x:\n- 1\n- 2\n- 3\n- 9\n"
        assert _edited(text, {"x": [1, 2, 3, 9]}, {"x": [1, 7, 9]}) == "x:\n- 1\n- 7\n- 9\n"

    def test_a_group_replaced_in_a_list_holds_the_comment_above_it(self) -> None:
        text = "x:\n- 1\n# kept\n- 2\n- 3\n- 9\n"
        assert _edited(text, {"x": [1, 2, 3, 9]}, {"x": [1, 7, 9]}) == "x:\n- 1\n# kept\n- 7\n- 9\n"

    def test_every_item_of_the_list_replaced_leaves_it_a_list(self) -> None:
        assert _edited("x:\n- 1\n- 2\ny: 3\n", {"x": [1, 2], "y": 3}, {"x": [7], "y": 3}) == "x:\n- 7\ny: 3\n"

    def test_equal_items_are_told_apart_by_type(self) -> None:
        assert _edited("x:\n- 1\n- 2\n", {"x": [1, 2]}, {"x": [1.0, 2]}) == "x:\n- 1.0\n- 2\n"

    def test_a_key_order_inside_an_item_is_no_change(self) -> None:
        text = "x:\n- b: 1\n  a: 2\n"
        old = {"x": [{"a": 2, "b": 1}], "y": 1}
        assert _edited(text + "y: 1\n", old, {"x": [{"b": 1, "a": 2}], "y": 2}) == text + "y: 2\n"

    def test_a_long_list_with_a_popular_item_is_not_lost_to_the_junk_heuristic(self) -> None:
        # Past the 200 items the heuristic needs and within the match budget, and a comment on a popular
        # item's own line: only an alignment that kept that line keeps the comment, and the value would not tell.
        old = ["same"] * 600 + ["a", "z"]
        new = ["same"] * 300 + ["n"] + ["same"] * 300 + ["z", "a"]
        lines = [f"- {item}" for item in old]
        lines[450] += "  # kept"
        expected = [*lines[:300], "- n", *lines[300:600], "- z", "- a"]
        text = "x:\n" + "".join(f"{line}\n" for line in lines)
        assert _edited(text, {"x": old}, {"x": new}) == "x:\n" + "".join(f"{line}\n" for line in expected)

    def test_two_changes_far_apart_in_a_long_list_of_distinct_items_stay_lines(self) -> None:
        old = [f"item{index}" for index in range(600)]
        new = ["first", *old[1:-1], "last"]
        text = "x:\n" + "".join(f"- {item}  # {item}\n" for item in old)
        expected = text.replace("- item0  # item0", "- first  # item0").replace("- item599  #", "- last  #")
        assert _edited(text, {"x": old}, {"x": new}) == expected

    def test_a_reversed_long_list_stays_within_the_limit(self) -> None:
        old = list(range(200))
        text = "x:\n" + "".join(f"- {item}\n" for item in old)
        new = old[::-1]
        assert load_yaml_document(_edited(text, {"x": old}, {"x": new})) == {"x": new}


class TestSeveralChangesInOneEdit:
    """Each change a statement about one clean text, whatever the others moved."""

    @staticmethod
    def _items(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
        return [{"n": name, "v": value} for name, value in pairs]

    def test_a_deletion_before_a_pair_writes_the_pair_from_its_own_place(self) -> None:
        # Near-twins on purpose: a `written_path` off by one reads back as a sibling's text.
        text = "x:\n- n: a\n  v: 1\n# b\n- n: b\n  v: 2\n- n: c   # c\n  v: 3\n- n: d\n  v: 4\n"
        old = self._items(("a", 1), ("b", 2), ("c", 3), ("d", 4))
        new = self._items(("b", 2), ("c", 9), ("d", 4))
        assert _edited(text, {"x": old}, {"x": new}) == (
            "x:\n# b\n- n: b\n  v: 2\n- n: c   # c\n  v: 9\n- n: d\n  v: 4\n"
        )

    def test_an_insertion_before_a_pair(self) -> None:
        text = "x:\n- n: b\n  v: 2\n- n: c   # c\n  v: 3\n"
        old = self._items(("b", 2), ("c", 3))
        new = self._items(("a", 1), ("b", 2), ("c", 9))
        assert _edited(text, {"x": old}, {"x": new}) == "x:\n- n: a\n  v: 1\n- n: b\n  v: 2\n- n: c   # c\n  v: 9\n"

    def test_a_key_removed_beside_a_nested_list_changed(self) -> None:
        text = "a: 1  # goes\n# kept\nb:\n  c:\n  - 1   # one\n  - 2\nd: 3\n"
        old = {"a": 1, "b": {"c": [1, 2]}, "d": 3}
        new = {"b": {"c": [1, 2, 7]}, "d": 3}
        assert _edited(text, old, new) == "# kept\nb:\n  c:\n  - 1   # one\n  - 2\n  - 7\nd: 3\n"

    def test_two_fields_of_one_item(self) -> None:
        text = "x:\n- k: 1   # k\n  # between\n  n: a\n  v: 2\n"
        old = {"x": [{"k": 1, "n": "a", "v": 2}]}
        new = {"x": [{"k": 5, "n": "a", "v": 6}]}
        assert _edited(text, old, new) == "x:\n- k: 5   # k\n  # between\n  n: a\n  v: 6\n"

    def test_a_field_of_each_item_of_a_foreign_layout(self) -> None:
        text = "x:\n    -   n: 'a'\n        v: 1\n\n    -   n: 'b'\n        v: 2\n"
        old = {"x": self._items(("a", 1), ("b", 2))}
        new = {"x": self._items(("a", 5), ("b", 6))}
        assert _edited(text, old, new) == text.replace("v: 1", "v: 5").replace("v: 2", "v: 6")


class TestALeadingDocumentMarker:
    @pytest.mark.parametrize("head", ["---\n", "--- # c\n"], ids=["bare", "with-a-comment"])
    def test_a_key_replaced_added_and_removed_below_it(self, head: str) -> None:
        text = f"{head}a: 1\nb: 2\n"
        assert _edited(text, {"a": 1, "b": 2}, {"a": 3, "b": 2}) == f"{head}a: 3\nb: 2\n"
        assert _edited(text, {"a": 1, "b": 2}, {"a": 1, "b": 2, "c": 3}) == f"{head}a: 1\nb: 2\nc: 3\n"
        assert _edited(text, {"a": 1, "b": 2}, {"z": 0, "a": 1, "b": 2}) == f"{head}z: 0\na: 1\nb: 2\n"
        assert _edited(text, {"a": 1, "b": 2}, {"b": 2}) == f"{head}b: 2\n"

    def test_a_directive_before_it_is_a_marker_past_the_first_line(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("%YAML 1.1\n---\na: 1\n", {"a": 1}, {"a": 2})
        assert str(refused.value) == "the file holds a document marker"


class TestAKeyOnAListItemsLine:
    """An insert lands at the key's own position, a removal runs to the next key, both through the list walk."""

    def test_a_key_is_inserted_at_the_first_keys_own_position(self) -> None:
        old = {"x": [{"b": 1}]}
        new = {"x": [{"a": 0, "b": 1}]}
        assert _edited("x:\n- b: 1\n", old, new) == "x:\n- a: 0\n  b: 1\n"

    def test_a_key_is_inserted_after_a_key_on_the_line(self) -> None:
        old = {"x": [{"a": 1, "c": 3}]}
        new = {"x": [{"a": 1, "b": [2], "c": 3}]}
        assert _edited("x:\n-   a: 1\n    c: 3\n", old, new) == "x:\n-   a: 1\n    b:\n    - 2\n    c: 3\n"

    def test_a_comment_between_the_first_key_and_the_next_stays_above_the_item(self) -> None:
        text = "items:\n- a: 1\n  # kept\n\n  b: 2\nc: 3\n"
        old = {"items": [{"a": 1, "b": 2}], "c": 3}
        new = {"items": [{"b": 2}], "c": 3}
        assert _edited(text, old, new) == "items:\n  # kept\n\n- b: 2\nc: 3\n"

    def test_a_block_scalar_first_key_keeps_the_comment_behind_it(self) -> None:
        text = "items:\n- a: |\n    one\n  # kept\n  b: 2\n"
        old = {"items": [{"a": "one\n", "b": 2}]}
        new = {"items": [{"b": 2}]}
        assert _edited(text, old, new) == "items:\n  # kept\n- b: 2\n"

    def test_the_first_key_goes_to_the_next_key(self) -> None:
        old = {"x": [{"a": 1, "b": 2}]}
        assert _edited("x:\n- a: 1\n  b: 2\n", old, {"x": [{"b": 2}]}) == "x:\n- b: 2\n"

    def test_a_later_key_goes_by_its_lines(self) -> None:
        old = {"x": [{"a": 1, "b": 2, "c": 3}]}
        new = {"x": [{"a": 1, "c": 3}]}
        assert _edited("x:\n- a: 1\n  b: 2\n  c: 3\n", old, new) == "x:\n- a: 1\n  c: 3\n"

    def test_the_only_key_exchanged_for_another_stays_a_list_item(self) -> None:
        assert _edited("x:\n- a: 1\n", {"x": [{"a": 1}]}, {"x": [{"b": 2}]}) == "x:\n- b: 2\n"

    def test_the_only_key_with_nothing_beside_it_is_refused(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("x:\n- a: 1\n", {"x": [{"a": 1, "b": 2}]}, {"x": [{"b": 2}]})
        assert str(refused.value) == "the file would be left with an empty list item"


class TestAFlowCollection:
    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("a: [1, 2]  # kept\nb: 2\n", [1, 2], [1, 3], "a: [1, 3]  # kept\nb: 2\n"),
            ("a: [\n    1,\n    2,  # gone\n  ]  # kept\nb: 2\n", [1, 2], [1, 3], "a: [1, 3]  # kept\nb: 2\n"),
            ("a: [[1], [2, [3]]]\nb: 2\n", [[1], [2, [3]]], [[1], [2, [4]]], "a: [[1], [2, [4]]]\nb: 2\n"),
            ("a: {z: 1, y: 2}\nb: 2\n", {"z": 1, "y": 2}, {"z": 1, "y": 3}, "a: {z: 1, y: 3}\nb: 2\n"),
            ("a: {k: [1], m: {n: 2}}\nb: 2\n", {"k": [1], "m": {"n": 2}}, {"k": [2]}, "a: {k: [2]}\nb: 2\n"),
            ("a: [1, 2]\nb: 2\n", [1, 2], {"k": 1}, "a: {k: 1}\nb: 2\n"),
            ("a: [1, 2]\nb: 2\n", [1, 2], [], "a: []\nb: 2\n"),
        ],
        ids=["one-line", "multi-line", "nested", "mapping", "mapping-nested", "list-to-mapping", "emptied"],
    )
    def test_a_flow_collection_stays_one(self, text: str, old: Any, new: Any, result: str) -> None:
        assert _edited(text, {"a": old, "b": 2}, {"a": new, "b": 2}) == result

    def test_the_findings_own_init(self) -> None:
        text = "init: [20, 20, 20, 20]\n"
        assert _edited(text, {"init": [20] * 4}, {"init": [20, 20, 25, 20]}) == "init: [20, 20, 25, 20]\n"

    def test_a_long_value_stays_on_one_line(self) -> None:
        old = {"a": ["x"]}
        new = {"a": ["word " * 100, "y"]}
        assert _edited("a: [x]\n", old, new) == f"a: ['{'word ' * 100}', y]\n"

    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("a: [1, 2]  # kept\nb: 2\n", [1, 2], 7, "a: 7  # kept\nb: 2\n"),
            ("a: {k: 1}\nb: 2\n", {"k": 1}, "text", "a: text\nb: 2\n"),
        ],
        ids=["list", "mapping"],
    )
    def test_a_new_value_that_is_no_collection_takes_the_value_region(
        self, text: str, old: Any, new: Any, result: str
    ) -> None:
        assert _edited(text, {"a": old, "b": 2}, {"a": new, "b": 2}) == result

    def test_a_block_value_going_to_a_collection_of_another_kind_takes_the_writers_form(self) -> None:
        assert _edited("a:\n- 1\nb: 2\n", {"a": [1], "b": 2}, {"a": {"k": 1}, "b": 2}) == "a:\n  k: 1\nb: 2\n"

    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("a: []  # gone\nb: 2\n", [], [1, 2], "a:\n- 1\n- 2  # gone\nb: 2\n"),
            ("a: {}\nb: 2\n", {}, {"k": [1]}, "a:\n  k:\n  - 1\nb: 2\n"),
        ],
        ids=["list", "mapping"],
    )
    def test_an_empty_flow_filled_takes_the_writers_form(self, text: str, old: Any, new: Any, result: str) -> None:
        assert _edited(text, {"a": old, "b": 2}, {"a": new, "b": 2}) == result

    def test_a_flow_item_of_a_block_list_stays_flow(self) -> None:
        text = "x:\n- [1, 2]  # kept\n- {a: 1}\n"
        old = {"x": [[1, 2], {"a": 1}]}
        new = {"x": [[1, 3], {"a": 2}]}
        assert _edited(text, old, new) == "x:\n- [1, 3]  # kept\n- {a: 2}\n"

    def test_a_flow_list_under_an_item_stays_flow(self) -> None:
        text = "x:\n- name: a\n  init: [1, 2]\n"
        new = {"x": [{"name": "a", "init": [1, 3]}]}
        assert _edited(text, {"x": [{"name": "a", "init": [1, 2]}]}, new) == "x:\n- name: a\n  init: [1, 3]\n"

    def test_an_empty_flow_item_is_filled_in_the_writers_form(self) -> None:
        assert _edited("x:\n- []\n- 1\n", {"x": [[], 1]}, {"x": [[5], 1]}) == "x:\n- - 5\n- 1\n"

    def test_a_flow_item_going_to_a_scalar_is_replaced_by_its_lines(self) -> None:
        assert _edited("x:\n- [1]\n- 1\n", {"x": [[1], 1]}, {"x": [5, 1]}) == "x:\n- 5\n- 1\n"


class TestUniformCrlf:
    def test_a_scalar_replaced(self) -> None:
        assert _edited("a: 1\r\nb: 2\r\n", {"a": 1, "b": 2}, {"a": 3, "b": 2}) == "a: 3\r\nb: 2\r\n"

    def test_a_key_added_and_a_key_removed(self) -> None:
        text = "a: 1\r\nb: 2\r\n"
        assert _edited(text, {"a": 1, "b": 2}, {"a": 1, "c": {"d": 1}}) == "a: 1\r\nc:\r\n  d: 1\r\n"

    def test_a_list_item_added(self) -> None:
        assert _edited("x:\r\n- 1\r\n", {"x": [1]}, {"x": [1, {"a": 1, "b": 2}]}) == "x:\r\n- 1\r\n- a: 1\r\n  b: 2\r\n"

    def test_a_flow_list_changed(self) -> None:
        assert _edited("x: [1, 2]\r\n", {"x": [1, 2]}, {"x": [1, 3]}) == "x: [1, 3]\r\n"

    def test_no_bare_line_feed_is_left(self) -> None:
        edited = _edited("x:\r\n- 1\r\n# c\r\n- 2\r\n", {"x": [1, 2]}, {"x": [0, 1]})
        assert edited.count("\n") == edited.count("\r\n")


#: A file whose layout an edit must leave alone.
_FINDING_FILE = """\
name: IBrakeTemperature
kind: sender_receiver
description: Brake disc temperature per wheel
data_elements:
- name: DiscTemperature
  type: Temperature_Array
  init: [20, 20, 20, 20]
  asil: B
  description: The measured temperature of each brake disc, in a sentence long enough for the writer to wrap
- name: PadWear
  type: Wear_Array
  asil: B
  init: [0, 0, 0, 0]
  description: The estimated wear of each brake pad, in a sentence long enough for the writer to wrap as well
"""


class TestTheFinding:
    """One reviewed field, one changed line."""

    @staticmethod
    def _old() -> dict[str, Any]:
        order = ("name", "type", "init", "asil", "description")
        loaded = load_yaml_document(_FINDING_FILE)
        elements = [{name: element[name] for name in order} for element in loaded["data_elements"]]
        return {**loaded, "data_elements": elements}

    def _diff(self, new: dict[str, Any]) -> list[str]:
        edited = _edited(_FINDING_FILE, self._old(), new)
        assert load_yaml_document(edited) == new
        return [
            line
            for line in difflib.unified_diff(_FINDING_FILE.splitlines(), edited.splitlines(), lineterm="", n=0)
            if line[0] in "+-" and line[:3] not in ("+++", "---")
        ]

    def test_the_description_is_a_one_line_diff(self) -> None:
        new = self._old()
        new["description"] = "Brake disc temperature, one value per wheel"
        assert self._diff(new) == [
            "-description: Brake disc temperature per wheel",
            "+description: Brake disc temperature, one value per wheel",
        ]

    def test_one_value_of_the_flow_init_is_a_one_line_diff(self) -> None:
        new = self._old()
        new["data_elements"][1]["init"] = [0, 0, 1, 0]
        assert self._diff(new) == ["-  init: [0, 0, 0, 0]", "+  init: [0, 0, 1, 0]"]

    def test_a_data_element_added_touches_no_existing_line(self) -> None:
        new = self._old()
        new["data_elements"].append({"name": "Spare", "type": "Spare_T"})
        assert self._diff(new) == ["+- name: Spare", "+  type: Spare_T"]


class TestListRefusals:
    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            ("x:\n- - a\n- - b\n", [["a"], ["b"]], [["a"], ["c"]]),
            ("x:\n- 1\n- - a\n", [1, ["a"]], [2, ["a"]]),
        ],
        ids=["item-of-a-nested-list", "another-item-beside-one"],
    )
    def test_an_item_on_a_dash_of_another_list_is_refused(self, text: str, old: Any, new: Any) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"x": old}, {"x": new})
        assert str(refused.value) == "a list item does not stand on a line of its own"

    @pytest.mark.parametrize(
        "text",
        ["x:\n-\n  a: 1\n- b\n", "x:\n- # note\n  a: 1\n- b\n"],
        ids=["dash-alone", "dash-and-comment"],
    )
    def test_an_item_off_its_dash_line_is_refused(self, text: str) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"x": [{"a": 1}, "b"]}, {"x": [{"a": 1}, "c"]})
        assert str(refused.value) == "a list item does not stand on a line of its own"

    def test_a_list_of_another_length_than_the_old_side_is_refused(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("x:\n- 1\n- 2\n- 3\n", {"x": [1, 2]}, {"x": [1, 5]})
        assert str(refused.value) == "a list in the file differs in length from the model's"

    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            ("x:\n- &one 1\n- *one\n", [1, 1], [1, 2]),
            ("x:\n- 1\n- !!int 2\n", [1, 2], [1, 3]),
            ("x:\n- 1\n- &m\n  k: 1\n", [1, {"k": 1}], [2, {"k": 1}]),
        ],
        ids=["alias", "tag", "anchor"],
    )
    def test_an_indirect_item_is_refused(self, text: str, old: Any, new: Any) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"x": old}, {"x": new})
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_a_mapping_item_on_the_path_is_refused_as_a_mapping_is(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("x:\n- a: 1\n  a: 2\n", {"x": [{"a": 1}]}, {"x": [{"a": 5}]})
        assert str(refused.value) == "the file gives a key twice"

    def test_a_flow_list_needs_no_item_of_its_own_line(self) -> None:
        assert _edited("x: [[a], [b]]\n", {"x": [["a"], ["b"]]}, {"x": [["a"], ["c"]]}) == "x: [[a], [c]]\n"


class TestAValueRegion:
    @pytest.mark.parametrize(
        ("text", "old", "new", "result"),
        [
            ("a: 1  # kept\nb: 2\n", 1, [1, 2], "a:\n- 1\n- 2  # kept\nb: 2\n"),
            ("a:\n- 1\n- 2\n# kept\nb: 2\n", [1, 2], 1, "a: 1\n# kept\nb: 2\n"),
            ("a:\n  - 1\n# kept\nb: 2\n", [1], [], "a: []\n# kept\nb: 2\n"),
            ("a:\n  k: 1\n# kept\nb: 2\n", {"k": 1}, {}, "a: {}\n# kept\nb: 2\n"),
            ("a: []\nb: 2\n", [], [1], "a:\n- 1\nb: 2\n"),
            ("a: {}\nb: 2\n", {}, {"k": 1}, "a:\n  k: 1\nb: 2\n"),
            ("a:\nb: 2\n", None, [1], "a:\n- 1\nb: 2\n"),
            ("a: |\n  x\n\n# kept\nb: 2\n", "x\n", [1], "a:\n- 1\n\n# kept\nb: 2\n"),
        ],
        ids=[
            "scalar-to-list",
            "list-to-scalar",
            "list-emptied",
            "map-emptied",
            "list-filled",
            "map-filled",
            "null",
            "block",
        ],
    )
    def test_it_runs_from_the_key_through_the_last_descendant(self, text: str, old: Any, new: Any, result: str) -> None:
        assert _edited(text, {"a": old, "b": 2}, {"a": new, "b": 2}) == result

    def test_it_shifts_by_the_column_of_the_file(self) -> None:
        text = "top:\n    a: 1\n    b: 2\n"
        old = {"top": {"a": 1, "b": 2}}
        new = {"top": {"a": {"k": [1]}, "b": 2}}
        assert _edited(text, old, new) == "top:\n    a:\n      k:\n      - 1\n    b: 2\n"


class TestRefusedForms:
    """Each refusal a fixed sentence, none of them holding text of the model."""

    @pytest.mark.parametrize(
        ("text", "sentence"),
        [
            ("a: x\r\nb: 1\n", "the file has mixed line ends"),
            ("a: x\rb: 1\r", "the file has mixed line ends"),
            ("\ufeffa: x\n", "the file holds a byte-order mark"),
            ("\n\ufeffa: x\n", "the file holds a byte-order mark"),
            ("a: 'x\ufeffy'\n", "the file holds a byte-order mark"),
            ("a: 'x\x85y'\n", "the file holds a character YAML reads as a line break"),
            ("a: 'x\u2028y'\n", "the file holds a character YAML reads as a line break"),
            ("a: 'x\u2029y'\n", "the file holds a character YAML reads as a line break"),
            ("a: x\n---\nb: 1\n", "the file holds a document marker"),
            ("a: x\n...\n", "the file holds a document marker"),
            ("a: [\n", "the file cannot be read as YAML"),
            ("# nothing\n", "the file is empty"),
            ("- a\n", "the file's root is no block mapping"),
            ("{a: x}\n", "the file's root is no block mapping"),
            ("!!map\na: x\n", "the file writes a value through an alias, an anchor or a tag"),
            ("&root\na: x\n", "the file writes a value through an alias, an anchor or a tag"),
            ("a: &one x\nb: 1\n", "the file writes a value through an alias, an anchor or a tag"),
            ("b: &one x\na: *one\n", "the file writes a value through an alias, an anchor or a tag"),
            ("a: !!str x\nb: 1\n", "the file writes a value through an alias, an anchor or a tag"),
            ("a: x\nb: 1\nb: 2\n", "the file gives a key twice"),
            ("base: &b {c: 1}\n<<: *b\na: x\n", "the file uses a merge key"),
            ("? a\n# gone\nb: 1\n", "the file writes a key in the explicit form"),
            ("? a\n: x\nb: 1\n", "the file writes a key in the explicit form"),
        ],
        ids=[
            "mixed-line-ends",
            "bare-cr",
            "bom-start",
            "bom-after-a-blank-line",
            "bom-inside-a-string",
            "nel",
            "line-separator",
            "paragraph-separator",
            "second-document",
            "document-end",
            "not-yaml",
            "empty",
            "root-sequence",
            "root-flow",
            "root-tag",
            "root-anchor",
            "anchor",
            "alias",
            "tag",
            "duplicate-key",
            "merge-key",
            "explicit-key-no-colon",
            "explicit-key",
        ],
    )
    def test_each_refusal_says_which(self, text: str, sentence: str) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"a": "x", "b": 1}, {"a": "SECRET", "b": 1})
        assert str(refused.value) == sentence
        assert "SECRET" not in str(refused.value)

    def test_an_explicit_key_deep_in_a_value_that_is_not_on_the_path_is_refused(self) -> None:
        # `last_end` of `b` would run past the comment, which no read-back can see.
        with pytest.raises(FormRefused) as refused:
            _edited("a: 1\nb:\n  ? k\n# gone\nc: 3\n", {"a": 1, "b": {"k": None}, "c": 3}, {"a": 1, "c": 3})
        assert str(refused.value) == "the file writes a key in the explicit form"

    def test_a_scan_for_explicit_keys_ends_on_a_self_referring_anchor(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("a: &x [*x]\nb: 1\n", {"a": [], "b": 1}, {"a": [1], "b": 1})
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_a_removed_value_ending_in_an_alias_is_refused(self) -> None:
        text = "a: &x 1\nb:\n  c: *x\n"
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"b": {"c": 1}, "z": 1}, {"z": 1})
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_a_key_placed_after_a_value_ending_in_an_alias_is_refused(self) -> None:
        text = "a: &x 1\nb:\n  c: *x\n"
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"b": {"c": 1}}, {"b": {"c": 1}, "d": 2})
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            (
                "x:\n- n: 1\n  zz_anchor: &x kept\n  zz_note: *x\n",
                {"x": [{"n": 1}]},
                {"x": [{"n": 1}, {"n": 2}]},
            ),
            (
                "m:\n  n:\n    k: 1\n    zz_anchor: &x kept\n    zz_note: *x\n",
                {"m": {"n": {"k": 1}}},
                {"m": {"n": {"k": 1}, "o": 2}},
            ),
            ("m:\n  n: 1\n  zz_anchor: &x kept\n  zz_note: *x\nz: 1\n", {"m": {"n": 1}, "z": 1}, {"z": 1}),
            (
                "x:\n- n: 1\n- n: 2\n  zz_anchor: &x kept\n  zz_note: *x\n",
                {"x": [{"n": 1}, {"n": 2}]},
                {"x": [{"n": 1}]},
            ),
            (
                "x:\n- n: 1\n  sub:\n  - k: 1\n    zz_anchor: &x kept\n    zz_note: *x\n",
                {"x": [{"n": 1, "sub": [{"k": 1}]}]},
                {"x": [{"n": 1, "sub": [{"k": 1}]}, {"n": 2, "sub": [{"k": 2}]}]},
            ),
            ("m:\n  n: 1\n  zz_anchor: &x kept\n  zz_note: *x\nz: 1\n", {"m": {"n": 1}, "z": 1}, {"m": 3, "z": 1}),
            (
                "x:\n- n: 1\n  sub:\n  - &p 1\n  - &q 2\n  - *p\n  - *q\n",
                {"x": [{"n": 1}]},
                {"x": [{"n": 1}, {"n": 2}]},
            ),
            ("a: &x kept\nm:\n  sub:\n  - *x\n", {"m": {}}, {"m": {}, "z": 1}),
            ("a: &x {k: 1}\nm:\n  n: 1\n  <<: *x\n", {"m": {"n": 1}}, {"m": {"n": 1}, "z": 1}),
        ],
        ids=[
            "item-appended-after",
            "mapping-gains-a-key",
            "removed-key",
            "removed-item",
            "nested-under-the-last-item",
            "value-region",
            "two-aliases-in-their-anchors-order",
            "only-item-of-a-list",
            "merge-key-at-the-end",
        ],
    )
    def test_an_end_on_an_alias_is_refused_and_nothing_moves(self, text: str, old: Any, new: Any) -> None:
        # What an undeclared key lost to the next element looks like to a read-back: nothing.
        with pytest.raises(FormRefused) as refused:
            _edited(text, old, new)
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            ("f: {&x k: 1}\ng:\n  *x : 2\n  h: 3\n", {"g": {"k": 2, "h": 3}}, {"g": {"i": 1, "k": 2, "h": 3}}),
            ("f: {&x k: 1}\nl:\n- a: 1\n  *x : 2\n", {"l": [{"a": 1, "k": 2}]}, {"l": [{"k": 2}]}),
            ("f: {&x k: 1}\ng:\n  h: 3\n  *x : 2\n", {"g": {"k": 2, "h": 3}}, {"g": {"k": 2, "h": 4}}),
        ],
        ids=["first-key", "next-key-of-a-dash-line", "last-key"],
    )
    def test_a_key_written_through_an_alias_is_refused_on_the_path(self, text: str, old: Any, new: Any) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, old, new)
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_an_alias_in_a_flow_collection_ends_at_its_bracket(self) -> None:
        text = "x:\n- n: 1\n  zz_anchor: &x kept\n  zz_note: {a: *x}\n"
        new = {"x": [{"n": 1}, {"n": 2}]}
        written = yaml.safe_dump(new, sort_keys=False)
        assert edit_element_text(text, {"x": [{"n": 1}]}, new, written) == f"{text}- n: 2\n"

    def test_an_alias_the_change_reads_no_end_of_stays(self) -> None:
        text = "x:\n- n: 1\n  zz_anchor: &x kept\n  zz_note: *x\ny: 1\n"
        assert _edited(text, {"x": [{"n": 1}], "y": 1}, {"x": [{"n": 1}], "y": 2}) == text.replace("y: 1", "y: 2")

    def test_a_key_placed_before_an_undeclared_alias_reads_no_end_of_it(self) -> None:
        text = "m:\n  n: 1\n  zz_anchor: &x kept\n  zz_note: *x\n"
        assert _edited(text, {"m": {"n": 1}}, {"m": {"n": 1, "o": 2}}) == text.replace("n: 1\n", "n: 1\n  o: 2\n")

    def test_a_key_the_writer_itself_writes_in_the_explicit_form_is_refused(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("a: 1\n", {"a": 1}, {"a": 1, "k" * 200: 2})
        assert str(refused.value) == "the file writes a key in the explicit form"

    def test_an_anchor_outside_the_path_is_no_refusal(self) -> None:
        assert _edited("a: x\nb: &one 1\nc: *one\n", {"a": "x"}, {"a": "y"}) == "a: y\nb: &one 1\nc: *one\n"

    def test_a_merge_key_outside_the_path_is_no_refusal(self) -> None:
        text = "a: x\nb: {c: 1}\nd:\n  <<: {e: 1}\n"
        assert _edited(text, {"a": "x"}, {"a": "y"}) == text.replace("a: x", "a: y")

    def test_a_duplicate_key_outside_the_path_is_no_refusal(self) -> None:
        text = "a: x\nb:\n  c: 1\n  c: 2\n"
        assert _edited(text, {"a": "x"}, {"a": "y"}) == text.replace("a: x", "a: y")

    def test_an_explicit_key_in_a_flow_mapping_is_no_refusal(self) -> None:
        assert _edited("a: x\nb: {c, d: 1}\n", {"a": "x"}, {"a": "y"}) == "a: y\nb: {c, d: 1}\n"

    @pytest.mark.parametrize(
        "text",
        ["a: 1\r\nb: 2\n", "a: 1\nb: 2\r\n", "a: 1\rb: 2\r", "a: 1\r\nb: 2\r"],
        ids=["crlf-then-lf", "lf-then-crlf", "bare-cr", "crlf-then-bare-cr"],
    )
    def test_mixed_line_ends_are_refused(self, text: str) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited(text, {"a": 1, "b": 2}, {"a": 3, "b": 2})
        assert str(refused.value) == "the file has mixed line ends"

    def test_an_edit_that_moves_no_byte_is_refused(self) -> None:
        # The file already holds the new value: a `prior` that disagrees with it.
        with pytest.raises(FormRefused) as refused:
            _edited("a: 2\n", {"a": 1}, {"a": 2})
        assert str(refused.value) == "the line edit found no line to change"

    def test_an_edit_that_moves_no_byte_is_refused_in_crlf_too(self) -> None:
        with pytest.raises(FormRefused) as refused:
            _edited("a: 2\r\n", {"a": 1}, {"a": 2})
        assert str(refused.value) == "the line edit found no line to change"

    @pytest.mark.parametrize(
        ("text", "old", "new"),
        [
            ("x:\n- n: 1\n  junk: &j\n  - *j\n", {"x": [{"n": 1}]}, {"x": [{"n": 2}]}),
            ("x:\n  n: 1\n  junk: &j\n  - *j\n", {"x": {"n": 1}}, {"x": 3}),
            ("x:\n- a\n- &j\n  k: *j\n", {"x": ["a", {}]}, {"x": ["b", {}]}),
            ("x:\n- n: 1\n  junk: &j\n    k: *j\ny: 1\n", {"x": [{"n": 1}], "y": 1}, {"x": [], "y": 1}),
        ],
        ids=["item-walked", "value-replaced", "anchored-item", "item-removed"],
    )
    def test_a_recursive_alias_is_refused_and_the_edit_ends(self, text: str, old: Any, new: Any) -> None:
        with at_most_steps(), pytest.raises(FormRefused) as refused:
            _edited(text, old, new)
        assert str(refused.value) == "the file writes a value through an alias, an anchor or a tag"

    def test_a_uniform_crlf_file_is_no_refusal(self) -> None:
        assert _edited("a: x\r\nb: 1\r\n", {"a": "x", "b": 1}, {"a": "y", "b": 1}) == "a: y\r\nb: 1\r\n"
