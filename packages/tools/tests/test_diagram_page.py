"""The viewer pages and the report: ``writer/diagrams/page.py`` and ``report.py``.

A page is one view's SVG **inlined** into an HTML document with the shared script, because a ``file:`` page cannot read
the DOM of an SVG it loads. ``index.html`` lists every view and carries the report as a section; ``report.txt`` carries
no timing, so it is byte-identical across runs. The inputs are the SVG goldens of ``fixtures/diagrams/svg`` and the
extractor's records of that model, so only the script's syntax check needs Node.

What these tests do not reach: whether the script **behaves** in a browser. They pin the markup, the link map, the
no-external-resource rule and the script's syntax; the highlight and the tooltip run in Node over a DOM stub, never a
browser, in ``test_diagram_page_script.py``; search, zoom, pan and click-through in a real browser are a reader's check.
The index and the pages are not frozen as goldens here; ``test_diagram_generation.py`` freezes the whole output.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.writer import diagrams as diagrams_package
from swcraft_tools.writer.diagrams import graph, page, report
from swcraft_tools.writer.diagrams import sequence as sequence_module
from swcraft_tools.writer.diagrams import svg as svg_module
from typing_extensions import get_protocol_members

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
SVG_DIR = FIXTURE_DIR / "svg"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"
PROJECT = "Diagram fixture"
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def extracted() -> graph.DiagramGraphs:
    return graph.build_graphs(str(FIXTURE_DIR / "model"))


@pytest.fixture(scope="module")
def views(extracted: graph.DiagramGraphs) -> list[tuple[str, str]]:
    return [(view, (SVG_DIR / f"{view}.svg").read_text(encoding="utf-8")) for view in graph.view_ids(extracted)]


@pytest.fixture(scope="module")
def view_ids(views: list[tuple[str, str]]) -> list[str]:
    return [view for view, _ in views]


def _svg(view: str) -> str:
    return (SVG_DIR / f"{view}.svg").read_text(encoding="utf-8")


#: Where each composition of the fixture is placed, as the generator hands it to a page.
PLACEMENTS = graph.build_graphs(str(FIXTURE_DIR / "model")).placements


def _page(view: str, view_ids: list[str], project: str = PROJECT) -> str:
    return page.render_view_page(project, view, _svg(view), view_ids, None, PLACEMENTS)


class _Collector(HTMLParser):
    """Every start tag with its attributes, and the text of every ``<script>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.scripts: list[tuple[dict[str, str], str]] = []
        self._script: dict[str, str] | None = None
        self._text: list[str] = []
        self.title = ""
        self._in_title = False
        self._depth_svg = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        self.tags.append((tag, attributes))
        if tag == "svg":
            self._depth_svg += 1
        if tag == "script":
            self._script, self._text = attributes, []
        if tag == "title" and self._depth_svg == 0:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg":
            self._depth_svg -= 1
        if tag == "script" and self._script is not None:
            self.scripts.append((self._script, "".join(self._text)))
            self._script = None
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._text.append(data)
        if self._in_title:
            self.title += data


def _parse(text: str) -> _Collector:
    collector = _Collector()
    collector.feed(text)
    collector.close()
    return collector


def _hrefs(text: str) -> list[str]:
    return [attributes["href"] for tag, attributes in _parse(text).tags if tag == "a" and "href" in attributes]


def _links_map(text: str) -> dict[str, str]:
    scripts = [body for attributes, body in _parse(text).scripts if attributes.get("id") == page.LINKS_SCRIPT_ID]
    assert len(scripts) == 1
    return json.loads(scripts[0])


# One page per view


def test_every_view_page_inlines_its_svg_byte_for_byte_once(views: list[tuple[str, str]], view_ids: list[str]) -> None:
    for view, svg_text in views:
        text = page.render_view_page(PROJECT, view, svg_text, view_ids)
        assert text.count(svg_text) == 1, view
        assert text.count("<svg") == 1, view
        assert text.startswith("<!DOCTYPE html>\n"), view
        assert text.endswith("</html>\n"), view


def test_two_renders_of_every_page_the_index_and_the_report_are_byte_identical(
    extracted: graph.DiagramGraphs, views: list[tuple[str, str]], view_ids: list[str]
) -> None:
    for view, svg_text in views:
        assert page.render_view_page(PROJECT, view, svg_text, view_ids) == page.render_view_page(
            PROJECT, view, svg_text, list(view_ids)
        )
    first = report.render_report(PROJECT, views, extracted.not_drawn)
    assert first == report.render_report(PROJECT, list(views), list(extracted.not_drawn))
    assert page.render_index(PROJECT, views, first) == page.render_index(PROJECT, list(views), first)


def test_every_page_carries_the_one_shared_script_and_style(views: list[tuple[str, str]], view_ids: list[str]) -> None:
    for view, svg_text in views:
        parsed = _parse(page.render_view_page(PROJECT, view, svg_text, view_ids))
        code = [body for attributes, body in parsed.scripts if not attributes]
        assert code == [page.PAGE_SCRIPT], view
        text = page.render_view_page(PROJECT, view, svg_text, view_ids)
        assert text.count(f"<style>\n{page.PAGE_STYLE}</style>") == 1, view


def test_the_page_names_the_view_and_the_project(view_ids: list[str]) -> None:
    parsed = _parse(_page("ibd-Chassis", view_ids))
    assert parsed.title == f"Chassis — internal block diagram — {PROJECT}"
    body = next(attributes for tag, attributes in parsed.tags if tag == "body")
    assert body == {"data-view": "ibd-Chassis"}
    assert page.view_heading(_svg("sequence-speed_cycle")) == "speed_cycle — sequence diagram"
    assert page.view_heading(_svg("nesting")) == "Vehicle — nesting"


def test_the_page_has_search_zoom_and_reload_controls(view_ids: list[str]) -> None:
    parsed = _parse(_page("ibd-Vehicle", view_ids))
    ids = {attributes.get("id"): (tag, attributes) for tag, attributes in parsed.tags if "id" in attributes}
    assert ids["search"][0] == "input" and ids["search"][1]["type"] == "search"
    for control in ("search-previous", "search-next", "zoom-in", "zoom-out", "zoom-fit", "reload"):
        assert ids[control][0] == "button", control
        assert ids[control][1]["type"] == "button", control
    assert ids["search-count"][0] == "output"
    assert ids["canvas"][0] == "main"


def test_the_script_implements_search_over_titles_pan_zoom_reload_and_click_through() -> None:
    script = page.PAGE_SCRIPT
    # The search reads the page's own titles and marks hits; the current hit is panned to.
    assert 'querySelectorAll("title")' in script
    assert '"hit"' in script and '"current"' in script
    assert "getBBox()" in script
    # Pan and zoom rewrite the inlined SVG's viewBox; the wheel zooms, a drag pans.
    assert 'setAttribute("viewBox"' in script
    assert '"wheel"' in script and '"pointerdown"' in script and '"pointermove"' in script
    # Reload, and click-through from the link map.
    assert "location.reload()" in script
    assert page.LINKS_SCRIPT_ID in script
    assert "location.href" in script
    assert "[data-kind" in script and "data-type" in script
    # The view's own frame is never a link: the nesting view's root frame fills the picture.
    assert """[data-type]:not([data-role="frame"])""" in script
    assert script.count("LINKABLE") == 3, "one selector for the press and the link marking"


