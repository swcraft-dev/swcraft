"""The page script's highlight and tooltip **behaviour**, run in Node over a minimal DOM.

``test_diagram_page.py`` pins the script's text, and its ``_lit`` re-implements the highlight rule in Python, which
proves the attributes are there, not that the script uses them. Here the shipped :data:`page.PAGE_SCRIPT` runs unchanged
against a DOM stub built from a rendered page's own SVG; pointer, click, drag, search, key and fragment steps are
dispatched to its listeners and the page's state is read back after each. The second half runs it over the generator's
own pages with a stub host (``window.swcraftHost``) recording what the page posts; elements are addressed there by
ordinal among the ``[data-role]`` elements, because role, owner and name do not tell the collision fixture's elements
apart.

The script runs against **this stub, never a browser**. What the stub does not reach: a real browser's event order,
hit-testing (the pointer's target is named, never computed; for a port it is its hit box), the tooltip's measured size
and position, whether a browser still shows a native tooltip, the colours and dash as drawn, and the pan and zoom
arithmetic. The selector matcher understands exactly the forms the script uses (a tag, ``.class``, ``[attr]``,
``[attr="value"]``, ``:not(...)``, a comma list) and throws on any other, so a new form fails here.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.writer import diagrams
from swcraft_tools.writer.diagrams import graph, page, sequence, svg

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
SVG_DIR = FIXTURE_DIR / "svg"
LAID_OUT_DIR = FIXTURE_DIR / "laid-out"
SVG_NS = "{http://www.w3.org/2000/svg}"

#: The DOM stub and the step runner. It reads ``{script, svg, blocks, hash, steps}`` on stdin and prints, per step, what
#: is lit, the status line, the tooltip, the panel's heading, whether it is open and its rows, the search hits, and the
#: ``<title>`` nodes left once the script loaded. A step's action is one of ``over``, ``move``, ``out``, ``click``,
#: ``drag``, ``search``, ``escape``, ``pagekey``, ``close``, and for the host's half ``key``, ``button``, ``message``,
#: ``blur`` and ``focussearch``; an unknown action is an error, never a click. An element is named by its key (``on``)
#: or its ordinal (``at``, with an optional ``part``).
HARNESS = r"""
"use strict";
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));

