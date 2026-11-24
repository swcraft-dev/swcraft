"""The shared emitters' escaping rule: an angle-bracketed placeholder survives to the page.

``swcraft_tools.guide_reference.render`` leaves a source string's Markdown alone, except ``<``:
a description saying ``ERROR: <msg>`` reached the built site as a tag and rendered as nothing,
while the committed Markdown still read correctly, so neither the drift gate nor the content
oracles saw it. The escape cannot be blanket: inside a backtick code span the characters are
already literal and an entity would be shown as an entity. So
:func:`~swcraft_tools.guide_reference.render.escape_markup` escapes outside code spans and copies
spans through, and the cases below pin both halves.

Two populations, on purpose: unit cases over strings written here, and a browser-shaped oracle over
the committed pages (after code spans are removed, nothing a browser reads as a start tag), which
fails whether or not a new emitter was routed through the escape.

**What this module does not reach.** It renders no Markdown or HTML, so it asserts tag-shaped
text is absent, not a particular DOM (``mkdocs build --strict`` reads the pages as a site); it says
nothing about ``&``, which the escape leaves alone; and it reads only the generated tree -- the
hand-written pages may carry deliberate HTML, so they are the guide surface gate's population.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from guide_fixtures import REFERENCE_DIR, committed_pages, generated_heading_problems
from swcraft_tools.guide_reference import all_artifacts
from swcraft_tools.guide_reference.render import (
    anchored_code,
    cell,
    escape_markup,
    heading,
    paragraph,
    purpose_comment,
)

#: The strings measured losing their placeholder in the built site, each with the page it
#: reaches; held as data so the assertion names the real defect.
MEASURED_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("reference/cli.md", "&lt;dist_root>/components/&lt;NAME>.arxml"),
)

#: A backtick code span, removed before the tag scan: inside one the browser sees no markup.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)

#: What a browser reads as a start tag. Deliberately narrow: ``a < b`` is not a tag, and the
#: escape does not claim to be an HTML sanitizer.
_TAG_SHAPED = re.compile(r"<[A-Za-z_]")


def _prose_outside_code_spans(text: str) -> str:
    return _CODE_SPAN.sub(" ", text)


def _committed_pages() -> dict[str, str]:
    return {
        relative: path.read_text(encoding="utf-8") for relative, path in committed_pages(REFERENCE_DIR, "*.md").items()
    }


class TestTheEscape:
    """:func:`escape_markup`, over strings written here."""

    def test_a_placeholder_in_prose_is_escaped(self) -> None:
        assert escape_markup("ERROR: <msg> if not found") == "ERROR: &lt;msg> if not found"

    def test_a_placeholder_inside_a_code_span_is_left_alone(self) -> None:
        assert escape_markup("stored under `swc/<group>/`") == "stored under `swc/<group>/`"

    def test_a_double_backtick_span_is_a_span_too(self) -> None:
        """The tool descriptions use a double-backtick span for text containing single backticks."""
        assert escape_markup("one ``<output_dir>/<name>.arxml`` each") == "one ``<output_dir>/<name>.arxml`` each"

    def test_prose_on_both_sides_of_a_span_is_escaped(self) -> None:
        assert escape_markup("<a> `<b>` <c>") == "&lt;a> `<b>` &lt;c>"

    def test_an_unterminated_backtick_opens_no_span(self) -> None:
        """CommonMark's rule, and the safe direction: the tail is escaped as prose rather than skipped."""
        assert escape_markup("a ` b <c>") == "a ` b &lt;c>"

    def test_a_closing_run_must_be_the_same_length(self) -> None:
        assert escape_markup("`a``b` <c>") == "`a``b` &lt;c>"

    def test_the_closing_greater_than_is_not_escaped(self) -> None:
        """``>`` opens nothing on its own, so escaping it would only make the source harder to read."""
        assert ">" in escape_markup("<msg>")

    def test_it_is_idempotent(self) -> None:
        """``&`` is not escaped, so running the escape twice is the same as running it once."""
        once = escape_markup("ERROR: <msg>")
        assert escape_markup(once) == once

    def test_text_with_nothing_to_escape_is_returned_unchanged(self) -> None:
        assert escape_markup("an ordinary sentence with `code` in it") == "an ordinary sentence with `code` in it"