def test_the_count_square_takes_pointer_events_while_every_other_label_stays_inert() -> None:
    """The labels layer stays click-through; the count square alone can be rested on."""
    style = page.PAGE_STYLE
    assert "main > svg > g.labels { pointer-events: none; }" in style
    assert "main > svg > g.labels > g.edge-count { pointer-events: auto; }" in style
    assert "main > svg > g.labels > g.frame-up { pointer-events: auto; cursor: pointer; }" in style


def _search(page_text: str, query: str) -> list[ET.Element]:
    """The elements the page's search marks for *query*, by the script's own rule.

    The script walks every ``<title>`` of the inlined SVG but the document's own and marks the title's parent when its
    text, or the ``<desc>`` list a count square or a capped port carries, contains the query case-insensitively.
    """
    root = ET.fromstring(re.search(r"<svg\b.*?</svg>", page_text, re.DOTALL).group(0))  # type: ignore[union-attr]
    parents = {child: parent for parent in root.iter() for child in parent}
    marked = []
    for title in root.iter(f"{SVG_NS}title"):
        element = parents[title]
        if element is root:
            continue
        listing = element.find(f"{SVG_NS}desc")
        text = (title.text or "") + "\n" + ((listing.text or "") if listing is not None else "")
        if query.lower() in text.lower():
            marked.append(element)
    return marked


def test_the_search_finds_one_connector_of_a_bundle_through_its_count_square(view_ids: list[str]) -> None:
    """The count square is findable by a connector's name: the search reads its ``<desc>`` beside the title.

    The bundle's names sit in that ``<desc>``, in neither ``<title>`` element, so the line itself is no hit.
    """
    page_text = _page("overview-Chassis", view_ids)
    hits = _search(page_text, "RAW_link")
    marked = {(element.get("class"), element.get("data-name")) for element in hits}
    assert marked == {("edge-count", "Sensor -> Controller")}
    assert _search(page_text, "Sensor -> Controller"), "the head line is still searched"
    script = page.PAGE_SCRIPT
    assert 'querySelectorAll("title")' in script and "title.parentNode !== svg" in script
    assert 'querySelector("desc.connector-list, desc.member-list")' in script


# A line lights up with its two ends, and a click keeps it


def _rules(style: str) -> list[tuple[str, str]]:
    """Every rule of *style* as ``(selector list, declarations)``, whitespace collapsed."""
    return [
        (" ".join(selectors.split()), " ".join(body.split()))
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style)
    ]


def test_the_style_lights_a_line_and_its_ends_by_one_class() -> None:
    """A ``highlight`` rule for a line and for a port, a box and a lifeline, and nothing else new.

    A line's width is set on its **group**, never on the path: the hit area's clone would take a width set on the path
    and shrink while lit. The colour is the page's link blue ``#1a5fb4``; the arrowhead a lit bundle points with is a
    hard attribute inside its ``<marker>``, so the rule swaps ``marker-end`` to the lit marker. A port's hit box is
    excluded from every highlight rule. What a browser draws of it is not measured: the rules are pinned as text.
    """
    style = page.PAGE_STYLE
    assert svg_module.HIGHLIGHT_STROKE == "#1a5fb4"
    assert "nav a { color: #1a5fb4; " in style, "the highlight is the page's link colour"
    assert "#000000" not in style, "the black of 2026-09-22 is gone"
    assert "#7048e8" not in style
    rules = dict(_rules(style))
    hit = svg_module.PORT_HIT_CLASS
    # Every rule that can reach a rect or a path excludes the hit band too.
    band = page.HIT_BAND_CLASS
    lit = rules[
        f"svg .highlight rect:not(.{hit}):not(.{band}), svg .highlight path:not(.{band}), svg .highlight circle, "
        "svg .highlight line"
    ]
    assert lit == f"stroke: {svg_module.HIGHLIGHT_STROKE};"
    port = rules[
        f"svg g.port.highlight rect:not(.{hit}):not(.{band}), svg g.port.highlight path:not(.{band}), "
        "svg g.port.highlight circle, svg g.port.highlight line"
    ]
    assert port == "stroke-width: 2px;", "the square and the glyph equally thick"
    assert rules[f"svg g.member.highlight rect:not(.{band}), svg g.lifeline.highlight rect:not(.{band})"] == (
        "stroke-width: 2px;"
    )
    assert rules["svg g.edge.highlight"] == "stroke-width: 2.5px;", "a lit line is thicker"
    assert (
        rules["nav .placed-in.highlight"] == f"outline: 2px solid {svg_module.HIGHLIGHT_STROKE}; outline-offset: 2px;"
    )
    assert rules[f"svg g.edge.highlight path:not(.{band})"] == f"marker-end: url(#{svg_module.LIT_ARROW_MARKER_ID});"
    assert "context-stroke" not in style
    assert rules[f"svg g.message.highlight path:not(.{band})"] == (
        f"stroke-width: 2.5px; marker-end: url(#{sequence_module.LIT_DATA_MARKER_ID});"
    ), "a lit data message, the self-message included, points with the open twin"
    assert rules[f"svg g.message.activation.highlight path:not(.{band})"] == (
        f"marker-end: url(#{svg_module.LIT_ARROW_MARKER_ID});"
    ), "a lit activation points with the filled twin"
    assert not re.search(r"\.highlight[^{]*> path[^{]*\{[^}]*stroke-width", style)
    # Every highlight selector that can reach a port's rects excludes the hit box.
    selectors = [selector for group, _ in _rules(style) for selector in group.split(", ")]
    for reaching in ("svg .highlight rect", "svg g.port.highlight rect"):
        assert reaching not in selectors, f"{reaching} would draw a port's hit box"
    # A lit dashed line stays dashed: no highlight rule names the dash.
    for selectors, body in _rules(style):
        if ".highlight" in selectors:
            assert "stroke-dasharray" not in body, selectors
    # Search keeps its own colours.
    assert rules[f"svg .hit rect:not(.{band}), svg .hit path:not(.{band}), svg .hit circle, svg .hit line"] == (
        "stroke: #e8590c; stroke-width: 2.5px;"
    )
    assert rules[
        f"svg .current rect:not(.{band}), svg .current path:not(.{band}), svg .current circle, svg .current line"
    ] == ("stroke: #c92a2a; stroke-width: 4px;")


def test_every_marker_a_style_rule_swaps_to_is_emitted_by_every_view_whose_lines_point(
    views: list[tuple[str, str]],
) -> None:
    """A ``marker-end`` the style names must exist in every SVG whose pointing lines it reaches.

    What it does not reach: an IBD's lines hold the ``edge`` class and point with no head, so the edge rule names a
    marker an IBD does not define and a lit IBD line stays headless; and what a browser draws.
    """
    swaps = [
        (selector, marker)
        for group, body in _rules(page.PAGE_STYLE)
        for marker in re.findall(r"marker-end: url\(#([^)]+)\)", body)
        for selector in group.split(", ")
    ]
    assert {marker for _, marker in swaps} == {svg_module.LIT_ARROW_MARKER_ID, sequence_module.LIT_DATA_MARKER_ID}
    reached = 0
    for selector, marker in swaps:
        (kind,) = re.findall(r"g\.(edge|message)\b", selector)
        for view, text in views:
            if re.search(rf'<g class="{kind}[ "]', text) and 'marker-end="' in text:
                assert f'<marker id="{marker}" ' in text, (view, selector)
                reached += 1
    assert reached, "no view holds a class a marker rule names"


def test_a_block_diagram_defines_no_open_lit_twin(views: list[tuple[str, str]]) -> None:
    """The open twin is the sequence renderer's alone; a block diagram's ``<defs>`` lacks it."""
    blocks = [(view, text) for view, text in views if not view.startswith("sequence-")]
    assert any(view.startswith("ibd-") for view, _ in blocks)
    for view, text in blocks:
        assert sequence_module.LIT_DATA_MARKER_ID not in text, view
        assert "g.message" not in text and '<g class="message' not in text, view