class El {
  constructor(tag, attrs, text) {
    this.tagName = tag;
    this.attrs = Object.assign({}, attrs || {});
    this.children = [];
    this.parentNode = null;
    this.textContent = text || "";
    this.listeners = {};
    this.value = "";
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.hidden = "hidden" in this.attrs;
    this.style = {};
    this.classes = new Set((this.attrs.class || "").split(/\s+/).filter(Boolean));
    const self = this;
    this.classList = {
      add: (name) => self.classes.add(name),
      remove: (name) => self.classes.delete(name),
      contains: (name) => self.classes.has(name),
    };
  }
  // As the DOM's: assigning the text drops the children (and their parent links), reading it
  // joins the element's own text with its descendants'.
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(""); }
  set textContent(text) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.ownText = String(text);
  }
  get id() { return this.attrs.id || ""; }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  hasAttribute(name) { return name in this.attrs; }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === "class") this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  removeAttribute(name) { delete this.attrs[name]; }
  focus() { focus.active = this; }
  blur() { if (focus.active === this) focus.active = null; }
  // As an input's: the whole value selected (the caret is never moved by the script otherwise).
  select() { this.selectionStart = 0; this.selectionEnd = String(this.value).length; }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
  setPointerCapture() {}
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index < 0) throw new Error("not a child");
    this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  getScreenCTM() { return null; }
  // The element's first drawn rectangle, where it has one (a box, a port's square), else a unit box.
  getBBox() {
    const queue = this.children.slice();
    while (queue.length) {
      const child = queue.shift();
      if (child.tagName === "rect" && child.attrs.width !== undefined) {
        const box = child.attrs;
        return { x: +box.x || 0, y: +box.y || 0, width: +box.width, height: +box.height };
      }
      queue.push(...child.children);
    }
    return { x: 0, y: 0, width: 1, height: 1 };
  }
  matches(selector) { return selector.split(",").some((part) => matchCompound(this, part.trim())); }
  closest(selector) {
    for (let element = this; element; element = element.parentNode) {
      if (element.matches(selector)) return element;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (element) => {
      for (const child of element.children) {
        if (child.matches(selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matchCompound(element, selector) {
  let rest = selector;
  const tag = rest.match(/^[a-zA-Z]+/);
  if (tag) {
    if (element.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;
    rest = rest.slice(tag[0].length);
  }
  while (rest) {
    let token;
    if ((token = rest.match(/^\.([-\w]+)/))) {
      if (!element.classes.has(token[1])) return false;
    } else if ((token = rest.match(/^\[([-\w]+)(?:="([^"]*)")?\]/))) {
      if (!element.hasAttribute(token[1])) return false;
      if (token[2] !== undefined && element.getAttribute(token[1]) !== token[2]) return false;
    } else if ((token = rest.match(/^:not\(([^)]*)\)/))) {
      if (matchCompound(element, token[1])) return false;
    } else {
      throw new Error("the stub does not understand the selector " + JSON.stringify(selector));
    }
    rest = rest.slice(token[0].length);
  }
  return true;
}

// Which element holds the focus.
const focus = { active: null };
const keyed = {};
function build(node, parent) {
  const element = new El(node.tag, node.attrs, node.text);
  element.parentNode = parent;
  if (node.key) keyed[node.key] = element;
  for (const child of node.children) element.children.push(build(child, element));
  return element;
}

const canvas = new El("main", { id: "canvas" });
const svg = build(input.svg, canvas);
canvas.children.push(svg);
const byId = { canvas: canvas, status: new El("footer", { id: "status" }) };
const tip = new El("div", { id: "tip", role: "tooltip", hidden: "" });
// The tooltip's size and the window's, stated: the stub lays nothing out.
tip.offsetWidth = 100;
tip.offsetHeight = 20;
byId.tip = tip;
const CONTROLS = ["search", "search-count", "search-next", "search-previous", "zoom-in", "zoom-out", "zoom-fit"];
for (const id of CONTROLS.concat(["reload", "connectors-close", "open-in-model", "selection-next"])) {
  byId[id] = new El("input", { id: id });
}
byId.search.placeholder = "Search names";
byId["open-in-model"].hidden = true;
byId["selection-next"].hidden = true;
byId.selection = new El("div", { id: "selection", hidden: "" });
byId["selection-text"] = new El("span", { id: "selection-text" });
byId.connectors = new El("aside", { id: "connectors", hidden: "" });
byId["connectors-head"] = new El("span", { id: "connectors-head" });
byId["connectors-list"] = new El("ol", { id: "connectors-list" });
for (const id of Object.keys(input.blocks)) byId[id] = new El("script", { id: id }, input.blocks[id]);
const documentListeners = {};
const document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new El(tag, {}),
  createElementNS: (namespace, tag) => new El(tag, {}),
  get activeElement() { return focus.active; },
  addEventListener: (type, listener) => {
    (documentListeners[type] = documentListeners[type] || []).push(listener);
  },
};
const windowListeners = {};
let reloads = 0;
const window = {
  location: { hash: input.hash, href: "", reload: () => { reloads += 1; } },
  innerWidth: 800,
  innerHeight: 600,
  addEventListener: (type, listener) => {
    (windowListeners[type] = windowListeners[type] || []).push(listener);
  },
};
// A stub host when the payload asks for one: it records every call, as the bridge would post it.
const posted = [];
const hostHandlers = {};
if (input.host) {
  window.swcraftHost = {
    state: input.host.state || {},
    navigate: (href) => posted.push(["navigate", href]),
    open: (file, member) => posted.push(["open", file, member]),
    reload: () => posted.push(["reload"]),
    on: (type, handler) => { hostHandlers[type] = handler; },
  };
}
// The drawn elements by ordinal: every [data-role] element in document order, before the script
// runs -- the generator's count, which keys the model block.
const drawn = svg.querySelectorAll("[data-role]");

new Function("document", "window", input.script)(document, window);
const titlesLeft = svg.querySelectorAll("title").length;

function target(key) {
  if (key === null) return svg;
  const element = keyed[key];
  if (!element) throw new Error("no element " + key);
  // The pointer is on the element's own drawing, its last child, as a browser would report it.
  return element.children.length ? element.children[element.children.length - 1] : element;
}
// A step names an element by its ordinal (``at``) and, optionally, which of its parts the pointer
// is on: ``band`` (the hit band the script appended), ``first`` (its first drawn child -- a box's
// rect, a line's path), or by default its last child as above.
function targetAt(step) {
  const element = drawn[step.at];
  if (!element) throw new Error("no element at ordinal " + step.at);
  const parts = element.children.filter((child) => child.tagName !== "title" && child.tagName !== "desc");
  if (step.part === "band") {
    const band = element.children.find((child) => child.classes.has("hit-band"));
    if (!band) throw new Error("no hit band at ordinal " + step.at);
    return band;
  }
  if (step.part === "first") return parts[0] || element;
  return element.children.length ? element.children[element.children.length - 1] : element;
}
function dispatch(type, element, clientX, clientY, modified) {
  const event = {
    type: type,
    target: element,
    button: 0,
    pointerId: 1,
    clientX: clientX,
    clientY: clientY || 0,
    ctrlKey: false,
    metaKey: Boolean(modified),
  };
  event.preventDefault = () => {};
  for (const listener of svg.listeners[type] || []) listener(event);
}
function marked(name) {
  return Object.keys(keyed).filter((key) => keyed[key].classes.has(name)).sort();
}
function markedAt(name) {
  return drawn.map((element, index) => (element.classes.has(name) ? index : -1)).filter((index) => index >= 0);
}
function bandsAt() {
  return drawn.map((element) => element.children.filter((child) => child.classes.has("hit-band")).length);
}
function answer() {
  return {
    lit: marked("highlight"),
    status: byId.status.textContent,
    tip: tip.hidden ? null : tip.textContent,
    at: tip.hidden ? null : [tip.style.left, tip.style.top],
    heading: byId["connectors-head"].textContent,
    panelOpen: !byId.connectors.hidden,
    // Each row as [its own text, its note's text or null].
    rows: byId["connectors-list"].children.map((item) => {
      const note = item.children.find((child) => child.className === "note");
      return [item.ownText, note ? note.textContent : null];
    }),
    hits: marked("hit"),
    titlesLeft: titlesLeft,
    // By ordinal, for the elements two keys would not tell apart.
    hitsAt: markedAt("hit"),
    currentAt: markedAt("current"),
    selectedAt: markedAt("selected"),
    litAt: markedAt("highlight"),
    bands: bandsAt(),
    drawnCount: svg.querySelectorAll("[data-role]").length,
    posted: posted.slice(),
    href: window.location.href,
    reloads: reloads,
    selection: byId.selection.hidden ? null : byId["selection-text"].textContent,
    openButton: !byId.selection.hidden && !byId["open-in-model"].hidden,
    nextButton: !byId.selection.hidden && !byId["selection-next"].hidden,
    focused: focus.active ? focus.active.id : null,
    placeholder: byId.search.placeholder,
    value: byId.search.value,
    // The search box's selected range, [start, end], meaningful only while it holds a value.
    selected: [byId.search.selectionStart, byId.search.selectionEnd],
    count: byId["search-count"].value,
    viewBox: svg.getAttribute("viewBox"),
  };
}

const answers = [answer()];
for (const step of input.steps) {
  const element = step.at !== undefined ? targetAt(step) : step.on === undefined ? svg : target(step.on);
  const x = step.x || 0;
  const y = step.y || 0;
  if (step.do === "over") {
    dispatch("pointerover", element, x, y);
  } else if (step.do === "move") {
    dispatch("pointermove", element, x, y);
  } else if (step.do === "out") {
    dispatch("pointerout", element, x, y);
  } else if (step.do === "search") {
    // Typed: what the box then holds (typing over a whole selection replaces it), and its input event.
    byId.search.value = step.text;
    byId.search.selectionStart = byId.search.selectionEnd = step.text.length;
    for (const listener of byId.search.listeners.input || []) listener({ target: byId.search });
  } else if (step.do === "escape") {
    for (const listener of documentListeners.keydown || []) {
      listener({ key: "Escape", target: svg, ctrlKey: false, metaKey: false, altKey: false });
    }
  } else if (step.do === "pagekey") {
    // A key pressed on the page: the document's keydown, its target the element holding the
    // focus -- the search box while it does, else the page itself (the stub's SVG stands for body).
    for (const listener of documentListeners.keydown || []) {
      listener({ key: step.key, target: focus.active || svg, ctrlKey: false, metaKey: false, altKey: false });
    }
  } else if (step.do === "close") {
    const button = byId["connectors-close"];
    for (const listener of button.listeners.click || []) listener({ target: button });
  } else if (step.do === "click" || step.do === "drag") {
    dispatch("pointerdown", element, x, y, step.meta);
    if (step.do === "drag") dispatch("pointermove", element, x + 40, y, step.meta);
    dispatch("pointerup", element, step.do === "drag" ? x + 40 : x, y, step.meta);
  } else if (step.do === "key") {
    // A key in the search box: its own keydown listener, as the browser dispatches it.
    const event = { key: step.key, shiftKey: Boolean(step.shift), target: byId.search, ctrlKey: false, metaKey: false };
    event.preventDefault = () => {};
    for (const listener of byId.search.listeners.keydown || []) listener(event);
  } else if (step.do === "button") {
    const button = byId[step.id];
    for (const listener of button.listeners.click || []) listener({ target: button });
  } else if (step.do === "focussearch") {
    // The user puts the caret in the search box.
    byId.search.focus();
  } else if (step.do === "message") {
    // A message the host relays from the extension.
    if (!hostHandlers[step.type]) throw new Error("the page listens for no message " + JSON.stringify(step.type));
    hostHandlers[step.type](step.data);
  } else if (step.do === "blur") {
    focus.active = null;
    for (const listener of windowListeners.blur || []) listener({ type: "blur" });
  } else {
    throw new Error("the harness does not know the step " + JSON.stringify(step.do));
  }
  answers.push(answer());
}
process.stdout.write(JSON.stringify(answers));
"""


def _key(element: ET.Element) -> str | None:
    role = element.get("data-role")
    if role is None:
        # An overview's count square carries no role; it is keyed by its class and pair.
        if "edge-count" in (element.get("class") or "").split():
            return f"count|{element.get('data-name')}"
        # The frame's name on a placed composition, the way up: one per view at most.
        if "frame-up" in (element.get("class") or "").split():
            return "up"
        return None
    return "|".join(part for part in (role, element.get("data-owner"), element.get("data-name")) if part)


def _tree(element: ET.Element) -> dict[str, Any]:
    tag = element.tag.removeprefix(SVG_NS)
    return {
        "tag": tag,
        "attrs": dict(element.attrib),
        "text": element.text or "" if tag in ("title", "desc") else "",
        "key": _key(element),
        "children": [_tree(child) for child in element],
    }


def _run(node: str, tmp_path: Path, view: str, steps: list[dict[str, Any]], fragment: str = "") -> list[list[str]]:
    """Render *view*'s page, run the shipped script over its SVG and answer what is lit after each step."""
    return [answer["lit"] for answer in _answers(node, tmp_path, view, steps, fragment)]


def _title(view: str, key: str) -> str:
    """The ``<title>`` text the rendered SVG gives the element keyed *key*."""
    root = ET.fromstring((SVG_DIR / f"{view}.svg").read_text(encoding="utf-8"))
    (element,) = [element for element in root.iter() if _key(element) == key]
    first = element[0]
    assert first.tag == f"{SVG_NS}title"
    return first.text or ""


def _answers(
    node: str,
    tmp_path: Path,
    view: str,
    steps: list[dict[str, Any]],
    fragment: str = "",
    retitle: dict[str, str] | None = None,
    contents: Mapping[str, Sequence[str]] | None = None,
    unlisted: frozenset[str] = frozenset(),
    host: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render *view*'s page, run the shipped script over its SVG and answer the page's state after each step.

    *retitle* maps an element's key to a replacement ``<title>`` text. *contents* renders the view with that interface
    map instead of reading its golden (how a capped port reaches the page). *unlisted* names the elements whose
    ``<desc>`` list is removed. *host*, when given, is the stub host's state, and every drawn element then opens
    ``swc/Drawn.yaml`` at its own ordinal's member, so a click selects it.
    """
    extracted = graph.build_graphs(str(FIXTURE_DIR / "model"))
    view_ids = graph.view_ids(extracted)
    if contents is None:
        svg_text = (SVG_DIR / f"{view}.svg").read_text(encoding="utf-8")
    elif view.startswith("sequence-"):
        stems = [f"sequence-{stem}" for stem in extracted.sequence_stems]
        svg_text = sequence.render_sequence(extracted.sequences[stems.index(view)], contents)
    else:
        laid_out = json.loads((LAID_OUT_DIR / f"{view}.json").read_text(encoding="utf-8"))
        svg_text = svg.render_ibd(laid_out, contents)
    text = page.render_view_page("P", view, svg_text, view_ids, None, extracted.placements)
    svg_markup = re.search(r"<svg\b.*?</svg>", text, re.DOTALL)
    assert svg_markup is not None
    blocks = {
        block_id: re.search(rf'<script type="application/json" id="{block_id}">(.*?)</script>', text).group(1)  # type: ignore[union-attr]
        for block_id in (page.LINKS_SCRIPT_ID, page.UP_SCRIPT_ID)
    }
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8", newline="\n")
    svg_root = ET.fromstring(svg_markup.group(0))
    for element in list(svg_root.iter()):
        if _key(element) in unlisted:
            for listing in element.findall(f"{SVG_NS}desc"):
                element.remove(listing)
        text = (retitle or {}).get(_key(element) or "")
        if text is not None:
            assert element[0].tag == f"{SVG_NS}title"
            element[0].text = text
    payload: dict[str, Any] = {
        "script": page.PAGE_SCRIPT,
        "svg": _tree(svg_root),
        "blocks": blocks,
        "hash": f"#{fragment}" if fragment else "",
        "steps": steps,
    }
    if host is not None:
        drawn = [element for element in svg_root.iter() if element.get("data-role") is not None]
        blocks[page.MODEL_SCRIPT_ID] = json.dumps(
            {str(index): {"file": "swc/Drawn.yaml", "member": f"drawn/{index}"} for index in range(len(drawn))}
        )
        payload["host"] = {"state": host}
    completed = subprocess.run(
        [node, str(harness)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def node() -> str:
    return requires_node()


LINE = "connector|diag_link"
FAR = "port|Diag|pDiag"
NEAR = "port|Chassis|rDiag"
#: A port with no line: the pointer on it lights it alone.
LONE = "port|Chassis|pSpeedOut"
LIT_LINE = sorted([LINE, FAR, NEAR])
#: An IBD member box: titled, but never a subject (it has no ``id``, so it is no end).
BOX = "member|Diag"


def test_the_pointer_on_a_line_lights_it_and_its_two_ends_and_a_lone_port_lights_alone(
    node: str, tmp_path: Path
) -> None:
    answers = _run(
        node,
        tmp_path,
        "ibd-Vehicle",
        [{"do": "over", "on": LINE}, {"do": "over", "on": LONE}, {"do": "over", "on": BOX}],
    )
    assert answers == [[], LIT_LINE, [LONE], []]


def test_a_click_pins_what_it_lights_against_the_pointer_and_empty_space_unpins(node: str, tmp_path: Path) -> None:
    answers = _run(
        node,
        tmp_path,
        "ibd-Vehicle",
        [
            {"do": "click", "on": NEAR},
            {"do": "over", "on": LONE},
            {"do": "click", "on": None},
            {"do": "over", "on": LINE},
            {"do": "over", "on": LONE},
        ],
    )
    assert answers == [[], LIT_LINE, LIT_LINE, [], LIT_LINE, [LONE]]


def test_a_drag_pins_nothing(node: str, tmp_path: Path) -> None:
    answers = _run(node, tmp_path, "ibd-Vehicle", [{"do": "drag", "on": LINE}, {"do": "over", "on": LONE}])
    assert answers == [[], [], [LONE]]


def test_arriving_by_the_fragment_pins_the_member_it_names(node: str, tmp_path: Path) -> None:
    answers = _run(node, tmp_path, "ibd-Vehicle", [{"do": "over", "on": LINE}], fragment="Chassis")
    assert answers == [["member|Chassis"], ["member|Chassis"]]


def test_a_lifeline_is_an_end_keyed_by_its_name(node: str, tmp_path: Path) -> None:
    answers = _run(node, tmp_path, "sequence-speed_cycle", [{"do": "over", "on": "activation|SensorRun"}])
    assert answers[1] == sorted(["activation|SensorRun", "lifeline|Task_10ms", "lifeline|Sensor"])


def test_an_overview_pair_lights_its_two_boxes(node: str, tmp_path: Path) -> None:
    answers = _run(node, tmp_path, "overview-Chassis", [{"do": "over", "on": "bundle|Sensor -> Controller"}])
    assert answers[1] == sorted(["bundle|Sensor -> Controller", "member|Sensor", "member|Controller"])


# The title cache and the tooltip


def test_the_titles_leave_the_page_and_the_status_line_still_reads_them(node: str, tmp_path: Path) -> None:
    """Every ``<title>`` node is gone once the script has loaded, the view's heading included.

    The status line still says what the subject's title said: the cache serves it.
    """
    answers = _answers(node, tmp_path, "ibd-Vehicle", [{"do": "over", "on": LINE}, {"do": "click", "on": NEAR}])
    assert answers[0]["titlesLeft"] == 0
    assert answers[1]["status"] == _title("ibd-Vehicle", LINE)
    assert answers[2]["status"] == _title("ibd-Vehicle", NEAR)


def test_the_panel_heading_still_reads_the_count_squares_title(node: str, tmp_path: Path) -> None:
    square = "count|Sensor -> Controller"
    answers = _answers(node, tmp_path, "overview-Chassis", [{"do": "click", "on": square}])
    assert answers[0]["titlesLeft"] == 0
    assert answers[1]["heading"] == _title("overview-Chassis", square) == "2 connectors Sensor -> Controller"


def test_the_search_still_finds_a_titles_text(node: str, tmp_path: Path) -> None:
    """The search's index was read from the nodes before they left: a port is still found."""
    answers = _answers(node, tmp_path, "ibd-Vehicle", [{"do": "search", "text": "pdiag"}])
    assert FAR in answers[1]["hits"]
    assert LINE in answers[1]["hits"], "a line's title names its ends"


def test_the_tooltip_shows_any_titled_elements_text_at_once_a_member_box_too(node: str, tmp_path: Path) -> None:
    """The tooltip reads the nearest titled ancestor, independent of the highlight's subject."""
    answers = _answers(
        node,
        tmp_path,
        "ibd-Vehicle",
        [
            {"do": "over", "on": BOX, "x": 100, "y": 50},
            {"do": "over", "on": NEAR, "x": 100, "y": 50},
            {"do": "over", "on": "frame|Vehicle", "x": 100, "y": 50},
        ],
    )
    assert answers[0]["tip"] is None, "hidden until a pointer rests on something"
    assert answers[1]["tip"] == _title("ibd-Vehicle", BOX)
    assert answers[1]["lit"] == [], "a member box is no subject, but it has a hover text"
    assert answers[1]["at"] == ["114px", "68px"], "next to the pointer"
    assert answers[2]["tip"] == _title("ibd-Vehicle", NEAR)
    assert answers[3]["tip"] == _title("ibd-Vehicle", "frame|Vehicle")


def test_the_empty_canvas_shows_no_tooltip(node: str, tmp_path: Path) -> None:
    """The ``<svg>`` itself is never a titled element: its heading is removed and not cached."""
    answers = _answers(node, tmp_path, "ibd-Vehicle", [{"do": "over", "x": 100, "y": 50}])
    assert answers[1]["tip"] is None


def test_a_line_with_a_two_line_title_shows_both_lines(node: str, tmp_path: Path) -> None:
    """A dashed line's title carries its reason on the second line; the tooltip shows the whole text."""
    two_lines = f"{_title('ibd-Vehicle', LINE)}\nnot judged: a reason on the second line."
    answers = _answers(node, tmp_path, "ibd-Vehicle", [{"do": "over", "on": LINE}], retitle={LINE: two_lines})
    assert answers[1]["tip"] == two_lines


def test_the_tooltip_follows_the_pointer_while_a_click_pins_the_highlight(node: str, tmp_path: Path) -> None:
    answers = _answers(
        node,
        tmp_path,
        "ibd-Vehicle",
        [{"do": "click", "on": NEAR}, {"do": "over", "on": BOX}, {"do": "move", "on": BOX, "x": 30, "y": 40}],
    )
    assert answers[1]["tip"] is None, "a press hides it"
    assert answers[2]["lit"] == LIT_LINE and answers[2]["tip"] == _title("ibd-Vehicle", BOX)
    assert answers[3]["at"] == ["44px", "58px"], "a move carries it with the pointer"


def test_the_tooltip_stays_inside_the_window(node: str, tmp_path: Path) -> None:
    """Near the window's right and bottom edges it flips to the pointer's other side.

    The stub states a 100x20 tooltip in an 800x600 window; a browser measures both.
    """
    answers = _answers(node, tmp_path, "ibd-Vehicle", [{"do": "over", "on": BOX, "x": 790, "y": 595}])
    assert answers[1]["at"] == ["682px", "567px"]


def test_the_tooltip_hides_on_leaving_on_a_press_and_on_escape(node: str, tmp_path: Path) -> None:
    answers = _answers(
        node,
        tmp_path,
        "ibd-Vehicle",
        [
            {"do": "over", "on": BOX},
            {"do": "out", "on": BOX},
            {"do": "over", "on": BOX},
            {"do": "escape"},
            {"do": "over", "on": NEAR},
            {"do": "drag", "on": NEAR},
        ],
    )
    assert [answer["tip"] is None for answer in answers] == [True, False, True, False, True, False, True]


# The list panel: a capped port lists its interface's every line

#: The boundary port ``Chassis.pSpeedOut`` of ``ibd-Chassis`` (``ISpeed``).
CAPPED = "port|pSpeedOut"
#: The boundary port ``Chassis.rDiag`` (``IDiag``).
DIAG = "port|rDiag"
#: A member's ``ISpeed`` port with a description, ``The filtered speed.``
DESCRIBED = "port|Sensor|pSpeed"
#: ``ISpeed`` grown to eleven data elements; ``IDiag`` within the cap.
ELEVEN = tuple(f"e{index}: uint8" for index in range(1, 12))
WALK_MAP: dict[str, tuple[str, ...]] = {"ISpeed": ELEVEN, "IDiag": ("ReadStatus(out status: uint8)", "Reset()")}
#: ``IDiag`` over the cap too, so that a click can move from one capped port to another.
TEN_OPERATIONS = tuple(f"Op{index}()" for index in range(1, 11))


def _rows(lines: Sequence[str]) -> list[list[str | None]]:
    return [[line, None] for line in lines]


def test_a_click_on_a_capped_port_lists_every_line_pins_it_and_a_second_click_closes(node: str, tmp_path: Path) -> None:
    answers = _answers(
        node,
        tmp_path,
        "ibd-Chassis",
        [
            {"do": "click", "on": CAPPED},
            {"do": "over", "on": DIAG},
            {"do": "click", "on": CAPPED},
        ],
        contents=WALK_MAP,
    )
    opened, moved, closed = answers[1:]
    assert opened["panelOpen"] is True
    assert opened["heading"] == "Chassis.pSpeedOut: provide ISpeed (sender-receiver)", "the title's first line"
    assert opened["rows"] == _rows(ELEVEN), "every line, unprefixed, in the model's order"
    assert CAPPED in opened["lit"], "the click still pins the port"
    assert moved["lit"] == opened["lit"] and moved["panelOpen"] is True, "the pin holds over another subject"
    assert closed["panelOpen"] is False and closed["rows"] == [] and closed["lit"] == []


@pytest.mark.parametrize(
    "closing",
    [{"do": "click", "on": None}, {"do": "close"}],
    ids=["empty-canvas", "close-button"],
)
def test_a_click_on_empty_canvas_and_the_close_button_close_a_ports_list(
    node: str, tmp_path: Path, closing: dict[str, Any]
) -> None:
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "click", "on": CAPPED}, closing], contents=WALK_MAP)
    assert answers[1]["panelOpen"] is True
    assert answers[2]["panelOpen"] is False and answers[2]["rows"] == [] and answers[2]["lit"] == []


@pytest.mark.parametrize(
    "closing",
    [{"do": "click", "on": CAPPED}, {"do": "close"}],
    ids=["second-click", "close-button"],
)
def test_closing_a_ports_list_with_a_host_ends_the_selection_and_its_open_in_model(
    node: str, tmp_path: Path, closing: dict[str, Any]
) -> None:
    # Both ways of closing the list end the pin and the selection together: the close button never clears the pin's
    # highlight while leaving the bar's Open in model aimed at the port.
    steps = [{"do": "click", "on": CAPPED}, closing, {"do": "button", "id": "open-in-model"}]
    answers = _answers(node, tmp_path, "ibd-Chassis", steps, contents=WALK_MAP, host={})
    opened, closed, pressed = answers[1:]
    assert opened["panelOpen"] is True and CAPPED in opened["lit"]
    assert opened["selection"] is not None and opened["openButton"] is True, "the pinned port is selected"
    assert closed["panelOpen"] is False and closed["lit"] == []
    assert closed["selection"] is None and closed["openButton"] is False, "no bar outlives the pin"
    assert pressed["posted"] == [], "Open in model targets nothing once the list is closed"


def test_a_click_on_a_port_within_the_cap_opens_nothing_and_pins(node: str, tmp_path: Path) -> None:
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "click", "on": DIAG}], contents=WALK_MAP)
    assert answers[1]["panelOpen"] is False and answers[1]["rows"] == []
    assert DIAG in answers[1]["lit"]


def test_a_reopened_list_shows_its_rows_once_and_a_second_capped_port_switches_it(node: str, tmp_path: Path) -> None:
    answers = _answers(
        node,
        tmp_path,
        "ibd-Chassis",
        [
            {"do": "click", "on": CAPPED},
            {"do": "click", "on": None},
            {"do": "click", "on": CAPPED},
            {"do": "click", "on": DIAG},
        ],
        contents={"ISpeed": ELEVEN, "IDiag": TEN_OPERATIONS},
    )
    assert answers[3]["rows"] == _rows(ELEVEN)
    switched = answers[4]
    assert switched["panelOpen"] is True
    assert switched["heading"] == "Chassis.rDiag: require IDiag (client-server)"
    assert switched["rows"] == _rows(TEN_OPERATIONS)
    assert DIAG in switched["lit"] and CAPPED not in switched["lit"]


def test_a_click_from_an_open_list_to_a_port_within_the_cap_closes_the_list_and_pins_that_port(
    node: str, tmp_path: Path
) -> None:
    """A port within the cap is no owner of a list: the click closes the open one and pins what that port lights."""
    answers = _answers(
        node, tmp_path, "ibd-Chassis", [{"do": "click", "on": CAPPED}, {"do": "click", "on": DIAG}], contents=WALK_MAP
    )
    assert answers[1]["panelOpen"] is True
    moved = answers[2]
    assert moved["panelOpen"] is False and moved["rows"] == []
    direct = _answers(node, tmp_path, "ibd-Chassis", [{"do": "click", "on": DIAG}], contents=WALK_MAP)
    assert DIAG in moved["lit"] and CAPPED not in moved["lit"]
    assert moved["lit"] == direct[1]["lit"], "as if the list had never been open"


def test_a_count_square_without_a_list_pins_and_opens_no_panel(node: str, tmp_path: Path) -> None:
    """A count square whose ``<desc>`` is absent (a bundle listing no connector) is no owner either."""
    square = "count|Sensor -> Controller"
    answers = _answers(
        node, tmp_path, "overview-Chassis", [{"do": "click", "on": square}], unlisted=frozenset({square})
    )
    assert answers[1]["panelOpen"] is False and answers[1]["rows"] == []
    assert answers[1]["lit"] != []


def test_a_line_break_in_a_member_name_splits_its_row(node: str, tmp_path: Path) -> None:
    """A known limit: the list is one text split at line breaks, so a name holding one becomes two rows."""
    lines = (*ELEVEN[:10], "odd\nname: uint8")
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "click", "on": CAPPED}], contents={"ISpeed": lines})
    assert answers[1]["rows"][-2:] == [["odd", None], ["name: uint8", None]]
    assert len(answers[1]["rows"]) == len(lines) + 1