class TestTheEmittersApplyIt:
    """The two emitters a source string actually passes through."""

    def test_a_paragraph_escapes(self) -> None:
        assert paragraph("Returns  <msg>\non failure") == "Returns &lt;msg> on failure"

    def test_a_cell_escapes_and_still_escapes_its_pipe(self) -> None:
        assert cell("<a> | <b>") == r"&lt;a> \| &lt;b>"

    def test_a_cell_leaves_a_code_span_alone(self) -> None:
        assert cell("`swc/<group>/`") == "`swc/<group>/`"


class TestTheAnchoredCode:
    """:func:`anchored_code`, the emitter that gives a code span an id through ``attr_list``."""

    def test_an_anchor_is_rendered_as_an_attribute_list_after_the_span(self) -> None:
        assert anchored_code("rule:swc.a-b_c", "swc.a-b_c") == "`rule:swc.a-b_c`{ #swc.a-b_c }"

    def test_a_cell_carries_it_unchanged(self) -> None:
        """The table emitter's escapes leave the attribute list alone: it holds no pipe and no ``<``."""
        assert cell(anchored_code("rule:x.y", "x.y")) == "`rule:x.y`{ #x.y }"

    @pytest.mark.parametrize("anchor", ("", "has space", "a}b", 'a"b', "a#b", "a<b"))
    def test_an_anchor_outside_the_pattern_is_refused(self, anchor: str) -> None:
        with pytest.raises(ValueError, match="is outside"):
            anchored_code("rule:x", anchor)


class TestTheCommittedPages:
    """The browser-shaped oracle, over the pages the release ships."""

    def test_the_scan_reads_a_non_empty_corpus(self) -> None:
        """An empty population would make every assertion below vacuously true."""
        pages = _committed_pages()
        assert len(pages) >= len(all_artifacts()), "the reference tree is smaller than the generator's output"

    @pytest.mark.parametrize("relative", sorted(_committed_pages()))
    def test_no_generated_page_carries_tag_shaped_text(self, relative: str) -> None:
        prose = _prose_outside_code_spans(_committed_pages()[relative])
        hits = sorted({match.group() for match in _TAG_SHAPED.finditer(prose)})
        assert not hits, (
            f"docs/guide/{relative} carries text a browser reads as an HTML tag: {hits} — "
            "route the source string through `render.escape_markup` and rerun "
            "`python scripts/dev.py docs-reference`"
        )

    @pytest.mark.parametrize(("relative", "placeholder"), MEASURED_PLACEHOLDERS)
    def test_the_measured_placeholder_survives_to_the_page(self, relative: str, placeholder: str) -> None:
        """The regression itself: the escaped spelling is on the page, and the raw one nowhere on it."""
        page = _committed_pages()[relative]
        assert placeholder in page, f"docs/guide/{relative} no longer carries the placeholder {placeholder!r}"
        raw = placeholder.replace("&lt;", "<")
        assert raw not in page, f"docs/guide/{relative} still carries the raw {raw!r}, which renders as nothing"

    def test_a_code_span_placeholder_is_still_unescaped_somewhere(self) -> None:
        """The other direction: a blanket escape would have put ``&lt;`` inside these spans."""
        spanned = {
            relative: sorted({match.group() for match in _CODE_SPAN.finditer(text) if "<" in match.group()})
            for relative, text in _committed_pages().items()
        }
        with_spans = {relative: spans for relative, spans in spanned.items() if spans}
        assert with_spans, "no generated page carries an angle bracket inside a code span any more"
        for spans in with_spans.values():
            assert all("&lt;" not in span for span in spans)


class TestTheGateFires:
    """The page scan, shown finding the defect it exists for."""

    def test_a_page_that_regains_a_raw_placeholder_is_found(self, tmp_path: Path) -> None:
        page = tmp_path / "tools.md"
        page.write_text('or "ERROR: <msg>" if not found\n', encoding="utf-8")
        prose = _prose_outside_code_spans(page.read_text(encoding="utf-8"))
        assert [match.group() for match in _TAG_SHAPED.finditer(prose)] == ["<m"]

    def test_the_same_text_escaped_is_not_found(self, tmp_path: Path) -> None:
        page = tmp_path / "tools.md"
        page.write_text('or "ERROR: &lt;msg>" if not found\n', encoding="utf-8")
        prose = _prose_outside_code_spans(page.read_text(encoding="utf-8"))
        assert not list(_TAG_SHAPED.finditer(prose))

    def test_the_span_stripper_really_removes_a_span(self) -> None:
        """Without this the tag scan would pass for the wrong reason on every page."""
        assert not _TAG_SHAPED.search(_prose_outside_code_spans("`<group>`"))
        assert _TAG_SHAPED.search(_prose_outside_code_spans("<group>"))