def test_the_page_carries_the_connector_panel_and_opens_it_from_the_count_square(view_ids: list[str]) -> None:
    """The connector list is the page's panel, not the browser's tooltip.

    The markup and the script's rule are pinned here; **what a browser does with them is not tested**.
    """
    parsed = _parse(_page("overview-Chassis", view_ids))
    ids = [attributes.get("id") for _, attributes in parsed.tags if attributes.get("id")]
    assert ["connectors", "connectors-head", "connectors-close", "connectors-list"] == [
        value for value in ids if str(value).startswith("connectors")
    ]
    aside = next(attributes for tag, attributes in parsed.tags if tag == "aside")
    assert aside == {"id": "connectors", "hidden": ""}
    names = [tag for tag, _ in parsed.tags]
    assert names.index("main") < names.index("aside") < names.index("footer")
    for rule in ("#connectors {", "#connectors[hidden] { display: none; }", "#connectors ol {"):
        assert rule in page.PAGE_STYLE, rule
    assert "overflow-y: auto" in page.PAGE_STYLE, "a long list scrolls"
    # Opened by the click that pins the line or a capped port, closed by a second click on the same owner, by a click on
    # empty canvas and by the panel's own button: that behaviour runs under Node in ``test_diagram_page_script.py``.
    assert 'getElementById("connectors-close")' in page.PAGE_SCRIPT


def test_a_panel_row_is_a_whole_connector_with_its_description_under_it() -> None:
    """The script splits a row: the connector's line as the ``<li>``'s text, the part after the tab a ``span.note``.

    **What a browser does with them is not tested**: the markup, the rule and the script are pinned here.
    """
    script = page.PAGE_SCRIPT
    assert "item.textContent = parts[0];" in script
    assert 'note.className = "note";' in script
    assert 'note.textContent = parts.slice(1).join(" ");' in script
    assert "#connectors li .note { display: block;" in page.PAGE_STYLE
    # A row carries whole names, and an interface's can be a path with no space in it.
    assert "overflow-wrap: anywhere" in page.PAGE_STYLE


def test_the_page_has_a_status_line(view_ids: list[str]) -> None:
    parsed = _parse(_page("ibd-Chassis", view_ids))
    tags = [(tag, attributes) for tag, attributes in parsed.tags if attributes.get("id") == "status"]
    assert tags == [("footer", {"id": "status", "role": "status"})]
    names = [tag for tag, _ in parsed.tags]
    assert names.index("main") < names.index("footer"), "the status line runs under the picture"
    assert "footer {" in page.PAGE_STYLE


def test_every_page_carries_one_hidden_tooltip_beside_the_status_line(view_ids: list[str]) -> None:
    """One ``#tip``, outside the picture, hidden until a pointer rests on something titled.

    It takes no pointer itself (or it would steal the hover it shows), keeps a title's line breaks and is bounded by the
    window, cutting what does not fit. Where a browser puts it, and where it cuts, is not measured.
    """
    for view in view_ids:
        parsed = _parse(_page(view, view_ids))
        tips = [(tag, attributes) for tag, attributes in parsed.tags if attributes.get("id") == "tip"]
        assert tips == [("div", {"id": "tip", "role": "tooltip", "hidden": ""})], view
        ids = [attributes.get("id") for _, attributes in parsed.tags]
        assert ids.index("status") < ids.index("tip"), view
    rules = dict(_rules(page.PAGE_STYLE))
    tip = rules["#tip"]
    for declaration in ("position: fixed;", "background: white;", "pointer-events: none;", "white-space: pre-line;"):
        assert declaration in tip, declaration
    assert "border: 1px solid" in tip
    for declaration in (
        "max-width: min(40em, calc(100vw - 8px));",
        "max-height: calc(100vh - 8px);",
        "overflow: hidden;",
        "overflow-wrap: anywhere;",
    ):
        assert declaration in tip, declaration
    assert rules["#tip[hidden]"] == "display: none;"


#: Every ``addEventListener`` call of the script, with its receiver and its event.
_LISTENER = re.compile(r"([A-Za-z_.]+(?:\(\"[a-z-]+\"\))?)\.addEventListener\(\s*\"([a-z]+)\"")


def _function(script: str, name: str) -> str:
    """The text of the script's ``function <name>(...) { ... }``, to its closing brace."""
    start = script.index(f"  function {name}(")
    return script[start : script.index("\n  }\n", start) + 4]


def test_the_highlight_is_one_delegated_listener_on_the_svg() -> None:
    """The highlight is one delegated listener on the ``<svg>``, never a listener per element.

    The listeners are one ``pointerover`` on the ``<svg>``, the existing press, and the tooltip's ``pointerout``. On a
    pointer move neither the highlight nor the tooltip queries the DOM: the ends are looked up in indexes built once,
    the tooltip's text is an ancestor walk and a cache lookup.
    """
    script = page.PAGE_SCRIPT
    calls = _LISTENER.findall(script)
    assert len(calls) == script.count("addEventListener("), "every registration is read"
    on_svg = sorted(event for receiver, event in calls if receiver == "svg")
    assert on_svg == sorted(
        ["wheel", "pointerdown", "pointermove", "pointerup", "pointercancel", "pointerover", "pointerout"]
    )
    receivers = {receiver for receiver, _ in calls}
    assert all(
        # The selection bar's two buttons by the variables holding them, every other control by its id.
        receiver in {"svg", "input", "document", "openButton", "nextButton"}
        or receiver.startswith("document.getElementById(")
        for receiver in receivers - {"window"}
    ), receivers
    # The one listener on the window, and only with a host: the blur holds the tooltip hidden. A reveal is a selection
    # and gives the search box no focus.
    assert [event for receiver, event in calls if receiver == "window"] == ["blur"]
    window_listeners = 'if (host) {\n    window.addEventListener("blur", holdTip);\n'
    assert script.index(window_listeners) > 0
    hover = script[script.index('"pointerover"') :]
    hover = hover[: hover.index("});")]
    assert "showTip(event);" in hover
    move = script[script.index('"pointermove"') :]
    move = move[: move.index("});")]
    assert "showTip(event);" in move
    assert 'svg.addEventListener("pointerout", hideTip);' in script
    for body in (hover, move, _function(script, "showTip"), _function(script, "tipText"), _function(script, "hideTip")):
        assert "querySelector" not in body
        assert "getElementById" not in body
    assert "titles.has(element)" in _function(script, "tipText")
    assert "element.parentNode" in _function(script, "tipText")


def test_the_highlight_script_reads_the_ends_and_pins_on_a_click() -> None:
    script = page.PAGE_SCRIPT
    assert '"highlight"' in script
    assert '"data-source"' in script and '"data-target"' in script
    assert 'getElementById("status")' in script
    assert "statusLine.textContent" in script
    assert "pinned" in script
    # The status text is the lit element's own <title>, read from the cache the removal fills: one source of text.
    title_of = _function(script, "titleOf")
    assert "titles.get(element)" in title_of
    assert "querySelector" not in title_of
    for word in ("fetch(", "XMLHttpRequest", "import(", "http:", "https:", "//www", "Worker("):
        assert word not in script, word


def test_any_port_is_a_subject_connected_or_not() -> None:
    """The pointer on **any** end lights it; one without a line lights alone."""
    subject = _function(page.PAGE_SCRIPT, "subjectOf")
    assert "return end || null;" in subject
    assert "lines[endKey(end)]" not in subject