def test_a_member_row_with_a_tab_is_shown_whole(node: str, tmp_path: Path) -> None:
    lines = (*ELEVEN[:10], "odd\tname: uint8")
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "click", "on": CAPPED}], contents={"ISpeed": lines})
    assert answers[1]["rows"][-1] == ["odd\tname: uint8", None], "a tab is a connector row's alone"


def test_a_count_square_still_lists_its_connectors_with_their_notes(node: str, tmp_path: Path) -> None:
    answers = _answers(node, tmp_path, "overview-Chassis", [{"do": "click", "on": "count|Sensor -> Controller"}])
    assert answers[1]["panelOpen"] is True
    assert answers[1]["heading"] == "2 connectors Sensor -> Controller"
    assert answers[1]["rows"] == [
        ["raw_link: Sensor.pRaw -> Controller.rRaw — ISpeed", None],
        ["speed_link: Sensor.pSpeed -> Controller.rSpeed — ISpeed", "The filtered speed into the controller."],
    ]


def test_the_search_finds_a_member_past_the_cap_through_the_ports_list(node: str, tmp_path: Path) -> None:
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "search", "text": "e11: uint8"}], contents=WALK_MAP)
    assert CAPPED in answers[1]["hits"] and DESCRIBED in answers[1]["hits"]
    assert DIAG not in answers[1]["hits"]