# Anchors and purposes: the corpus names a section ``<page>#<anchor>`` and shows its purpose in a
# search hit, so every generated ``##``/``###`` carries an explicit anchor equal to the id the site
# mints from its words (no URL moves), and every heading of levels 1-3 a purpose comment.


class TestTheHeadingEmitter:
    """:func:`heading`, with its anchor and its purpose."""

    def test_a_cut_level_heading_renders_its_anchor_and_its_purpose(self) -> None:
        assert heading(2, "`swc`", anchor="swc", purpose="Writing an SWC") == (
            "## `swc` { #swc }\n\n<!-- purpose: Writing an SWC -->"
        )

    def test_a_page_heading_needs_no_anchor(self) -> None:
        assert heading(1, "Units", purpose="Units") == "# Units\n\n<!-- purpose: Units -->"

    @pytest.mark.parametrize("level", (2, 3))
    def test_a_cut_level_heading_without_an_anchor_is_refused(self, level: int) -> None:
        with pytest.raises(ValueError, match="needs an explicit anchor"):
            heading(level, "Title", purpose="When it helps")

    @pytest.mark.parametrize("anchor", ("", "has space", "a}b", "a#b", "a<b"))
    def test_an_anchor_outside_the_pattern_is_refused(self, anchor: str) -> None:
        with pytest.raises(ValueError, match="is outside"):
            heading(2, "Title", anchor=anchor, purpose="When it helps")

    @pytest.mark.parametrize("level", (0, 7))
    def test_a_level_outside_markdowns_is_refused(self, level: int) -> None:
        with pytest.raises(ValueError, match="outside Markdown's 1-6"):
            heading(level, "Title", anchor="title", purpose="When it helps")

    def test_a_purpose_comment_is_one_line(self) -> None:
        assert purpose_comment("When it helps") == "<!-- purpose: When it helps -->"

    @pytest.mark.parametrize("text", ("", " padded", "padded ", "two\nlines", "ends --> early", "a\rb"))
    def test_a_purpose_that_is_not_one_clean_line_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="is not one non-empty line"):
            purpose_comment(text)


class TestTheGeneratedHeadings:
    """Every page the ``packages/tools/`` generators render, read as the site reads it."""

    def test_the_population_is_every_generated_page(self) -> None:
        assert len(all_artifacts()) >= 20, "the generators render fewer pages than the reference holds"

    @pytest.mark.parametrize("relative", sorted(all_artifacts()))
    def test_every_heading_has_its_anchor_unchanged_and_a_purpose(self, relative: str) -> None:
        problems = generated_heading_problems(all_artifacts()[relative])
        assert problems == [], f"{relative}: {problems}"

    def test_every_cut_level_heading_of_the_reference_carries_an_explicit_anchor(self) -> None:
        """Counted, so the rule above is seen to read headings at all."""
        cut = [
            line
            for text in all_artifacts().values()
            for line in text.splitlines()
            if re.match(r"^#{2,3} ", line) and re.search(r" \{ #[^ }]+ \}$", line)
        ]
        assert len(cut) > 50, len(cut)


class TestTheHeadingGateFires:
    """:func:`generated_heading_problems`, shown finding each defect."""

    GOOD = "# Page\n\n<!-- purpose: The page -->\n\n## `swc` { #swc }\n\n<!-- purpose: The kind -->\n\nText.\n"

    def test_a_good_page_has_no_problem(self) -> None:
        assert generated_heading_problems(self.GOOD) == []

    def test_a_heading_without_an_anchor_is_found(self) -> None:
        problems = generated_heading_problems(self.GOOD.replace(" { #swc }", ""))
        assert len(problems) == 1 and "has no explicit anchor" in problems[0]

    def test_an_anchor_that_moves_the_id_is_found(self) -> None:
        problems = generated_heading_problems(self.GOOD.replace("{ #swc }", "{ #the-swc }"))
        assert len(problems) == 1 and "change the page's ids" in problems[0]

    def test_a_heading_without_a_purpose_is_found(self) -> None:
        problems = generated_heading_problems(self.GOOD.replace("<!-- purpose: The kind -->\n\n", ""))
        assert len(problems) == 1 and "has no purpose comment" in problems[0]

    def test_a_purpose_over_the_cap_is_found(self) -> None:
        problems = generated_heading_problems(self.GOOD.replace("The kind", "x" * 101))
        assert len(problems) == 1 and "over 100" in problems[0]