def test_the_titles_are_cached_and_removed_after_the_search_has_read_them() -> None:
    """No double tooltip: every ``<title>`` of the inlined SVG is cached and removed after the search has read them.

    The view's own heading goes too, or it would become the browser's tooltip over the whole picture. The standalone
    ``.svg`` keeps every title.
    """
    script = page.PAGE_SCRIPT
    built = script.index("var titled = ")
    removal = script.index("removeChild(title)")
    assert built < removal
    assert "titles.set(" in script[built:removal]
    assert "var titles = new Map();" in script
    assert script.index("var titles = new Map();") < built
    # What the removal leaves is the Node behaviour suite's (test_diagram_page_script.py).


def test_the_tooltip_reads_the_nearest_titled_ancestor_and_hides_on_a_press_and_escape() -> None:
    script = page.PAGE_SCRIPT
    tip_text = _function(script, "tipText")
    assert "parentNode" in tip_text and "titles.has(" in tip_text, "an ancestor walk over the cache"
    show = _function(script, "showTip")
    assert "tipText(event.target)" in show
    assert "subjectOf" not in show, "the tooltip is independent of the highlight's subject"
    press = script[script.index('"pointerdown"') :]
    assert "hideTip()" in press[: press.index("});")]
    keys = script[script.index('document.addEventListener("keydown"') :]
    assert "hideTip()" in keys[: keys.index("});")]
    assert 'getElementById("tip")' in script
    # Where it shows and when it hides are the Node behaviour suite's (test_diagram_page_script.py).


def _lit(page_text: str, subject: tuple[str, str, str | None]) -> set[tuple[str | None, str | None, str | None]]:
    """What the script lights when *subject* (role, name, owner) is pointed at, by its own rule.

    A line lights itself and its two ends; an end (keyed by its ``id``, or by its name on a lifeline) lights itself,
    every line naming it, and the ends at their far side.
    """
    root = ET.fromstring(re.search(r"<svg\b.*?</svg>", page_text, re.DOTALL).group(0))  # type: ignore[union-attr]
    elements = [element for element in root.iter(f"{SVG_NS}g") if element.get("data-role")]
    ends = {}
    for element in elements:
        if element.get("data-role") == "lifeline":
            ends[element.get("data-name")] = element
        elif element.get("data-role") in {"port", "member"} and element.get("id"):
            ends[element.get("id")] = element
    lines = [element for element in elements if element.get("data-source") is not None]
    role, name, owner = subject
    target = next(
        element
        for element in elements
        if element.get("data-role") == role
        and element.get("data-name") == name
        and (owner is None or element.get("data-owner") == owner)
    )
    if target.get("data-source") is not None:
        joined = [target]
    else:
        key = next(key for key, element in ends.items() if element is target)
        joined = [line for line in lines if key in (line.get("data-source"), line.get("data-target"))]
    lit = {target}
    for line in joined:
        lit.add(line)
        lit.update(ends[line.get(end)] for end in ("data-source", "data-target"))
    return {(element.get("data-role"), element.get("data-name"), element.get("data-owner")) for element in lit}


def test_a_connector_lights_its_two_ports(view_ids: list[str]) -> None:
    lit = _lit(_page("ibd-Chassis", view_ids), ("connector", "raw_link", None))
    assert lit == {("connector", "raw_link", None), ("port", "pRaw", "Sensor"), ("port", "rRaw", "Controller")}


def test_a_port_lights_every_line_it_has_and_the_far_ports(view_ids: list[str]) -> None:
    lit = _lit(_page("ibd-Vehicle", view_ids), ("port", "rDiag", "Chassis"))
    assert lit == {("port", "rDiag", "Chassis"), ("connector", "diag_link", None), ("port", "pDiag", "Diag")}


def test_an_overview_pair_lights_its_two_boxes(view_ids: list[str]) -> None:
    lit = _lit(_page("overview-Chassis", view_ids), ("bundle", "Sensor -> Controller", None))
    assert lit == {("bundle", "Sensor -> Controller", None), ("member", "Sensor", None), ("member", "Controller", None)}


def test_a_sequence_step_lights_its_task_and_its_runnables_component(view_ids: list[str]) -> None:
    lit = _lit(_page("sequence-speed_cycle", view_ids), ("activation", "SensorRun", None))
    assert lit == {("activation", "SensorRun", None), ("lifeline", "Task_10ms", None), ("lifeline", "Sensor", None)}


def test_an_ibd_page_links_the_index_its_overview_the_nesting_and_its_member_compositions(
    view_ids: list[str],
) -> None:
    text = _page("ibd-Vehicle", view_ids)
    assert _hrefs(text) == [
        "index.html",
        "overview-Vehicle.html",
        "nesting.html",
        "ibd-Vehicle.svg",
        "ibd-Chassis.html",
    ]
    assert _links_map(text) == {"Chassis": "ibd-Chassis.html"}


def test_an_overview_page_descends_into_the_member_compositions_overview(view_ids: list[str]) -> None:
    text = _page("overview-Vehicle", view_ids)
    assert _hrefs(text) == [
        "index.html",
        "ibd-Vehicle.html",
        "nesting.html",
        "overview-Vehicle.svg",
        "overview-Chassis.html",
    ]
    assert _links_map(text) == {"Chassis": "overview-Chassis.html"}


def test_a_composition_without_member_compositions_links_only_its_neighbours(view_ids: list[str]) -> None:
    text = _page("ibd-Chassis", view_ids)
    assert _hrefs(text) == [
        "index.html",
        "overview-Chassis.html",
        "nesting.html",
        "ibd-Chassis.svg",
        "ibd-Vehicle.html#Chassis",
    ]
    assert _links_map(text) == {}


def test_the_nesting_page_links_every_composition_box_to_its_ibd_in_preorder(view_ids: list[str]) -> None:
    text = _page("nesting", view_ids)
    assert _hrefs(text) == ["index.html", "nesting.svg", "ibd-Vehicle.html", "ibd-Chassis.html"]
    assert _links_map(text) == {"Chassis": "ibd-Chassis.html", "Vehicle": "ibd-Vehicle.html"}


def test_a_sequence_page_links_the_index_and_the_nesting(view_ids: list[str]) -> None:
    text = _page("sequence-speed_cycle", view_ids)
    assert _hrefs(text) == ["index.html", "nesting.html", "sequence-speed_cycle.svg"]
    assert _links_map(text) == {}


def test_a_click_through_target_without_a_page_is_not_linked(view_ids: list[str]) -> None:
    without_chassis = [view for view in view_ids if not view.endswith("-Chassis")]
    without_nesting = [view for view in without_chassis if view != "nesting"]
    text = page.render_view_page(PROJECT, "ibd-Vehicle", _svg("ibd-Vehicle"), without_nesting)
    assert _hrefs(text) == ["index.html", "overview-Vehicle.html", "ibd-Vehicle.svg"]
    assert _links_map(text) == {}


def test_the_link_map_and_the_names_are_escaped() -> None:
    hostile = 'A</script x><b x="1">&'
    svg_text = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10" data-view="ibd-V">\n'
        "<title>V &lt;one&gt; — internal block diagram</title>\n"
        '<g class="member composition" data-role="member" data-name="m" data-type="'
        + hostile.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        + '" data-kind="composition"><title>m</title></g>\n</svg>\n'
    )
    view = f"ibd-{hostile}"
    text = page.render_view_page('P<&"', "ibd-V", svg_text, ["ibd-V", view])
    parsed = _parse(text)
    assert parsed.title == 'V <one> — internal block diagram — P<&"'
    assert _links_map(text) == {hostile: page.page_href(view)}
    assert text.count("</script") == 4, "the link map, the way up, the model block and the script: no name closes one"
    assert page.page_href(view) in _hrefs(text)
    assert all("<" not in href and '"' not in href for href in _hrefs(text))


