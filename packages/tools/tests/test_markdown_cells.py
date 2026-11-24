"""The safe-Markdown helpers a report's tables are rendered through, against hand-written expectations."""

from __future__ import annotations

from swcraft_tools.markdown_cells import cell_text, code_fence, code_span, prose_code_span, table


class TestCellText:
    def test_a_pipe_is_escaped(self) -> None:
        assert cell_text("a|b") == "a\\|b"

    def test_a_backslash_before_a_pipe_cannot_end_the_cell(self) -> None:
        """Guards `\\|` reading `\\\\|` -- an escaped backslash, then a bare pipe."""
        assert cell_text("a\\|b") == "a\\\\\\|b"

    def test_a_line_break_becomes_a_space(self) -> None:
        assert cell_text("first\nsecond\r\nthird") == "first second third"

    def test_lt_and_amp_are_entities(self) -> None:
        assert cell_text("<b>x</b> & y") == "&lt;b>x&lt;/b> &amp; y"

    def test_a_plain_text_is_unchanged(self) -> None:
        assert cell_text("Core0 at 40 %") == "Core0 at 40 %"


class TestCodeSpan:
    def test_a_name_is_set_in_a_code_span(self) -> None:
        assert code_span("ExampleSWC") == "`ExampleSWC`"

    def test_a_name_holding_a_backtick_gets_a_double_backtick_span(self) -> None:
        assert code_span("a`b") == "`` a`b ``"

    def test_a_pipe_and_a_line_break_as_in_a_cell_and_no_backslash_doubled(self) -> None:
        assert code_span("a|b\nc\\d") == "`a\\|b c\\d`"

    def test_a_backslash_before_a_pipe_cannot_end_the_cell(self) -> None:
        """Guards `a\\|b` reading `a\\\\|b`, which cmark-gfm splits at the pipe; the run is doubled."""
        assert code_span("a\\|b") == "`a\\\\\\|b`"
        assert code_span("a\\\\|b") == "`a" + "\\" * 5 + "|b`"

    def test_a_run_of_backticks_gets_a_longer_fence(self) -> None:
        assert code_span("a``b") == "``` a``b ```"
        assert code_span("``x`") == "``` ``x` ```"


class TestProseCodeSpan:
    def test_a_pipe_is_kept_and_a_line_break_becomes_a_space(self) -> None:
        assert prose_code_span("a|b\nc\\d") == "`a|b c\\d`"

    def test_a_name_holding_a_backtick_gets_a_double_backtick_span(self) -> None:
        assert prose_code_span("a`b|c") == "`` a`b|c ``"

    def test_a_run_of_backticks_gets_a_longer_fence_and_a_pipe_after_a_backslash_is_kept(self) -> None:
        assert prose_code_span("a``b\\|c") == "``` a``b\\|c ```"


class TestTheTable:
    def test_a_table_is_header_delimiter_and_rows(self) -> None:
        assert table(("Name", "Kind"), [("`A`", "core"), ("`B`", "memory")]) == [
            "| Name | Kind |",
            "| --- | --- |",
            "| `A` | core |",
            "| `B` | memory |",
        ]

    def test_a_table_without_rows_is_its_header_alone(self) -> None:
        assert table(("Name",), []) == ["| Name |", "| --- |"]


class TestCodeFence:
    def test_lines_without_backticks_get_a_fence_of_three(self) -> None:
        assert code_fence(["a: 1", "  b: 2"]) == ["```", "a: 1", "  b: 2", "```"]

    def test_the_fence_is_one_longer_than_the_longest_run_in_any_line(self) -> None:
        assert code_fence(["x ```` y", "``"]) == ["`````", "x ```` y", "``", "`````"]

    def test_no_line_is_a_fence_alone(self) -> None:
        assert code_fence([]) == ["```", "```"]