def test_the_search_finds_a_data_messages_lines_up_to_the_cap_and_none_past_it(node: str, tmp_path: Path) -> None:
    """A message's lines are its title's, read into the search; past the cap there is no list to read."""
    data = ["data|pSpeed -> rSpeed : ISpeed", "data|pLoop -> rLoop : ISpeed"]
    found = _answers(node, tmp_path, "sequence-speed_cycle", [{"do": "search", "text": "speed: uint8"}])
    capped = _answers(
        node,
        tmp_path,
        "sequence-speed_cycle",
        [{"do": "search", "text": "e8: uint8"}, {"do": "search", "text": "e9: uint8"}],
        contents=WALK_MAP,
    )
    assert set(data) <= set(found[1]["hits"])
    assert set(data) <= set(capped[1]["hits"]), "the eighth line is in the title"
    assert capped[2]["hits"] == [], "the ninth is behind 'and 3 more', and a message has no list"


def test_a_capped_ports_tooltip_puts_the_click_line_tenth_before_the_description(node: str, tmp_path: Path) -> None:
    answers = _answers(node, tmp_path, "ibd-Chassis", [{"do": "over", "on": DESCRIBED}], contents=WALK_MAP)
    lines = answers[1]["tip"].split("\n")
    assert lines[0] == "Sensor.pSpeed: provide ISpeed (sender-receiver)"
    assert lines[1:9] == [f"· e{index}: uint8" for index in range(1, 9)]
    assert lines[9] == "and 3 more — click the port to list all 11"
    assert lines[10:] == ["The filtered speed."]