# The frame's name is the way up, and the header carries the chain


def _up_map(text: str) -> dict[str, str]:
    scripts = [body for attributes, body in _parse(text).scripts if attributes.get("id") == page.UP_SCRIPT_ID]
    assert len(scripts) == 1
    return json.loads(scripts[0])


def _nav(text: str) -> str:
    return re.search(r"<nav>(.*?)</nav>", text, re.DOTALL).group(1)  # type: ignore[union-attr]


def _frame_svg(name: str, view: str) -> str:
    """The least SVG a page reads the way up from: a frame naming its composition."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10" data-view="{view}">\n'
        f"<title>{name} — internal block diagram</title>\n"
        f'<g class="frame"><g class="frame" data-role="frame" data-name="{name}"><title>composition {name}</title>'
        "</g></g>\n</svg>\n"
    )


def _views(*names: str) -> list[str]:
    return [f"{prefix}-{name}" for name in names for prefix in ("ibd", "overview")]


@pytest.mark.parametrize(
    ("view", "target"),
    [("ibd-Chassis", "ibd-Vehicle.html#Chassis"), ("overview-Chassis", "overview-Vehicle.html#Chassis")],
)
def test_one_parent_is_the_way_up_in_the_same_kind_of_view(view: str, target: str, view_ids: list[str]) -> None:
    text = _page(view, view_ids)
    assert _up_map(text) == {"Vehicle": target}
    assert '<span class="chain">' in _nav(text)


@pytest.mark.parametrize("view", ["ibd-Vehicle", "overview-Vehicle", "nesting", "sequence-speed_cycle"])
def test_the_root_and_a_view_that_is_no_composition_have_no_way_up(view: str, view_ids: list[str]) -> None:
    text = _page(view, view_ids)
    assert _up_map(text) == {}
    assert "chain" not in _nav(text) and "placed in" not in _nav(text)


def test_a_composition_placed_nowhere_has_no_way_up() -> None:
    text = page.render_view_page(PROJECT, "ibd-Loose", _frame_svg("Loose", "ibd-Loose"), _views("Loose"), None, {})
    assert _up_map(text) == {}
    assert _hrefs(text) == ["index.html", "overview-Loose.html", "ibd-Loose.svg"]


def test_several_parents_are_listed_and_none_is_chosen() -> None:
    placements = {"C": (("A", "c1"), ("B", "c 2"))}
    text = page.render_view_page(PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), _views("A", "B", "C"), None, placements)
    assert _up_map(text) == {"A": "ibd-A.html#c1", "B": "ibd-B.html#c%202"}
    nav = _nav(text)
    assert '<span class="placed-in" id="placed-in">placed in: <a href="ibd-A.html#c1">A</a>, ' in nav
    assert '<a href="ibd-B.html#c%202">B</a></span>' in nav
    assert "chain" not in nav
    # The script leads up only where one target stands; several mark the list.
    script = page.PAGE_SCRIPT
    assert "targets.length === 1" in script and 'getElementById("placed-in")' in script


def test_the_header_carries_the_chain_from_the_root_as_plain_anchors() -> None:
    placements = {"C": (("B", "c"),), "B": (("A", "b"),)}
    views = _views("A", "B", "C")
    text = page.render_view_page(PROJECT, "overview-C", _frame_svg("C", "overview-C"), views, None, placements)
    assert (
        '<span class="chain"><a href="overview-A.html#b">A</a> \u203a '
        '<a href="overview-B.html#c">B</a> \u203a <span>C</span></span>'
    ) in _nav(text)
    assert _up_map(text) == {"B": "overview-B.html#c"}


def test_a_level_placed_in_several_replaces_the_chain_by_the_views_own_parents() -> None:
    placements = {"C": (("B", "c"),), "B": (("A1", "b"), ("A2", "b"))}
    text = page.render_view_page(
        PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), _views("A1", "A2", "B", "C"), None, placements
    )
    nav = _nav(text)
    assert '<span class="placed-in" id="placed-in">placed in: <a href="ibd-B.html#c">B</a></span>' in nav
    assert "chain" not in nav
    assert _up_map(text) == {"B": "ibd-B.html#c"}


def test_a_placement_cycle_is_not_walked_forever() -> None:
    placements = {"C": (("B", "c"),), "B": (("C", "b"),)}
    text = page.render_view_page(PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), _views("B", "C"), None, placements)
    assert "placed in: " in _nav(text)


def test_a_parent_without_a_page_is_not_linked() -> None:
    placements = {"C": (("B", "c"),)}
    text = page.render_view_page(PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), _views("C"), None, placements)
    assert _up_map(text) == {}
    assert '<span class="chain"><span>B</span> \u203a <span>C</span></span>' in _nav(text)


def test_the_way_up_takes_the_parents_stem() -> None:
    placements = {"C": (("chassis", "c"),)}
    views = [*_views("C"), "ibd-chassis~1"]
    text = page.render_view_page(
        PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), views, {"chassis": "chassis~1"}, placements
    )
    assert _up_map(text) == {"chassis": "ibd-chassis~1.html#c"}


def test_the_fragment_names_a_member_the_parents_page_marks(view_ids: list[str]) -> None:
    """The link's fragment is a member's ``data-name`` on the parent's page; the script lights, pins and pans to it."""
    for view, parent_view in (("ibd-Chassis", "ibd-Vehicle"), ("overview-Chassis", "overview-Vehicle")):
        href = _up_map(_page(view, view_ids))["Vehicle"]
        target, fragment = href.split("#")
        assert target == f"{parent_view}.html"
        members = [
            element
            for element in ET.fromstring(_svg(parent_view)).iter(f"{SVG_NS}g")
            if element.get("data-role") == "member" and element.get("data-name") == fragment
        ]
        assert len(members) == 1, view
    script = page.PAGE_SCRIPT
    arrival = script[script.index("window.location.hash") :]
    assert "decodeURIComponent" in script
    assert """querySelectorAll('[data-role="member"]')""" in arrival
    assert "pin(element);" in arrival and "reveal(element);" in arrival
    # Pin lights through the one ``highlight`` class; a member with no line of its own is lit alone.
    assert "lines[endKey(subject)] || []" in script


def test_the_way_up_escapes_the_names_it_carries() -> None:
    hostile = 'P</script x><b x="1">&'
    placements = {"C": ((hostile, 'm</script>"'),)}
    views = [*_views("C"), f"ibd-{hostile}"]
    text = page.render_view_page(PROJECT, "ibd-C", _frame_svg("C", "ibd-C"), views, None, placements)
    assert _up_map(text) == {hostile: f"{page.page_href(f'ibd-{hostile}')}#m%3C%2Fscript%3E%22"}
    assert text.count("</script") == 4
    assert all("<" not in href and '"' not in href for href in _hrefs(text))


def test_the_frame_name_is_pressed_like_a_box_with_no_new_listener() -> None:
    script = page.PAGE_SCRIPT
    assert 'closest("g.frame-up")' in script
    assert page.UP_SCRIPT_ID in script
    style = page.PAGE_STYLE
    assert "svg g.frame-up text { fill: #1a5fb4; }" in style


# No external resource


_NETWORK_WORDS = ("fetch(", "XMLHttpRequest", "import(", "Worker(", "WebSocket", "EventSource", "sendBeacon", "@import")


def _external_resources(text: str) -> list[str]:
    parsed = _parse(text)
    offenders = []
    for tag, attributes in parsed.tags:
        if tag in {"link", "iframe", "object", "embed", "img", "base", "audio", "video", "source"}:
            offenders.append(f"<{tag}>")
        for name, value in attributes.items():
            if name in {"src", "srcset", "action", "formaction", "poster", "xlink:href"}:
                offenders.append(f"{tag} {name}={value}")
            if name == "href" and (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) or value.startswith(("/", "\\"))):
                offenders.append(f"{tag} href={value}")
            if name == "http-equiv":
                offenders.append(f"{tag} http-equiv")
    without_svg = re.sub(r"<svg\b.*?</svg>\n", "", text, flags=re.DOTALL)
    offenders.extend(f"url({target})" for target in re.findall(r"url\(\s*([^)#][^)]*)\)", text))
    offenders.extend(word for word in _NETWORK_WORDS if word in without_svg)
    offenders.extend(re.findall(r"(?:https?|ftp):|//[a-zA-Z]", without_svg.replace("://", ":")))
    return offenders


def test_no_page_and_not_the_index_names_an_external_resource(
    extracted: graph.DiagramGraphs, views: list[tuple[str, str]], view_ids: list[str]
) -> None:
    for view, svg_text in views:
        assert _external_resources(page.render_view_page(PROJECT, view, svg_text, view_ids)) == [], view
    index = page.render_index(PROJECT, views, report.render_report(PROJECT, views, extracted.not_drawn))
    assert _external_resources(index) == []


def test_the_external_resource_check_itself_finds_what_it_forbids() -> None:
    assert _external_resources('<link rel="stylesheet" href="a.css">') == ["<link>"]
    assert _external_resources('<a href="https://example.org/">x</a>') != []
    assert _external_resources('<script src="x.js"></script>') == ["script src=x.js"]
    assert _external_resources("<script>fetch('x')</script>") == ["fetch("]
    assert _external_resources('<p style="background: url(x.png)">x</p>') == ["url(x.png)"]
    assert _external_resources('<a href="nesting.html">x</a>') == []


# The index


def test_the_index_is_titled_with_the_project_name(
    extracted: graph.DiagramGraphs, views: list[tuple[str, str]]
) -> None:
    text = page.render_index(PROJECT, views, report.render_report(PROJECT, views, extracted.not_drawn))
    parsed = _parse(text)
    assert parsed.title == PROJECT
    assert "<h1>Diagram fixture</h1>" in text
    assert text.startswith("<!DOCTYPE html>\n") and text.endswith("</html>\n")


def test_the_index_lists_every_view_page_and_svg_in_the_given_order(
    extracted: graph.DiagramGraphs, views: list[tuple[str, str]], view_ids: list[str]
) -> None:
    text = page.render_index(PROJECT, views, "")
    pages = [href for href in _hrefs(text) if href.endswith(".html")]
    svgs = [href for href in _hrefs(text) if href.endswith(".svg")]
    assert pages == [f"{view}.html" for view in view_ids]
    assert svgs == [f"{view}.svg" for view in view_ids]
    # The link text is the view's own heading, and the groups follow the view kinds.
    for view, svg_text in views:
        assert f'<a href="{view}.html">{page.view_heading(svg_text)}</a>' in text
    assert re.findall(r"<h2>([^<]*)</h2>", text) == ["Compositions", "Nesting", "Deployment", "Sequences", "Report"]
    reordered = [views[1], views[0], *views[2:]]
    assert [href for href in _hrefs(page.render_index(PROJECT, reordered, "")) if href.endswith(".html")][:2] == [
        "overview-Chassis.html",
        "ibd-Chassis.html",
    ]


def test_the_index_carries_the_report_as_a_section(
    extracted: graph.DiagramGraphs, views: list[tuple[str, str]]
) -> None:
    text_report = report.render_report(PROJECT, views, extracted.not_drawn)
    text = page.render_index(PROJECT, views, text_report)
    escaped = text_report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert f'<section id="report">\n<h2>Report</h2>\n<pre>{escaped}</pre>\n</section>' in text
    assert f'<a href="{report.REPORT_FILE}">' in text


def test_an_index_without_nesting_or_sequences_omits_their_groups(views: list[tuple[str, str]]) -> None:
    only_compositions = [(view, svg_text) for view, svg_text in views if view.startswith(("ibd-", "overview-"))]
    text = page.render_index(PROJECT, only_compositions, "")
    assert re.findall(r"<h2>([^<]*)</h2>", text) == ["Compositions", "Report"]


def test_the_index_escapes_the_project_name(views: list[tuple[str, str]]) -> None:
    text = page.render_index('P <&> "q"', views, "a <b> & c")
    assert _parse(text).title == 'P <&> "q"'
    assert "<pre>a &lt;b&gt; &amp; c</pre>" in text


# The report


def test_the_fixture_report_is_exactly_this(extracted: graph.DiagramGraphs, views: list[tuple[str, str]]) -> None:
    sizes = {view: len(svg_text.encode("utf-8")) for view, svg_text in views}
    expected = "\n".join(
        [
            "Diagrams report: Diagram fixture",
            "",
            "Views: 7",
            f"  ibd-Chassis           906.48 x 314 px  {sizes['ibd-Chassis']} bytes",
            f"  overview-Chassis      670.14 x 245 px  {sizes['overview-Chassis']} bytes",
            f"  ibd-Vehicle           765.8 x 205 px  {sizes['ibd-Vehicle']} bytes",
            f"  overview-Vehicle      670.14 x 175 px  {sizes['overview-Vehicle']} bytes",
            f"  nesting               630.32 x 152 px  {sizes['nesting']} bytes",
            f"  deployment            388 x 228 px  {sizes['deployment']} bytes",
            f"  sequence-speed_cycle  614.6 x 296 px  {sizes['sequence-speed_cycle']} bytes",
            "",
            "Not drawn: 2 in 1 kind",
            "  not-run: 2",
            "    deployment  DiagServer: no task runs a runnable of it",
            "    deployment  Logger: no task runs a runnable of it",
            "",
            "Drawn as a loop: 1",
            "  ibd-Chassis  control_loop: Controller.pLoop -> Controller.rLoop",
            "",
        ]
    )
    assert report.render_report(PROJECT, views, extracted.not_drawn) == expected


def test_not_drawn_records_are_grouped_by_kind_with_counts_in_the_order_read() -> None:
    records = [
        graph.NotDrawn("ibd-B", "port-not-declared", "c2", "T declares no port p"),
        graph.NotDrawn(None, "composition-not-declared", "c9", "no composition Z"),
        graph.NotDrawn("ibd-A", "port-not-declared", "c1", "U declares no port q"),
        graph.NotDrawn("nesting", "no-root", "", "no composition declares root: true"),
        graph.NotDrawn("ibd-A", "loop", "self", "M.p -> M.r"),
    ]
    text = report.render_report("P", [], records)
    assert text == "\n".join(
        [
            "Diagrams report: P",
            "",
            "Views: 0",
            "",
            "Not drawn: 4 in 3 kinds",
            "  composition-not-declared: 1",
            "    (no view)  c9: no composition Z",
            "  no-root: 1",
            "    nesting  no composition declares root: true",
            "  port-not-declared: 2",
            "    ibd-B  c2: T declares no port p",
            "    ibd-A  c1: U declares no port q",
            "",
            "Drawn as a loop: 1",
            "  ibd-A  self: M.p -> M.r",
            "",
        ]
    )