def test_an_unknown_step_is_an_error_not_a_click(node: str, tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="does not know the step"):
        _answers(node, tmp_path, "ibd-Vehicle", [{"do": "clack", "on": LINE}])


# The host seam, the open gesture, the selection, the reveal and the hit band, over the generator's own pages, each
# carrying the model block the page script keys by ordinal.

COLLISIONS_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagram_collisions"


@pytest.fixture(scope="module")
def generated(node: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    """Each model's generated pages, by view id: the ``diagrams`` and the collision fixture."""
    pages: dict[str, dict[str, str]] = {}
    for name, model_root in (("diagrams", FIXTURE_DIR / "model"), ("collisions", COLLISIONS_DIR / "model")):
        views_root = tmp_path_factory.mktemp(f"views-{name}")
        diagrams.generate_diagrams(str(model_root), str(views_root), "P", node=node)
        pages[name] = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted(views_root.glob("*.html"), key=lambda path: path.as_posix())
            if path.name != page.INDEX_PAGE
        }
    return pages


def _blocks(page_text: str) -> dict[str, str]:
    found = {}
    for block_id in (page.LINKS_SCRIPT_ID, page.UP_SCRIPT_ID, page.MODEL_SCRIPT_ID):
        match = re.search(rf'<script type="application/json" id="{block_id}">(.*?)</script>', page_text)
        assert match is not None, block_id
        found[block_id] = match.group(1)
    return found


def _drawn(page_text: str) -> list[ET.Element]:
    """The page's ``[data-role]`` elements in document order: the ordinals the model block keys."""
    markup = re.search(r"<svg\b.*?</svg>", page_text, re.DOTALL)
    assert markup is not None
    return [element for element in ET.fromstring(markup.group(0)).iter() if element.get("data-role") is not None]


def _ordinal(page_text: str, role: str, name: str, owner: str | None = None) -> int:
    (found,) = [
        index
        for index, element in enumerate(_drawn(page_text))
        if element.get("data-role") == role
        and element.get("data-name") == name
        and (owner is None or element.get("data-owner") == owner)
    ]
    return found


def _model(page_text: str) -> dict[str, dict[str, Any]]:
    return json.loads(_blocks(page_text)[page.MODEL_SCRIPT_ID])


def _page_answers(
    node: str,
    tmp_path: Path,
    page_text: str,
    steps: list[dict[str, Any]],
    *,
    host: dict[str, Any] | None = None,
    fragment: str = "",
    model: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the shipped script over a generated page's own SVG and blocks; *host* is the stub host's state.

    *model* replaces the page's model block (a refused entry no fixture model produces).
    """
    markup = re.search(r"<svg\b.*?</svg>", page_text, re.DOTALL)
    assert markup is not None
    blocks = _blocks(page_text)
    if model is not None:
        blocks[page.MODEL_SCRIPT_ID] = json.dumps(dict(model))
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8", newline="\n")
    payload: dict[str, Any] = {
        "script": page.PAGE_SCRIPT,
        "svg": _tree(ET.fromstring(markup.group(0))),
        "blocks": blocks,
        "hash": f"#{fragment}" if fragment else "",
        "steps": steps,
    }
    if host is not None:
        payload["host"] = {"state": host}
    completed = subprocess.run(
        [node, str(harness)], input=json.dumps(payload), capture_output=True, encoding="utf-8", check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_every_sub_element_gets_one_hit_band_and_no_ordinal_moves(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """One hit band per port, line and message, carrying no role; boxes and frames get none."""
    for model_name, pages in generated.items():
        for view, page_text in pages.items():
            drawn = _drawn(page_text)
            (loaded,) = _page_answers(node, tmp_path, page_text, [])
            assert loaded["drawnCount"] == len(drawn), (model_name, view)
            expected = [1 if element.get("data-role") in page.HIT_BAND_ROLES else 0 for element in drawn]
            assert loaded["bands"] == expected, (model_name, view)


def test_the_hit_band_is_the_same_in_a_browser(node: str, tmp_path: Path, generated: dict[str, dict[str, str]]) -> None:
    """The band is a page rule: no host is needed for it."""
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    answers = _page_answers(node, tmp_path, page_text, [{"do": "over", "at": port, "part": "band"}])
    assert answers[0]["bands"][port] == 1
    assert port in answers[1]["litAt"], "the band lights its port"
    assert answers[1]["tip"].startswith("Sensor.pSpeed: provide ISpeed")


def test_the_walks_press_on_the_band_of_a_port_opens_the_port_not_its_box(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """A press on the band beside ``Sensor.pSpeed``'s square opens the port.

    It proves the band carries no role and climbs to its port; it does not show that the band paints above the
    ``Sensor`` box (the stub hit-tests nothing). Only the real tab and the hit geometry test in ``test_diagram_page.py``
    reach that.
    """
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    box = _ordinal(page_text, "member", "Sensor")
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "click", "at": port, "part": "band", "meta": True},
            {"do": "click", "at": box, "part": "first", "meta": True},
        ],
        host={},
    )
    assert answers[1]["posted"] == [["open", "swc/chassis/Sensor.yaml", "ports/pSpeed"]]
    assert answers[2]["posted"][-1] == ["open", "swc/chassis/Sensor.yaml", None], "the box itself opens its line 1"


def test_without_a_host_the_page_does_what_it_did_and_never_selects(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """In a browser: the links and the reload go through ``window.location``; a modified click is a click."""
    vehicle = generated["diagrams"]["ibd-Vehicle"]
    chassis_box = _ordinal(vehicle, "member", "Chassis")
    logger = generated["diagrams"]["ibd-Chassis"]
    answers = _page_answers(
        node,
        tmp_path,
        vehicle,
        [{"do": "click", "at": chassis_box, "part": "first", "meta": True}, {"do": "button", "id": "reload"}],
    )
    assert answers[1]["href"] == "ibd-Chassis.html" and answers[1]["posted"] == []
    assert answers[2]["reloads"] == 1
    box = _ordinal(logger, "member", "Logger")
    alone = _page_answers(node, tmp_path, logger, [{"do": "click", "at": box, "part": "first"}])
    assert alone[1]["selection"] is None and alone[1]["selectedAt"] == [] and alone[1]["openButton"] is False


def test_with_a_host_the_links_the_way_up_and_the_reload_go_to_the_host(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    vehicle = generated["diagrams"]["ibd-Vehicle"]
    chassis_box = _ordinal(vehicle, "member", "Chassis")
    answers = _page_answers(
        node,
        tmp_path,
        vehicle,
        [{"do": "click", "at": chassis_box, "part": "first"}, {"do": "button", "id": "reload"}],
        host={},
    )
    assert answers[1]["posted"] == [["navigate", "ibd-Chassis.html"]] and answers[1]["href"] == ""
    assert answers[2]["posted"][-1] == ["reload"] and answers[2]["reloads"] == 0
    up = _page_answers(node, tmp_path, generated["diagrams"]["ibd-Chassis"], [{"do": "click", "on": "up"}], host={})
    assert up[1]["posted"] == [["navigate", "ibd-Vehicle.html#Chassis"]]


def test_a_modified_click_on_a_member_composition_opens_its_file_and_stays_on_the_page(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    vehicle = generated["diagrams"]["ibd-Vehicle"]
    chassis_box = _ordinal(vehicle, "member", "Chassis")
    answers = _page_answers(
        node, tmp_path, vehicle, [{"do": "click", "at": chassis_box, "part": "first", "meta": True}], host={}
    )
    assert answers[1]["posted"] == [["open", "compositions/Chassis.yaml", None]]
    assert answers[1]["href"] == ""


def test_every_drawn_element_posts_its_own_entry_and_a_shape_with_none_posts_nothing(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """Every Ctrl/Cmd+clicked ``[data-role]`` element posts exactly its entry's file and member; one with none, nothing.

    The collision fixture's same-named elements are told apart by ordinal.
    """
    for model_name, pages in generated.items():
        for view, page_text in pages.items():
            entries = _model(page_text)
            drawn = _drawn(page_text)
            steps = [{"do": "click", "at": index, "part": "first", "meta": True} for index in range(len(drawn))]
            answers = _page_answers(node, tmp_path, page_text, steps, host={})
            for index in range(len(drawn)):
                before, after = answers[index]["posted"], answers[index + 1]["posted"]
                entry = entries.get(str(index))
                if entry is None:
                    assert after == before, (model_name, view, index)
                else:
                    assert after == [*before, ["open", entry["file"], entry["member"]]], (model_name, view, index)


def test_the_collision_fixtures_colliding_elements_open_what_tells_them_apart(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    pages = generated["collisions"]

    def opened(view: str, role: str, name: str) -> list[list[str | None]]:
        page_text = pages[view]
        found = [
            index
            for index, element in enumerate(_drawn(page_text))
            if (element.get("data-role"), element.get("data-name")) == (role, name)
        ]
        assert len(found) == 2, (view, role, name)
        steps = [{"do": "click", "at": index, "part": "first", "meta": True} for index in found]
        return _page_answers(node, tmp_path, page_text, steps, host={})[-1]["posted"]

    assert sorted(opened("nesting", "member", "Sensor")) == [
        ["open", "swc/collisions/SensorA.yaml", None],
        ["open", "swc/collisions/SensorB.yaml", None],
    ]
    assert opened("deployment", "member", "SensorA") == [["open", "swc/collisions/SensorA.yaml", None]] * 2
    assert sorted(opened("sequence-boot", "activation", "Init")) == [
        ["open", "swc/collisions/SensorA.yaml", "runnables/Init"],
        ["open", "swc/collisions/SensorB.yaml", "runnables/Init"],
    ]


@pytest.mark.parametrize(
    ("view", "role", "name", "bar"),
    [
        ("ibd-Chassis", "frame", "Chassis", "composition Chassis"),
        ("ibd-Chassis", "member", "Logger", "Logger (component)"),
        ("deployment", "member", "Sensor", "Sensor (component)"),
    ],
    ids=["the-frame", "an-id-less-ibd-box", "a-deployment-box"],
)
def test_a_plain_click_selects_an_element_no_pin_reaches_and_open_in_model_opens_it(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]], view: str, role: str, name: str, bar: str
) -> None:
    page_text = generated["diagrams"][view]
    ordinals = [
        index
        for index, element in enumerate(_drawn(page_text))
        if (element.get("data-role"), element.get("data-name")) == (role, name)
    ]
    target = ordinals[0]
    entry = _model(page_text)[str(target)]
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "click", "at": target, "part": "first"},
            {"do": "over", "at": target, "part": "first", "x": 10, "y": 10},
            {"do": "button", "id": "open-in-model"},
            {"do": "click"},
        ],
        host={},
    )
    clicked, hovered, pressed, emptied = answers[1:]
    assert clicked["selectedAt"] == [target] and clicked["litAt"] == []
    assert clicked["openButton"] is True and clicked["posted"] == []
    assert clicked["selection"] == bar, "the bar shows the first line of the element's title"
    assert hovered["tip"] is not None
    assert pressed["posted"] == [["open", entry["file"], entry["member"]]]
    assert pressed["tip"] is None, "the tooltip is hidden when open is posted"
    assert emptied["selection"] is None and emptied["selectedAt"] == []


def test_a_pinned_port_is_selected_by_its_pin_and_offers_open_in_model(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    answers = _page_answers(
        node, tmp_path, page_text, [{"do": "click", "at": port}, {"do": "button", "id": "open-in-model"}], host={}
    )
    assert port in answers[1]["litAt"] and answers[1]["selectedAt"] == [], "pinned as before, no outline class"
    assert answers[1]["selection"] == "Sensor.pSpeed: provide ISpeed (sender-receiver)"
    assert answers[2]["posted"] == [["open", "swc/chassis/Sensor.yaml", "ports/pSpeed"]]


# With a host the status line never repeats the selection: it is empty while anything is selected
# and shows what is lit once the selection is dropped. A browser's status line is the reference
# for "what is lit": the same steps without a host, where nothing is ever selected.


def _hosted_and_browsed(
    node: str, tmp_path: Path, page_text: str, steps: list[dict[str, Any]], **options: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        _page_answers(node, tmp_path, page_text, steps, host={}, **options),
        _page_answers(node, tmp_path, page_text, steps, **options),
    )


def test_with_a_host_a_clicked_connector_is_shown_by_the_bar_and_the_status_line_stays_empty(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    steps: list[dict[str, Any]] = [{"do": "click", "at": connector}]
    hosted, browsed = (answers[1] for answers in _hosted_and_browsed(node, tmp_path, page_text, steps))
    assert connector in hosted["litAt"] and hosted["openButton"] is True
    assert hosted["selection"] and hosted["status"] == "", "the bar shows the selection, the status line does not"
    assert browsed["selection"] is None and hosted["selection"] in browsed["status"], "a browser keeps the status line"


def test_with_a_host_a_pinned_selection_keeps_the_status_line_empty_while_the_tooltip_follows_the_pointer(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    other = _ordinal(page_text, "connector", "raw_link")
    hovered_alone = _page_answers(node, tmp_path, page_text, [{"do": "over", "at": other}])[1]
    answers = _page_answers(
        node, tmp_path, page_text, [{"do": "click", "at": connector}, {"do": "over", "at": other}], host={}
    )
    pinned_over = answers[2]
    assert connector in pinned_over["litAt"] and other not in pinned_over["litAt"], "the pin holds"
    assert pinned_over["status"] == "" and pinned_over["selection"]
    assert pinned_over["tip"] == hovered_alone["status"] != "", "the tooltip shows the other line"


def test_with_a_host_a_selected_frame_keeps_the_status_line_empty_while_the_pointer_lights_a_line(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    frame = _ordinal(page_text, "frame", "Chassis")
    connector = _ordinal(page_text, "connector", "speed_link")
    steps: list[dict[str, Any]] = [{"do": "click", "at": frame}, {"do": "over", "at": connector}]
    hosted, browsed = _hosted_and_browsed(node, tmp_path, page_text, steps)
    selected, hovered = hosted[1:]
    assert selected["selectedAt"] == [frame] and selected["litAt"] == [], "a frame is selected, not pinned"
    assert hovered["selectedAt"] == [frame] and connector in hovered["litAt"], "the hover lights the line"
    assert hovered["status"] == "" and browsed[2]["status"] != "", "a browser shows the lit line's title"


def test_with_a_host_a_refused_entry_on_a_pinned_line_leaves_the_status_line_empty(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    model = {**_model(page_text), str(connector): {"refused": "no-file"}}
    clicked = _page_answers(node, tmp_path, page_text, [{"do": "click", "at": connector}], host={}, model=model)[1]
    assert connector in clicked["litAt"]
    assert clicked["selection"] == "speed_link is declared in no file — Check Model reports it"
    assert clicked["openButton"] is False and clicked["status"] == ""


def test_with_a_host_a_revealed_connector_leaves_the_status_line_empty(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    request = {**_model(page_text)[str(connector)], "name": "speed_link"}
    revealed = _page_answers(
        node, tmp_path, page_text, [{"do": "message", "type": "reveal", "data": request}], host={}
    )[1]
    assert connector in revealed["litAt"] and revealed["openButton"] is True
    assert revealed["status"] == ""


def test_with_a_host_next_keeps_the_status_line_empty(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["collisions"]["deployment"]
    request = {"file": "swc/collisions/SensorA.yaml", "member": None, "name": "SensorA"}
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [{"do": "message", "type": "reveal", "data": request}, {"do": "button", "id": "selection-next"}],
        host={},
    )
    for answer in answers[1:]:
        assert answer["nextButton"] is True and answer["status"] == ""


def test_with_a_host_a_click_on_a_line_with_no_entry_after_a_selection_shows_the_lines_title(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["overview-Chassis"]
    box = _ordinal(page_text, "member", "Sensor")
    bundle = _ordinal(page_text, "bundle", "Sensor -> Controller")
    steps: list[dict[str, Any]] = [
        {"do": "click", "at": box, "part": "first"},
        {"do": "click", "at": bundle, "part": "first"},
    ]
    hosted, browsed = _hosted_and_browsed(node, tmp_path, page_text, steps)
    assert hosted[1]["selection"] and hosted[1]["status"] == "", "the box is selected"
    assert hosted[2]["selection"] is None and bundle in hosted[2]["litAt"]
    assert hosted[2]["status"] == browsed[2]["status"] != "", "the line is pinned, not selected"


def test_with_a_host_a_click_on_empty_space_returns_the_status_line_to_what_the_pointer_lights(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    other = _ordinal(page_text, "connector", "raw_link")
    steps: list[dict[str, Any]] = [{"do": "click", "at": connector}, {"do": "click"}, {"do": "over", "at": other}]
    hosted, browsed = _hosted_and_browsed(node, tmp_path, page_text, steps)
    assert hosted[2]["selection"] is None and hosted[2]["status"] == ""
    assert hosted[3]["status"] == browsed[3]["status"] != ""


@pytest.mark.parametrize(
    "closing",
    [{"do": "click", "on": CAPPED}, {"do": "close"}],
    ids=["second-click", "close-button"],
)
def test_with_a_host_closing_a_ports_list_returns_the_status_line_to_what_is_lit(
    node: str, tmp_path: Path, closing: dict[str, Any]
) -> None:
    steps: list[dict[str, Any]] = [{"do": "click", "on": CAPPED}, closing, {"do": "over", "on": DIAG}]
    hosted = _answers(node, tmp_path, "ibd-Chassis", steps, contents=WALK_MAP, host={})
    browsed = _answers(node, tmp_path, "ibd-Chassis", steps, contents=WALK_MAP)
    opened, closed, hovered = hosted[1:]
    assert opened["selection"] and opened["status"] == ""
    assert closed["selection"] is None and closed["status"] == "", "nothing is lit"
    assert DIAG in hovered["lit"] and hovered["status"] == browsed[3]["status"] != ""


def test_with_a_host_a_reveal_that_finds_nothing_returns_the_status_line_to_what_is_lit(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    connector = _ordinal(page_text, "connector", "speed_link")
    other = _ordinal(page_text, "connector", "raw_link")
    absent = {"file": "swc/chassis/DiagServer.yaml", "member": None, "name": "DiagServer"}
    steps: list[dict[str, Any]] = [
        {"do": "click", "at": connector},
        {"do": "message", "type": "reveal", "data": absent},
        {"do": "over", "at": other},
    ]
    hosted = _page_answers(node, tmp_path, page_text, steps, host={})
    browsed = _page_answers(node, tmp_path, page_text, [{"do": "over", "at": other}])
    assert hosted[1]["status"] == ""
    assert hosted[2]["selection"] == "DiagServer is not on this diagram as last generated"
    assert hosted[2]["litAt"] == [] and hosted[2]["status"] == ""
    assert hosted[3]["status"] == browsed[1]["status"] != ""


def test_with_a_host_an_arrival_pins_its_member_shows_its_title_and_selects_nothing(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Vehicle"]
    chassis = _ordinal(page_text, "member", "Chassis")
    hosted = _page_answers(node, tmp_path, page_text, [], host={"fragment": "Chassis"})[0]
    browsed = _page_answers(node, tmp_path, page_text, [], fragment="Chassis")[0]
    assert chassis in hosted["litAt"] and hosted["selection"] is None
    assert hosted["status"] == browsed["status"] != ""
    assert hosted["status"].startswith("Chassis")


@pytest.mark.parametrize(
    ("refusal", "line"),
    [
        ("several-files", "Sensor is declared in more than one file \u2014 Check Model names them"),
        ("no-file", "Sensor is declared in no file \u2014 Check Model reports it"),
    ],
)
def test_a_refused_entry_opens_nothing_and_says_why(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]], refusal: str, line: str
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    box = _ordinal(page_text, "member", "Sensor")
    model = {**_model(page_text), str(box): {"refused": refusal}}
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "click", "at": box, "part": "first", "meta": True},
            {"do": "click"},
            {"do": "click", "at": box, "part": "first"},
            {"do": "button", "id": "open-in-model"},
        ],
        host={},
        model=model,
    )
    gesture, _empty, plain, button = answers[1:]
    assert gesture["posted"] == [] and gesture["selection"] == line and gesture["openButton"] is False
    assert plain["selection"] == line and plain["openButton"] is False
    assert button["posted"] == []


def test_a_shape_with_no_entry_is_never_selected(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["overview-Chassis"]
    bundle = _ordinal(page_text, "bundle", "Sensor -> Controller")
    assert str(bundle) not in _model(page_text)
    answers = _page_answers(
        node, tmp_path, page_text, [{"do": "click", "at": bundle, "part": "first", "meta": True}], host={}
    )
    assert answers[1]["posted"] == [] and answers[1]["selection"] is None
    assert bundle in answers[1]["litAt"], "a plain click's pin is what is left"


def test_the_tooltip_hides_when_the_gesture_posts_open(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [{"do": "over", "at": port}, {"do": "click", "at": port, "meta": True}, {"do": "move", "at": port}],
        host={},
    )
    assert answers[1]["tip"] is not None
    assert answers[2]["tip"] is None and answers[2]["posted"]


def test_after_an_open_the_tooltip_stays_hidden_until_the_pointer_moves_on(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """After an open the tooltip stays hidden until the pointer moves on.

    Chromium re-sends ``pointerover`` at the release, then the window blurs; the stub replays that order.
    """
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    at = {"at": port, "x": 5, "y": 5}
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "over", **at},
            {"do": "click", **at, "meta": True},
            {"do": "over", **at},
            {"do": "blur"},
            {"do": "move", **at},
            {"do": "move", "at": port, "x": 9, "y": 5},
        ],
        host={},
    )
    assert answers[1]["tip"] is not None
    assert answers[2]["posted"] == [["open", "swc/chassis/Sensor.yaml", "ports/pSpeed"]]
    assert [answer["tip"] for answer in answers[2:6]] == [None, None, None, None]
    assert answers[6]["tip"] == answers[1]["tip"], "a fresh move shows it again"


def test_with_a_host_the_windows_blur_hides_the_tooltip_until_a_move_and_without_one_nothing_holds_it(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """The blur's hold is the host's: in a browser the page does what it did before."""
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    steps = [
        {"do": "over", "at": port, "x": 5, "y": 5},
        {"do": "blur"},
        {"do": "over", "at": port, "x": 5, "y": 5},
        {"do": "move", "at": port, "x": 6, "y": 5},
    ]
    hosted = _page_answers(node, tmp_path, page_text, steps, host={})
    assert [answer["tip"] is None for answer in hosted] == [True, False, True, True, False]
    browser = _page_answers(node, tmp_path, page_text, steps)
    assert [answer["tip"] is None for answer in browser] == [True, False, False, False, False]


def test_the_fragment_comes_from_the_host_not_the_address(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Vehicle"]
    chassis = _ordinal(page_text, "member", "Chassis")
    diag = _ordinal(page_text, "member", "Diag")
    hosted = _page_answers(node, tmp_path, page_text, [], host={"fragment": "Chassis"}, fragment="Diag")
    assert chassis in hosted[0]["litAt"] and diag not in hosted[0]["litAt"]


# A reveal selects the element exactly as a click on it does and centres it; the search box and its marks are not
# touched, and the status line is empty as for any selection.

CONTROLLER = {"file": "swc/chassis/Controller.yaml", "member": None, "name": "Controller"}


def _centre(answer: dict[str, Any]) -> tuple[float, float]:
    x, y, width, height = (float(part) for part in answer["viewBox"].split())
    return (round(x + width / 2, 2), round(y + height / 2, 2))


def _box_centre(page_text: str, ordinal: int) -> tuple[float, float]:
    rect = next(element for element in _drawn(page_text)[ordinal].iter() if element.tag == f"{SVG_NS}rect")
    x, y, width, height = (float(rect.get(name) or 0) for name in ("x", "y", "width", "height"))
    return (round(x + width / 2, 2), round(y + height / 2, 2))


def _untouched_search(answer: dict[str, Any]) -> bool:
    return answer["hitsAt"] == [] and answer["currentAt"] == [] and answer["value"] == "" and answer["count"] == ""


def test_a_reveal_selects_the_element_as_a_click_does_centres_it_and_leaves_the_search_alone(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    box = _ordinal(page_text, "member", "Controller")
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "focussearch"},
            {"do": "message", "type": "reveal", "data": CONTROLLER},
            {"do": "pagekey", "key": "+"},
        ],
        host={},
    )
    revealed, zoomed = answers[2], answers[3]
    clicked = _page_answers(node, tmp_path, page_text, [{"do": "click", "at": box, "part": "first"}], host={})[1]
    for key in ("selectedAt", "litAt", "selection", "openButton", "status"):
        assert revealed[key] == clicked[key], key
    assert revealed["selectedAt"] == [box] and revealed["openButton"] is True
    assert revealed["selection"].startswith("Controller") and revealed["nextButton"] is False
    assert _untouched_search(revealed) and revealed["status"] == "", "no marks, no count, no Shown from the model"
    assert revealed["placeholder"] == "Search names"
    assert answers[1]["focused"] == "search" and revealed["focused"] is None, "the box gives up the focus"
    assert _centre(revealed) == _box_centre(page_text, box), "centred on the element"
    revealed_width = float(revealed["viewBox"].split()[2])
    assert float(zoomed["viewBox"].split()[2]) < revealed_width, "+ zooms in after the jump (Amendment (c))"


def test_a_reveal_of_a_member_selects_the_members_drawing_and_falls_back_to_the_element(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    port = _ordinal(page_text, "port", "pSpeed", "Sensor")
    box = _ordinal(page_text, "member", "Sensor")
    member = {"file": "swc/chassis/Sensor.yaml", "member": "ports/pSpeed", "name": "Sensor.pSpeed"}
    undrawn = {"file": "swc/chassis/Sensor.yaml", "member": "runnables/SensorRun", "name": "Sensor.SensorRun"}
    absent = {"file": "swc/chassis/DiagServer.yaml", "member": None, "name": "DiagServer"}
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "message", "type": "reveal", "data": member},
            {"do": "message", "type": "reveal", "data": undrawn},
            {"do": "message", "type": "reveal", "data": absent},
        ],
        host={},
    )
    on_port, on_box, nowhere = answers[1:]
    clicked = _page_answers(node, tmp_path, page_text, [{"do": "click", "at": port}], host={})[1]
    assert on_port["litAt"] == clicked["litAt"] and port in on_port["litAt"], "pinned as a click pins a port"
    assert on_port["selection"] == "Sensor.pSpeed: provide ISpeed (sender-receiver)" and on_port["openButton"]
    assert on_box["selectedAt"] == [box] and on_box["litAt"] == [], "a member no view draws: the element"
    assert nowhere["selection"] == "DiagServer is not on this diagram as last generated"
    assert nowhere["openButton"] is False and nowhere["selectedAt"] == [] and nowhere["litAt"] == []
    assert all(_untouched_search(answer) for answer in answers[1:])


def test_a_reveal_handed_over_at_load_selects(node: str, tmp_path: Path, generated: dict[str, dict[str, str]]) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    logger = _ordinal(page_text, "member", "Logger")
    request = {"file": "swc/chassis/Logger.yaml", "member": None, "name": "Logger"}
    loaded = _page_answers(node, tmp_path, page_text, [], host={"reveal": request})[0]
    assert loaded["selectedAt"] == [logger] and loaded["openButton"] is True and _untouched_search(loaded)


def test_an_element_drawn_twice_selects_the_first_and_next_goes_round(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["collisions"]["deployment"]
    drawings = [
        index
        for index, element in enumerate(_drawn(page_text))
        if (element.get("data-role"), element.get("data-name")) == ("member", "SensorA")
    ]
    assert len(drawings) == 2, "the collision fixture's component run by two tasks"
    request = {"file": "swc/collisions/SensorA.yaml", "member": None, "name": "SensorA"}
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "message", "type": "reveal", "data": request},
            {"do": "button", "id": "selection-next"},
            {"do": "button", "id": "selection-next"},
            {"do": "click", "at": drawings[1], "part": "first"},
        ],
        host={},
    )
    first, second, round_again, clicked = answers[1:]
    for answer, at, index in ((first, drawings[0], 1), (second, drawings[1], 2), (round_again, drawings[0], 1)):
        assert answer["selectedAt"] == [at] and answer["openButton"] and answer["nextButton"]
        assert answer["selection"].endswith(f"\u2014 {index} of 2 on this diagram")
        assert _centre(answer) == _box_centre(page_text, at)
    assert clicked["selectedAt"] == [drawings[1]] and clicked["nextButton"] is False
    assert "on this diagram" not in clicked["selection"], "a click chooses for itself"


def test_a_modified_click_after_a_reveal_of_a_twice_drawn_element_drops_the_count_and_next(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    """A Ctrl/Cmd+click that opens after the reveal of a twice-drawn element drops the count and **Next**.

    It never empties the list while leaving "1 of 2" and a dead **Next** on the bar.
    """
    page_text = generated["collisions"]["deployment"]
    drawings = [
        index
        for index, element in enumerate(_drawn(page_text))
        if (element.get("data-role"), element.get("data-name")) == ("member", "SensorA")
    ]
    request = {"file": "swc/collisions/SensorA.yaml", "member": None, "name": "SensorA"}
    for opened in (drawings[1], drawings[0]):
        answers = _page_answers(
            node,
            tmp_path,
            page_text,
            [
                {"do": "message", "type": "reveal", "data": request},
                {"do": "click", "at": opened, "part": "first", "meta": True},
                {"do": "button", "id": "selection-next"},
            ],
            host={},
        )
        revealed, after_open, after_next = answers[1:]
        assert revealed["nextButton"] is True and revealed["selection"].endswith("1 of 2 on this diagram")
        assert after_open["posted"] == [["open", "swc/collisions/SensorA.yaml", None]]
        assert after_open["selectedAt"] == [drawings[0]] and after_open["openButton"] is True
        assert after_open["nextButton"] is False
        assert "on this diagram" not in after_open["selection"]
        assert after_open["selection"] == revealed["selection"].split(" \u2014 1 of 2")[0]
        assert after_next == after_open, "Next is hidden; nothing moves"


def test_a_click_after_a_reveal_just_changes_the_selection(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]]
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    logger = _ordinal(page_text, "member", "Logger")
    port = _ordinal(page_text, "port", "rLog", "Logger")
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [
            {"do": "message", "type": "reveal", "data": CONTROLLER},
            {"do": "click", "at": logger, "part": "first"},
            {"do": "click", "at": port},
            {"do": "click"},
        ],
        host={},
    )
    on_logger, on_port, emptied = answers[2:]
    assert on_logger["selectedAt"] == [logger] and on_logger["openButton"] is True
    assert port in on_port["litAt"] and on_port["selectedAt"] == []
    assert emptied["selection"] is None and emptied["selectedAt"] == [] and emptied["litAt"] == []


@pytest.mark.parametrize("before", [False, True], ids=["after", "before"])
def test_a_typed_search_and_a_reveal_do_not_touch_each_other(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]], before: bool
) -> None:
    """A search typed after a reveal is a normal search; one typed before stands through the reveal."""
    page_text = generated["diagrams"]["ibd-Chassis"]
    box = _ordinal(page_text, "member", "Controller")
    reveal = {"do": "message", "type": "reveal", "data": CONTROLLER}
    search = {"do": "search", "text": "Controller"}
    steps = [search, reveal] if before else [reveal, search]
    steps += [{"do": "click"}, {"do": "key", "key": "Escape"}]
    answers = _page_answers(node, tmp_path, page_text, steps, host={})
    plain = _page_answers(node, tmp_path, page_text, [search], host={})[1]
    assert len(plain["hitsAt"]) > 7, "a typed name finds every drawn name, the connectors too"
    typed = answers[2]
    assert typed["hitsAt"] == plain["hitsAt"] and typed["count"] == plain["count"]
    assert typed["selectedAt"] == [box], "the reveal's selection stands beside the search"
    clicked, escaped = answers[3:]
    assert clicked["hitsAt"] == plain["hitsAt"] and clicked["value"] == "Controller", "a click keeps the search"
    assert escaped["hitsAt"] == [] and escaped["value"] == ""


@pytest.mark.parametrize("hosted", [True, False], ids=["tab", "browser"])
def test_a_typed_search_stays_through_clicks(
    node: str, tmp_path: Path, generated: dict[str, dict[str, str]], hosted: bool
) -> None:
    page_text = generated["diagrams"]["ibd-Chassis"]
    logger = _ordinal(page_text, "member", "Logger")
    answers = _page_answers(
        node,
        tmp_path,
        page_text,
        [{"do": "search", "text": "rraw"}, {"do": "click"}, {"do": "click", "at": logger, "part": "first"}],
        host={} if hosted else None,
    )
    typed = answers[1]
    assert typed["hitsAt"] and typed["value"] == "rraw"
    for answer in answers[2:]:
        assert answer["hitsAt"] == typed["hitsAt"] and answer["currentAt"] == typed["currentAt"]
        assert answer["value"] == "rraw" and answer["count"] == typed["count"]