def test_a_report_with_no_loop_says_none() -> None:
    text = report.render_report("P", [], [])
    assert text.endswith("Not drawn: none\n\nDrawn as a loop: none\n")


def test_the_report_carries_no_timing(extracted: graph.DiagramGraphs, views: list[tuple[str, str]]) -> None:
    text = report.render_report(PROJECT, views, extracted.not_drawn)
    assert not re.search(r"\d\s*(?:ms|s|sec|seconds|milliseconds)\b", text)
    assert not re.search(r"\b(?:time|timing|elapsed|duration)\b", text, flags=re.IGNORECASE)
    parameters = list(report.render_report.__annotations__)
    assert parameters == ["project_name", "views", "not_drawn", "return"]


def test_the_loop_kind_is_the_extractors(extracted: graph.DiagramGraphs) -> None:
    assert [record.kind for record in extracted.not_drawn if record.view != "deployment"] == [report.LOOP_KIND]


def test_the_report_reads_the_extractors_record_shape_field_for_field() -> None:
    protocol = get_protocol_members(report.NotDrawnLike)
    fields = {item.name: item.type for item in dataclasses.fields(graph.NotDrawn)}
    assert set(protocol) == set(fields)


# The modules


def _swcraft_imports(module: str) -> list[str]:
    tree = ast.parse((PACKAGE_DIR / module).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return sorted(name for name in imported if "swcraft" in name)


def test_the_page_imports_only_the_svg_primitives_and_the_report_nothing_of_swcraft() -> None:
    assert _swcraft_imports("page.py") == [
        "swcraft_tools.writer.diagrams.report",
        "swcraft_tools.writer.diagrams.svg",
    ]
    assert _swcraft_imports("report.py") == []


@pytest.fixture
def node() -> str:
    return requires_node()


def test_the_shared_script_is_valid_javascript(node: str, tmp_path: Path) -> None:
    script = tmp_path / "page.js"
    script.write_text(page.PAGE_SCRIPT, encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# What a drawn element opens: the temporary attribute, the model block, the elements block

GOLDEN_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams_golden"


def _block(text: str, block_id: str) -> dict:
    scripts = [body for attributes, body in _parse(text).scripts if attributes.get("id") == block_id]
    assert len(scripts) == 1, block_id
    return json.loads(scripts[0])


def test_the_strip_counts_every_drawn_element_and_leaves_the_plain_text() -> None:
    """An ordinal counts every ``[data-role]`` element, mapped or not; the attribute leaves no byte behind."""
    plain = (
        '<svg data-view="v">\n<g class="frame" data-role="frame" data-name="F"><title>F</title></g>\n'
        '<g class="edge-count" data-name="n"><title>2 connectors</title></g>\n'
        '<g class="edge bundle" data-role="bundle" data-name="a"></g>\n'
        '<g class="member" data-role="member" data-name="M"><rect x="1"/></g>\n</svg>\n'
    )
    collected = plain.replace('data-name="F">', f'data-name="F" {svg_module.SOURCE_ATTRIBUTE}="1">').replace(
        'data-name="M">', f'data-name="M" {svg_module.SOURCE_ATTRIBUTE}="0">'
    )
    stripped, entries = page.strip_sources(collected, [{"file": "m.yaml", "member": None}, {"refused": "no-file"}])
    assert stripped == plain
    # The frame is ordinal 0, the bundle -- which opens nothing -- 1, the member 2; the count square has no role.
    assert entries == {"0": {"refused": "no-file"}, "2": {"file": "m.yaml", "member": None}}


@pytest.mark.parametrize(
    ("collected", "sources"),
    [
        (f'<svg><g class="edge-count" {svg_module.SOURCE_ATTRIBUTE}="0"></g></svg>', [{"file": "a.yaml"}]),
        (f'<svg><g data-role="member" {svg_module.SOURCE_ATTRIBUTE}="1"></g></svg>', [{"file": "a.yaml"}]),
        ('<svg><g data-role="member"></g></svg>', [{"file": "a.yaml"}]),
    ],
    ids=["on-an-element-with-no-role", "an-index-out-of-range", "an-entry-written-nowhere"],
)
def test_the_strip_refuses_a_disagreement_with_the_renderers(collected: str, sources: list) -> None:
    with pytest.raises(ValueError, match="source attribute|model entry"):
        page.strip_sources(collected, sources)


def test_the_model_block_is_written_on_every_view_page_and_escaped(view_ids: list[str]) -> None:
    hostile = 'swc/a</script x>&"/B.yaml'
    entries = {"0": {"file": hostile, "member": "ports/p</script>"}, "1": {"refused": "several-files"}}
    text = page.render_view_page(PROJECT, "ibd-Chassis", _svg("ibd-Chassis"), view_ids, model=entries)
    assert _block(text, page.MODEL_SCRIPT_ID) == entries
    assert text.count("</script") == 4
    assert _block(page.render_view_page(PROJECT, "nesting", _svg("nesting"), view_ids), page.MODEL_SCRIPT_ID) == {}


def test_the_index_carries_the_elements_block_and_still_no_executable_script(views: list[tuple[str, str]]) -> None:
    elements = {"swc/a</script>/B.yaml": {"views": ["ibd-Chassis"], "members": {"ports/p": ["ibd-Chassis"]}}}
    text = page.render_index(PROJECT, views, "report", elements)
    assert _block(text, page.ELEMENTS_SCRIPT_ID) == elements
    assert [attributes for attributes, _body in _parse(text).scripts] == [
        {"type": "application/json", "id": page.ELEMENTS_SCRIPT_ID}
    ]
    assert _block(page.render_index(PROJECT, views, "report"), page.ELEMENTS_SCRIPT_ID) == {}


def test_the_elements_index_lists_views_in_the_indexs_order_and_names_no_refusal() -> None:
    a, b = {"file": "swc/A.yaml", "member": None}, {"file": "swc/A.yaml", "member": "ports/p"}
    views = [
        ("sequence-s", {"0": a}),
        ("nesting", {"3": a}),
        ("ibd-C", {"1": b, "0": a, "2": {"refused": "no-file"}}),
        ("deployment", {"0": a}),
    ]
    assert page.elements_index(views) == {
        "swc/A.yaml": {
            "views": ["ibd-C", "nesting", "deployment", "sequence-s"],
            "members": {"ports/p": ["ibd-C"]},
        }
    }


def _golden_files() -> list[Path]:
    return sorted(
        (path for path in GOLDEN_DIR.glob("*/*") if path.suffix in (".svg", ".html")), key=lambda path: path.as_posix()
    )


def test_no_golden_page_or_svg_carries_a_style_attribute_or_the_temporary_source_attribute() -> None:
    """No golden carries a ``style="..."`` attribute or the generator's source attribute.

    ``style-src`` governs a style attribute in the tab. ``test_diagram_generation.py`` holds the same of a fresh
    generation.
    """
    files = _golden_files()
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "style=" not in text, path.name
        assert svg_module.SOURCE_ATTRIBUTE not in text, path.name


def test_every_golden_view_page_carries_a_model_block_and_the_index_its_elements_block() -> None:
    for directory in sorted((path for path in GOLDEN_DIR.iterdir() if path.is_dir()), key=lambda path: path.as_posix()):
        index = _block((directory / page.INDEX_PAGE).read_text(encoding="utf-8"), page.ELEMENTS_SCRIPT_ID)
        assert index, directory.name
        for html_file in directory.glob("*.html"):
            if html_file.name != page.INDEX_PAGE:
                assert _block(html_file.read_text(encoding="utf-8"), page.MODEL_SCRIPT_ID), html_file.name


def test_the_source_attribute_and_the_opens_key_are_the_extractors() -> None:
    """The renderer imports nothing of the extractor, so its spellings (key, entry type) are held equal here."""
    assert svg_module.OPENS_KEY == graph.OPENS_KEY
    assert svg_module.ModelEntry == graph.ModelEntry
    assert svg_module.SOURCE_ATTRIBUTE == "data-swcraft-source"


# The hit band's geometry, and the theme that never reaches a generated file

#: The reference tab: half of a 1 050 px editor area, VS Code's padding reset.
REFERENCE_TAB_WIDTH = 480.0
#: The narrowest target, in CSS px, a sub-element may offer the pointer at the reference scale.
MINIMUM_TARGET = 8.0


def _hit_band_width(style: str) -> float:
    """The band's screen width the page style declares; 0 when the page has no band rule."""
    for selectors, body in _rules(style):
        if selectors == f"svg .{page.HIT_BAND_CLASS}":
            assert "vector-effect: non-scaling-stroke" in body, "a band of a fixed screen width at every zoom"
            assert "pointer-events: stroke" in body, "a transparent stroke still takes the pointer"
            match = re.search(r"stroke-width: ([0-9.]+)px", body)
            assert match is not None, body
            return float(match.group(1))
    return 0.0


def _hit_targets(svg_text: str, band: float) -> list[tuple[str, float]]:
    """Each sub-element's narrowest hit target on screen at the reference scale, in CSS px.

    The scale is the view's fit in a tab :data:`REFERENCE_TAB_WIDTH` wide, never above 1. A port is its
    ``rect.port-hit`` ringed by the band's stroke; a line is its path under the SVG's own wider hit stroke where the
    view draws one, else under its own stroke, with the band centred on it.
    """
    root = ET.fromstring(svg_text)
    scale = min(1.0, REFERENCE_TAB_WIDTH / float(root.get("width") or "0"))
    own_style = "".join(element.text or "" for element in root.iter(f"{SVG_NS}style"))
    use_width = re.search(r"use\{stroke-width:([0-9.]+)px", own_style)
    targets: list[tuple[str, float]] = []
    for element in root.iter():
        role = element.get("data-role")
        if role not in page.HIT_BAND_ROLES:
            continue
        label = f"{role} {element.get('data-owner') or ''}.{element.get('data-name')}"
        if role == "port":
            (hit,) = [child for child in element if child.get("class") == svg_module.PORT_HIT_CLASS]
            narrow = min(float(hit.get("width") or "0"), float(hit.get("height") or "0"))
            targets.append((label, narrow * scale + band))
            continue
        path = next(element.iter(f"{SVG_NS}path"))
        if element.find(f"{SVG_NS}use") is not None and use_width is not None:
            width = float(use_width.group(1))
        else:
            width = float(path.get("stroke-width") or element.get("stroke-width") or "1")
        targets.append((label, max(width * scale, band)))
    return targets


def _golden_svgs() -> list[Path]:
    return [path for path in _golden_files() if path.suffix == ".svg"]


def test_every_sub_element_is_at_least_eight_css_px_wide_at_the_reference_scale() -> None:
    """A port, a line and a message offer the pointer at least :data:`MINIMUM_TARGET` CSS px on every golden view.

    Measured from the SVG's own geometry plus the band the page style declares. What it does not reach: whether Chromium
    hit-tests the band as declared, and the pointer's own aim.
    """
    band = _hit_band_width(page.PAGE_STYLE)
    files = _golden_svgs()
    assert files
    narrow: list[str] = []
    for path in files:
        for label, width in _hit_targets(path.read_text(encoding="utf-8"), band):
            if width < MINIMUM_TARGET:
                narrow.append(f"{path.parent.name}/{path.stem}: {label} {width:.2f}px")
    assert narrow == []


def test_without_the_band_the_walks_port_and_every_message_are_too_narrow() -> None:
    """The oracle's other half: the band is what passes them, not the geometry."""
    chassis = (GOLDEN_DIR / "diagrams" / "ibd-Chassis.svg").read_text(encoding="utf-8")
    bare = dict(_hit_targets(chassis, 0.0))
    assert 6.0 < bare["port Sensor.pSpeed"] < MINIMUM_TARGET
    sequence_view = (GOLDEN_DIR / "diagrams" / "sequence-speed_cycle.svg").read_text(encoding="utf-8")
    messages = [
        width for label, width in _hit_targets(sequence_view, 0.0) if label.split(" ")[0] in ("activation", "data")
    ]
    assert messages and all(width < MINIMUM_TARGET for width in messages)


def test_every_drawn_role_is_classified_for_the_band() -> None:
    """A new role fails here until it is either a sub-element with a band or a box or frame without one."""
    roles = set()
    for path in _golden_svgs():
        roles.update(re.findall(r'data-role="([a-z]+)"', path.read_text(encoding="utf-8")))
    assert roles <= set(page.HIT_BAND_ROLES) | {"member", "frame", "lifeline"}, roles
    for role in page.HIT_BAND_ROLES:
        assert f'[data-role="{role}"]' in page.PAGE_SCRIPT, role


def test_the_band_carries_no_role_and_every_mark_and_highlight_rule_excludes_it() -> None:
    script = page.PAGE_SCRIPT
    appended = script[script.index("var band = ") : script.index("element.appendChild(band);")]
    assert "data-role" not in appended, "a role on the band would move every ordinal"
    assert f'band.setAttribute("class", "{page.HIT_BAND_CLASS}")' in appended
    for selectors, _body in _rules(page.PAGE_STYLE):
        for selector in selectors.split(", "):
            reaches_a_shape = selector.endswith((" rect", " path")) or re.search(
                r" (rect|path)(:not\([^)]*\))+$", selector
            )
            if reaches_a_shape and re.search(r"\.(hit|current|highlight|selected)\b", selector):
                assert f":not(.{page.HIT_BAND_CLASS})" in selector, selector


#: What only the tab's own stylesheet may name: VS Code's body classes and colour variables.
THEME_WORDS = ("vscode-light", "vscode-dark", "vscode-high-contrast", "--vscode-", "prefers-color-scheme")


def test_no_generated_file_names_a_theme(
    node: str, tmp_path: Path, extracted: graph.DiagramGraphs, views: list[tuple[str, str]], view_ids: list[str]
) -> None:
    """The tab follows VS Code's theme by a stylesheet the extension inserts; no generated file carries it."""
    texts = {
        path.as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(GOLDEN_DIR.glob("*/*"), key=lambda path: path.as_posix())
        if path.is_file()
    }
    texts["PAGE_STYLE"] = page.PAGE_STYLE
    texts["PAGE_SCRIPT"] = page.PAGE_SCRIPT
    for view, svg_text in views:
        texts[f"render {view}"] = page.render_view_page(PROJECT, view, svg_text, view_ids)
    texts["render index"] = page.render_index(PROJECT, views, report.render_report(PROJECT, views, extracted.not_drawn))
    views_root = tmp_path / "views"
    diagrams_package.generate_diagrams(str(FIXTURE_DIR / "model"), str(views_root), PROJECT, node=node)
    for path in sorted(views_root.iterdir(), key=lambda path: path.as_posix()):
        texts[f"generated {path.name}"] = path.read_text(encoding="utf-8")
    assert len(texts) > 30
    for name, text in texts.items():
        for word in THEME_WORDS:
            assert word not in text, (name, word)
