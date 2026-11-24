"""The viewer pages: one HTML page per view with its SVG inlined, and the index over them.

The SVG is inlined byte for byte rather than loaded, because the search needs its DOM. The shared script works on
the page's own DOM and fetches nothing: pan and zoom, search over every ``<title>`` and ``<desc>`` list, reload,
click-through to a member composition's page, the list panel of a bundle's connectors or a capped port's members,
highlight of a line with its ends, its own tooltip, and the way up to a parent's page.

The model block (:data:`MODEL_SCRIPT_ID`) maps a drawn element's ordinal among the inlined SVG's
``[data-role]`` elements to its model entry; :func:`strip_sources` removes the renderers' temporary
attributes so the ``.svg`` files carry no file name. The index carries the inverse
(:data:`ELEMENTS_SCRIPT_ID`); both hold model-root-relative names only.

In the VS Code tab the script reads ``window.swcraftHost``, which the extension's bridge defines
before the script runs: ``navigate(href)`` (the href as the page holds it), ``open(file, member)``
(always an entry of the page's own model block), ``reload()``, ``state`` (``{fragment, reveal}``)
and ``on(type, handler)`` (the one message, ``reveal``, ``{file, member, name}``); a browser has
none. With a host, Ctrl/Cmd+click opens an element's model file, a plain click selects it, and a
reveal is a jump that selects the element as a click would, never a search.

Deterministic and self-contained (no URL but sibling file names). Not reached: the script's
behaviour in a real browser (the panel's look, the tooltip's position and the drawn colours are not test-measured);
a pointer leaving the picture keeps the last line lit; and anything about the model.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from swcraft_tools.writer.diagrams.report import REPORT_FILE
from swcraft_tools.writer.diagrams.svg import SOURCE_ATTRIBUTE, ModelEntry, escape

#: The index page every view page links back to.
INDEX_PAGE = "index.html"

#: The ``id`` of the JSON block holding the click-through map a page's script reads.
LINKS_SCRIPT_ID = "swcraft-links"

#: The ``id`` of the JSON block holding the way up: parent composition -> its page at the member.
UP_SCRIPT_ID = "swcraft-up"

#: What separates two levels of the header's chain.
CHAIN_SEPARATOR = " \u203a "

#: The ``id`` of the JSON block mapping a drawn element's ordinal to what it opens in the model.
MODEL_SCRIPT_ID = "swcraft-model"

#: The ``id`` of the index's JSON block mapping a storage name to the views that draw it.
ELEMENTS_SCRIPT_ID = "swcraft-elements"

#: The class of the hit band the page script appends to every sub-element (a port, a line, a message).
HIT_BAND_CLASS = "hit-band"

#: The roles that get a hit band: what is drawn small on or beside a box, never a box or a frame.
HIT_BAND_ROLES = ("port", "connector", "delegation", "bundle", "activation", "data")

#: The view-id prefixes, in the order the index groups them, with the group heading.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Compositions", ("ibd-", "overview-")),
    ("Nesting", ("nesting",)),
    ("Deployment", ("deployment",)),
    ("Sequences", ("sequence-",)),
)

_ATTRIBUTE = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)="([^"]*)"')
_GROUP_TAG = re.compile(r"<g\b([^>]*)>")
_HEADING = re.compile(r"<svg\b[^>]*>\s*<title>(.*?)</title>", re.DOTALL)
_FRAME_TAG = re.compile(r'<g\b[^>]*\bdata-role="frame"[^>]*>')
#: Every start tag of an SVG text: no attribute value holds ``<`` or ``>`` (``svg.escape``).
_START_TAG = re.compile(r"<[A-Za-z][^<>]*>")
_ROLE_ATTRIBUTE = re.compile(r'\sdata-role="')
_SOURCE_ATTRIBUTE = re.compile(r"\s" + re.escape(SOURCE_ATTRIBUTE) + r'="([0-9]+)"')

PAGE_STYLE = """\
html, body { height: 100%; margin: 0; }
body { display: flex; flex-direction: column; font: 14px Helvetica, Arial, sans-serif; color: #222222; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 12px;
  border-bottom: 1px solid #cccccc; background: #f6f6f6; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
nav { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; }
nav a { color: #1a5fb4; text-decoration: none; }
nav a:hover { text-decoration: underline; }
.controls { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin-left: auto; }
.controls input { width: 14em; }
.controls output { min-width: 5em; color: #555555; }
main { flex: 1; min-height: 0; overflow: hidden; background: white; }
main > svg { display: block; width: 100%; height: 100%; touch-action: none; user-select: none; cursor: grab; }
main > svg.panning { cursor: grabbing; }
main > svg > g.labels { pointer-events: none; }
main > svg > g.labels > g.edge-count { pointer-events: auto; }
main > svg > g.labels > g.frame-up { pointer-events: auto; cursor: pointer; }
svg g.frame-up text { fill: #1a5fb4; }
svg g.frame-up:hover text { text-decoration: underline; }
nav .placed-in.highlight { outline: 2px solid #1a5fb4; outline-offset: 2px; }
svg .link { cursor: pointer; }
svg .link:hover > rect { stroke: #1a5fb4; stroke-width: 2px; }
svg .hit rect:not(.hit-band), svg .hit path:not(.hit-band), svg .hit circle, svg .hit line { stroke: #e8590c;
  stroke-width: 2.5px; }
svg .current rect:not(.hit-band), svg .current path:not(.hit-band), svg .current circle, svg .current line {
  stroke: #c92a2a; stroke-width: 4px; }
svg .highlight rect:not(.port-hit):not(.hit-band), svg .highlight path:not(.hit-band), svg .highlight circle,
  svg .highlight line { stroke: #1a5fb4; }
svg g.edge.highlight { stroke-width: 2.5px; }
svg g.edge.highlight path:not(.hit-band) { marker-end: url(#swcraft-arrow-lit); }
svg g.message.highlight path:not(.hit-band) { stroke-width: 2.5px; marker-end: url(#swcraft-arrow-open-lit); }
svg g.message.activation.highlight path:not(.hit-band) { marker-end: url(#swcraft-arrow-lit); }
svg g.port.highlight rect:not(.port-hit):not(.hit-band), svg g.port.highlight path:not(.hit-band),
  svg g.port.highlight circle, svg g.port.highlight line { stroke-width: 2px; }
svg .selected > rect:not(.hit-band), svg .selected > path:not(.hit-band), svg .selected > line { stroke: #1a5fb4;
  stroke-width: 2.5px; }
svg .hit-band { fill: none !important; stroke: transparent !important; stroke-width: 8px !important;
  vector-effect: non-scaling-stroke; pointer-events: stroke; }
svg g.member.highlight rect:not(.hit-band), svg g.lifeline.highlight rect:not(.hit-band) { stroke-width: 2px; }
#tip { position: fixed; left: 0; top: 0; z-index: 10; max-width: min(40em, calc(100vw - 8px));
  max-height: calc(100vh - 8px); overflow: hidden; overflow-wrap: anywhere; padding: 3px 6px; background: white;
  border: 1px solid #cccccc; border-radius: 3px; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2); font-size: 12px;
  white-space: pre-line; pointer-events: none; }
#tip[hidden] { display: none; }
#connectors { position: fixed; right: 12px; bottom: 32px; width: 38em; max-width: 90vw; max-height: 60vh; display: flex;
  flex-direction: column; background: white; border: 1px solid #cccccc; border-radius: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25); font-size: 13px; }
#connectors[hidden] { display: none; }
#connectors .panel-head { display: flex; gap: 8px; align-items: baseline; padding: 6px 8px; font-weight: 600;
  border-bottom: 1px solid #cccccc; background: #f6f6f6; }
#connectors .panel-head button { margin-left: auto; }
#connectors ol { margin: 0; padding: 6px 8px 8px 30px; overflow-y: auto; }
#connectors li { margin: 2px 0; overflow-wrap: anywhere; }
#connectors li .note { display: block; color: #555555; font-size: 12px; }
#selection { display: flex; gap: 8px; align-items: center; padding: 3px 12px; border-top: 1px solid #cccccc;
  background: #eef4fb; font-size: 13px; }
#selection[hidden] { display: none; }
#selection span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
footer { min-height: 17px; padding: 3px 12px; border-top: 1px solid #cccccc; background: #f6f6f6; font-size: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
"""

PAGE_SCRIPT = """\
(function () {
  "use strict";
  var canvas = document.getElementById("canvas");
  var svg = canvas ? canvas.querySelector("svg") : null;
  if (!svg) {
    return;
  }
  // A composition box descends; the view's own frame -- the nesting view's root -- does not,
  // or every press on empty space inside the picture would leave the page.
  var LINKABLE = '[data-kind="composition"][data-type]:not([data-role="frame"])';
  // The host: in the VS Code tab the bridge defines window.swcraftHost before this script runs;
  // a browser has none, and every call below then does exactly what the page did before.
  var host = window.swcraftHost || null;

  function go(href) {
    if (host) {
      host.navigate(href);
    } else {
      window.location.href = href;
    }
  }

  function reloadPage() {
    if (host) {
      host.reload();
    } else {
      window.location.reload();
    }
  }

  // What a drawn element opens in the model: its ordinal among the [data-role] elements, in
  // document order -- the generator's count -- keys its entry. Read only with a host; a block
  // that is not JSON is read as no entry at all, so pan, zoom and search still start.
  var model = {};
  var modelBlock = host ? document.getElementById("swcraft-model") : null;
  if (modelBlock) {
    try {
      model = JSON.parse(modelBlock.textContent || "{}") || {};
    } catch (error) {
      model = {};
    }
  }
  var drawn = Array.prototype.slice.call(svg.querySelectorAll("[data-role]"));
  var ordinals = new Map();
  drawn.forEach(function (element, index) {
    ordinals.set(element, index);
  });

  function entryOf(element) {
    var key = element && ordinals.has(element) ? String(ordinals.get(element)) : null;
    return key !== null && Object.prototype.hasOwnProperty.call(model, key) ? model[key] : null;
  }

  // The hit band: every sub-element -- a port, a line, a message -- gains one clone of its hit
  // shape, with no role, so the ordinals above do not move. The page style draws it as a
  // transparent stroke of a fixed screen width, so a small target is hit where the eye places it
  // at every zoom; it lies inside the sub-element's own group, so closest("[data-role]") finds it.
  var SUB_ELEMENTS =
    '[data-role="port"], [data-role="connector"], [data-role="delegation"], [data-role="bundle"], ' +
    '[data-role="activation"], [data-role="data"]';
  Array.prototype.forEach.call(svg.querySelectorAll(SUB_ELEMENTS), function (element) {
    var port = element.getAttribute("data-role") === "port";
    var shape = element.querySelector(port ? "rect.port-hit" : "path");
    if (!shape) {
      return;
    }
    // The namespace is the inlined picture's own: the page names no URL.
    var band = document.createElementNS(svg.namespaceURI, port ? "rect" : "path");
    (port ? ["x", "y", "width", "height"] : ["d"]).forEach(function (name) {
      if (shape.hasAttribute(name)) {
        band.setAttribute(name, shape.getAttribute(name));
      }
    });
    band.setAttribute("class", "hit-band");
    element.appendChild(band);
  });
  var links = {};
  var linkBlock = document.getElementById("swcraft-links");
  if (linkBlock) {
    links = JSON.parse(linkBlock.textContent || "{}");
  }
  // The way up: the frame's name leads to the one composition that places this one, or,
  // placed in several, marks the header's list of them and chooses none.
  var up = {};
  var upBlock = document.getElementById("swcraft-up");
  if (upBlock) {
    up = JSON.parse(upBlock.textContent || "{}");
  }

  function goUp() {
    var targets = Object.keys(up).map(function (parent) {
      return up[parent];
    });
    if (targets.length === 1) {
      go(targets[0]);
      return;
    }
    var list = document.getElementById("placed-in");
    if (list) {
      list.classList.add("highlight");
      var first = list.querySelector("a");
      if (first) {
        first.focus();
      }
    }
  }
  var box = (svg.getAttribute("viewBox") || "").trim().split(/[ ,]+/).map(Number);
  var home = { x: box[0], y: box[1], width: box[2], height: box[3] };
  var view = { x: home.x, y: home.y, width: home.width, height: home.height };

  function apply() {
    svg.setAttribute("viewBox", [view.x, view.y, view.width, view.height].join(" "));
  }

  function toDiagram(clientX, clientY) {
    var matrix = svg.getScreenCTM();
    if (!matrix) {
      return { x: view.x + view.width / 2, y: view.y + view.height / 2 };
    }
    return new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
  }

  function zoom(factor, cx, cy) {
    var width = view.width / factor;
    if (width < home.width / 50 || width > home.width * 20) {
      return;
    }
    view.x = cx - (cx - view.x) / factor;
    view.y = cy - (cy - view.y) / factor;
    view.width = width;
    view.height = view.height / factor;
    apply();
  }

  function zoomCentre(factor) {
    zoom(factor, view.x + view.width / 2, view.y + view.height / 2);
  }

  function fit() {
    view = { x: home.x, y: home.y, width: home.width, height: home.height };
    apply();
  }

  svg.addEventListener(
    "wheel",
    function (event) {
      event.preventDefault();
      var point = toDiagram(event.clientX, event.clientY);
      zoom(event.deltaY < 0 ? 1.2 : 1 / 1.2, point.x, point.y);
    },
    { passive: false }
  );

  var press = null;
  svg.addEventListener("pointerdown", function (event) {
    hideTip();
    if (event.button !== 0) {
      return;
    }
    var matrix = svg.getScreenCTM();
    press = {
      id: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      x: view.x,
      y: view.y,
      scale: matrix ? matrix.a : 1,
      moved: false,
      origin: event.target,
      modified: Boolean(event.ctrlKey || event.metaKey),
      target: event.target.closest ? event.target.closest(LINKABLE) : null,
      up: event.target.closest ? event.target.closest("g.frame-up") : null
    };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", function (event) {
    showTip(event);
    if (!press || event.pointerId !== press.id) {
      return;
    }
    var dx = event.clientX - press.clientX;
    var dy = event.clientY - press.clientY;
    if (!press.moved && Math.abs(dx) + Math.abs(dy) > 3) {
      press.moved = true;
      svg.classList.add("panning");
    }
    if (press.moved) {
      view.x = press.x - dx / press.scale;
      view.y = press.y - dy / press.scale;
      apply();
    }
  });
  function release(event) {
    if (!press || event.pointerId !== press.id) {
      return;
    }
    var pressed = !press.moved && event.type === "pointerup";
    var clicked = pressed ? press.target : null;
    var climbed = pressed ? press.up : null;
    var origin = press.origin;
    var modified = press.modified;
    press = null;
    svg.classList.remove("panning");
    // A click chooses the selection itself: a reveal's other drawings are no longer offered --
    // a Ctrl/Cmd+click that opens included, so no count or Next is left on the bar.
    if (pressed) {
      dropReveal();
    }
    // With a host, Ctrl/Cmd+click on a drawn element that has an entry opens it in the model,
    // before the click-through and the pin: a member composition opens its file and stays here.
    var element = pressed && origin.closest ? origin.closest("[data-role]") : null;
    var entry = host ? entryOf(element) : null;
    if (entry && modified) {
      openEntry(element, entry);
      return;
    }
    if (climbed) {
      goUp();
      return;
    }
    if (clicked) {
      var target = links[clicked.getAttribute("data-type")];
      if (target) {
        go(target);
        return;
      }
    }
    if (pressed) {
      // A click keeps what it lights until a click on empty space, which lights nothing.
      // On an overview's count square, or on a port carrying its interface's whole list, it
      // also opens the list panel; a second click on the same owner, like a click on empty
      // space, closes it and lights nothing.
      var owner = origin.closest ? origin.closest("g.edge-count, g.port") : null;
      if (owner && !listingOf(owner)) {
        owner = null;
      }
      if (owner && owner === panelSquare) {
        closePanel();
        pin(null);
        select(null, null);
        return;
      }
      var subject = subjectOf(origin);
      pin(subject);
      select(entry ? element : null, subject);
      if (owner) {
        showConnectors(owner);
      } else {
        closePanel();
      }
    }
  }
  svg.addEventListener("pointerup", release);
  svg.addEventListener("pointercancel", release);

  Array.prototype.forEach.call(svg.querySelectorAll(LINKABLE), function (element) {
    if (links[element.getAttribute("data-type")]) {
      element.classList.add("link");
    }
  });

  // The highlight: a line lights with the two ends it names, an end -- an IBD's port, an
  // overview's box, a sequence's lifeline -- with every line it has and the ends at their
  // far side. Both indexes are built here, once; a pointer move only reads them.
  var ENDS = '[data-role="port"][id], [data-role="member"][id], [data-role="lifeline"]';
  var statusLine = document.getElementById("status");
  var ends = {};
  var lines = {};
  var named = {};
  var lit = [];
  var litSubject = null;
  var pinned = null;

  function endKey(element) {
    return element.id || element.getAttribute("data-name");
  }

  Array.prototype.forEach.call(svg.querySelectorAll(ENDS), function (element) {
    ends[endKey(element)] = element;
  });
  Array.prototype.forEach.call(svg.querySelectorAll("[data-source][data-target]"), function (line) {
    named[line.getAttribute("data-name")] = line;
    [line.getAttribute("data-source"), line.getAttribute("data-target")].forEach(function (end) {
      lines[end] = (lines[end] || []).concat([line]);
    });
  });

  // What a pointer on *target* stands for: a line (an overview's count square stands for
  // its pair's line), an end -- connected or not, which then lights alone -- or nothing.
  function subjectOf(target) {
    if (!target || !target.closest) {
      return null;
    }
    var square = target.closest("g.edge-count");
    if (square) {
      return named[square.getAttribute("data-name")] || null;
    }
    var line = target.closest("[data-source][data-target]");
    if (line) {
      return line;
    }
    var end = target.closest(ENDS);
    return end || null;
  }

  // Every drawn element's <title> text, keyed by the element: filled once, below, when the
  // titles are removed from the page. One source of text for the status line, the panel's
  // heading and the tooltip.
  var titles = new Map();

  function titleOf(element) {
    return titles.get(element) || "";
  }

  function mark(element) {
    element.classList.add("highlight");
    lit.push(element);
  }

  function light(subject) {
    lit.forEach(function (element) {
      element.classList.remove("highlight");
    });
    lit = [];
    litSubject = subject;
    renderStatus();
    if (!subject) {
      return;
    }
    mark(subject);
    var joined = subject.hasAttribute("data-source") ? [subject] : lines[endKey(subject)] || [];
    joined.forEach(function (line) {
      mark(line);
      [line.getAttribute("data-source"), line.getAttribute("data-target")].forEach(function (end) {
        if (ends[end]) {
          mark(ends[end]);
        }
      });
    });
  }

  function pin(subject) {
    pinned = subject;
    light(subject);
  }

  // The status line shows the title of what is lit; with a host it stays empty while something
  // is selected, because the bar under the picture already shows the selection.
  function renderStatus() {
    if (statusLine) {
      statusLine.textContent = host && selected ? "" : litSubject ? titleOf(litSubject) : "";
    }
  }

  // The selection, with a host: a plain click on any drawn element with an entry selects it --
  // a pinnable one is pinned as before, any other gets the outline class "selected" -- and the
  // bar under the picture shows its text and the Open in model button. In a browser the bar
  // stays hidden and nothing is ever selected.
  var selectionBar = document.getElementById("selection");
  var selectionText = document.getElementById("selection-text");
  var openButton = document.getElementById("open-in-model");
  var nextButton = document.getElementById("selection-next");
  var selected = null;
  var selectedEntry = null;
  // A reveal's drawings of the element on this view, in document order, and the one selected:
  // the bar offers the next while there is more than one, until a click chooses for itself.
  var revealList = [];
  var revealAt = 0;
  // The bar's line for the revealed drawing before its "k of n" was added.
  var revealLine = "";

  function select(element, subject) {
    applySelection(element, subject);
    renderStatus();
  }

  function applySelection(element, subject) {
    if (selected) {
      selected.classList.remove("selected");
    }
    nextButton.hidden = true;
    selected = element;
    selectedEntry = element ? entryOf(element) : null;
    if (element && element !== subject) {
      element.classList.add("selected");
    }
    if (!element) {
      selectionBar.hidden = true;
      return;
    }
    if (selectedEntry.refused) {
      showLine(refusalLine(element, selectedEntry));
      return;
    }
    showLine(titleOf(element).split("\\n")[0] || element.getAttribute("data-name") || "", true);
  }

  function showLine(text, openable) {
    selectionText.textContent = text;
    openButton.hidden = !openable;
    selectionBar.hidden = false;
  }

  function refusalLine(element, entry) {
    var name = element.getAttribute("data-name") || "";
    return entry.refused === "several-files"
      ? name + " is declared in more than one file \\u2014 Check Model names them"
      : name + " is declared in no file \\u2014 Check Model reports it";
  }

  // Opening hands the file and the member to the host; the editor beside takes the focus and
  // the page no longer hears the pointer leave, so the tooltip is hidden first. A refused entry
  // opens nothing and says why on the line under the picture.
  function openEntry(element, entry) {
    holdTip();
    if (entry.refused) {
      showLine(refusalLine(element, entry));
      return;
    }
    host.open(entry.file, typeof entry.member === "string" ? entry.member : null);
  }

  openButton.addEventListener("click", function () {
    if (host && selected && selectedEntry && !selectedEntry.refused) {
      openEntry(selected, selectedEntry);
    }
  });

  // The list panel: an overview's count square names its pair and its count in its
  // <title>, and carries the connectors themselves in a <desc> that draws nothing, one row
  // per line. A row is the connector's own IBD line and its interface, and -- after a tab --
  // the description drawn under it. A port whose interface has more lines than its hover
  // shows carries every line in a <desc> of its own, one whole row per line, no note. The
  // panel is what lists them -- the native tooltip truncated a long list and could neither
  // wrap nor scroll -- and it is opened, and closed again, by the click that pins the subject.
  var panel = document.getElementById("connectors");
  var panelHead = document.getElementById("connectors-head");
  var panelList = document.getElementById("connectors-list");
  var panelSquare = null;

  function closePanel() {
    panelSquare = null;
    if (panel) {
      panel.hidden = true;
      panelList.textContent = "";
    }
  }

  function listingOf(element) {
    return element.querySelector ? element.querySelector("desc.connector-list, desc.member-list") : null;
  }

  function connectorsOf(element) {
    var listing = listingOf(element);
    return listing
      ? (listing.textContent || "").split("\\n").filter(function (name) {
          return name !== "";
        })
      : [];
  }

  function showConnectors(owner) {
    if (!panel) {
      return;
    }
    // A connector row's tab parts a note from its line; a member row is shown whole.
    var notes = listingOf(owner).classList.contains("connector-list");
    panelSquare = owner;
    panelHead.textContent = titleOf(owner).split("\\n")[0];
    panelList.textContent = "";
    connectorsOf(owner).forEach(function (row) {
      var parts = notes ? row.split("\\t") : [row];
      var item = document.createElement("li");
      item.textContent = parts[0];
      if (parts.length > 1 && parts[1] !== "") {
        var note = document.createElement("span");
        note.className = "note";
        note.textContent = parts.slice(1).join(" ");
        item.appendChild(note);
      }
      panelList.appendChild(item);
    });
    panel.hidden = false;
  }

  // The close button ends what the click that opened the list began, as a second click on the
  // same owner does: the pin and the selection go together, so no Open in model outlives them.
  if (panel) {
    document.getElementById("connectors-close").addEventListener("click", function () {
      closePanel();
      pin(null);
      select(null, null);
    });
  }

  // The page's own tooltip, at once and next to the pointer: the cached title of the nearest
  // titled ancestor of what the pointer is on, whatever the highlight's subject is. An
  // ancestor walk and a cache lookup, never a query.
  var tip = document.getElementById("tip");

  function tipText(target) {
    for (var element = target; element && element !== svg; element = element.parentNode) {
      if (titles.has(element)) {
        return titles.get(element);
      }
    }
    return "";
  }

  function hideTip() {
    if (tip) {
      tip.hidden = true;
    }
  }

  // With a host the tooltip is held hidden from an open and from the window's blur until the
  // pointer moves on: at the release Chromium fires pointerover at the element just opened, and
  // the editor beside then takes the focus, so the page never hears the pointer leave. Only a
  // pointermove away from where the hold began shows it again. Without a host nothing holds it.
  var tipHeld = null;
  var pointerAt = { x: 0, y: 0 };

  function holdTip() {
    hideTip();
    tipHeld = { x: pointerAt.x, y: pointerAt.y };
  }

  function showTip(event) {
    pointerAt = { x: event.clientX, y: event.clientY };
    if (tipHeld) {
      if (event.type !== "pointermove" || (event.clientX === tipHeld.x && event.clientY === tipHeld.y)) {
        hideTip();
        return;
      }
      tipHeld = null;
    }
    var text = tip && !press ? tipText(event.target) : "";
    if (!text) {
      hideTip();
      return;
    }
    if (tip.textContent !== text) {
      tip.textContent = text;
    }
    tip.hidden = false;
    // Kept inside the window: flipped to the pointer's other side where it would overflow.
    var width = window.innerWidth || 0;
    var height = window.innerHeight || 0;
    var x = event.clientX + 14;
    var y = event.clientY + 18;
    if (width && x + (tip.offsetWidth || 0) > width - 4) {
      x = Math.max(4, event.clientX - (tip.offsetWidth || 0) - 8);
    }
    if (height && y + (tip.offsetHeight || 0) > height - 4) {
      y = Math.max(4, event.clientY - (tip.offsetHeight || 0) - 8);
    }
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  // One delegated listener: the pointer resting on an element lights what it stands for,
  // unless a click pinned something or a press is under way; the tooltip shows either way.
  svg.addEventListener("pointerover", function (event) {
    showTip(event);
    if (pinned || press) {
      return;
    }
    light(subjectOf(event.target));
  });
  svg.addEventListener("pointerout", hideTip);

  var input = document.getElementById("search");
  var count = document.getElementById("search-count");
  var titled = Array.prototype.filter
    .call(svg.querySelectorAll("title"), function (title) {
      return title.parentNode !== svg;
    })
    .map(function (title) {
      // The search reads the list beside the title, so a bundle is still found by any one of
      // its connectors' names now that the title carries the head line alone, and a port by
      // a member line past its hover's cap.
      var element = title.parentNode;
      var text = (title.textContent || "") + "\\n" + connectorsOf(element).join("\\n");
      return { element: element, text: text.toLowerCase() };
    });
  // The search has read the titles; now the cache takes them and the nodes leave the page's
  // DOM -- the view's own heading too, which would become the late native tooltip over the
  // whole picture -- so the browser has nothing to show beside the page's tooltip.
  Array.prototype.forEach.call(svg.querySelectorAll("title"), function (title) {
    var owner = title.parentNode;
    if (owner !== svg) {
      titles.set(owner, title.textContent || "");
    }
    owner.removeChild(title);
  });
  var hits = [];
  var current = -1;

  function clearMarks() {
    hits.forEach(function (element) {
      element.classList.remove("hit");
      element.classList.remove("current");
    });
    hits = [];
    current = -1;
  }

  function reveal(element) {
    var bounds = element.getBBox();
    var grow = Math.max(1, (bounds.width * 1.5) / view.width, (bounds.height * 1.5) / view.height);
    view.width = view.width * grow;
    view.height = view.height * grow;
    view.x = bounds.x + bounds.width / 2 - view.width / 2;
    view.y = bounds.y + bounds.height / 2 - view.height / 2;
    apply();
  }

  function show(index) {
    if (!hits.length) {
      return;
    }
    if (current >= 0) {
      hits[current].classList.remove("current");
    }
    current = (index + hits.length) % hits.length;
    hits[current].classList.add("current");
    count.value = current + 1 + " of " + hits.length;
    reveal(hits[current]);
  }

  function search() {
    clearMarks();
    var query = input.value.trim().toLowerCase();
    if (!query) {
      count.value = "";
      return;
    }
    hits = titled
      .filter(function (item) {
        return item.text.indexOf(query) >= 0;
      })
      .map(function (item) {
        return item.element;
      });
    hits.forEach(function (element) {
      element.classList.add("hit");
    });
    if (hits.length) {
      show(0);
    } else {
      count.value = "no match";
    }
  }

  input.addEventListener("input", search);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      show(event.shiftKey ? current - 1 : current + 1);
    } else if (event.key === "Escape") {
      input.value = "";
      search();
    }
  });
  document.getElementById("search-next").addEventListener("click", function () {
    show(current + 1);
  });
  document.getElementById("search-previous").addEventListener("click", function () {
    show(current - 1);
  });
  document.getElementById("zoom-in").addEventListener("click", function () {
    zoomCentre(1.25);
  });
  document.getElementById("zoom-out").addEventListener("click", function () {
    zoomCentre(1 / 1.25);
  });
  document.getElementById("zoom-fit").addEventListener("click", fit);
  document.getElementById("reload").addEventListener("click", function () {
    reloadPage();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      hideTip();
    }
    if (event.target === input || event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    if (event.key === "+" || event.key === "=") {
      zoomCentre(1.25);
    } else if (event.key === "-") {
      zoomCentre(1 / 1.25);
    } else if (event.key === "0") {
      fit();
    }
  });

  // Arriving by the way up: the fragment names the member one came from, which is marked
  // with the highlight, kept as a click keeps it, and panned to.
  // In the tab the host hands the fragment over, already decoded.
  var arrival = "";
  try {
    arrival = host
      ? String((host.state && host.state.fragment) || "")
      : decodeURIComponent(window.location.hash.slice(1));
  } catch (error) {
    arrival = "";
  }
  if (arrival) {
    Array.prototype.some.call(svg.querySelectorAll('[data-role="member"]'), function (element) {
      if (element.getAttribute("data-name") !== arrival) {
        return false;
      }
      pin(element);
      reveal(element);
      return true;
    });
  }

  // Show in Diagram, with a host -- a jump, never a search: the element is selected exactly as
  // a click on it selects it, and centred. Of the file's drawn elements, those of the member
  // where that member is drawn, else the element's own (no member), else any of the file's;
  // drawn more than once, the first is selected and the bar says how many with a Next button.
  // The search box and its marks are not touched; the search box gives up the focus so the
  // page's keys work.
  function revealModel(request) {
    if (!request || typeof request.file !== "string") {
      return;
    }
    var ofFile = drawn.filter(function (element) {
      var entry = entryOf(element);
      return entry !== null && entry.file === request.file;
    });
    function ofMember(member) {
      return ofFile.filter(function (element) {
        var held = entryOf(element).member;
        return member === null ? typeof held !== "string" : held === member;
      });
    }
    var found = typeof request.member === "string" ? ofMember(request.member) : [];
    if (!found.length) {
      found = ofMember(null);
    }
    if (!found.length) {
      found = ofFile;
    }
    if (document.activeElement === input && input.blur) {
      input.blur();
    }
    closePanel();
    if (!found.length) {
      var name = typeof request.name === "string" && request.name ? request.name : request.file;
      dropReveal();
      pin(null);
      select(null, null);
      showLine(name + " is not on this diagram as last generated");
      return;
    }
    revealList = found;
    selectRevealed(0);
  }

  function selectRevealed(index) {
    revealAt = index;
    var element = revealList[index];
    var subject = subjectOf(element);
    pin(subject);
    select(element, subject);
    if (revealList.length > 1) {
      revealLine = selectionText.textContent;
      selectionText.textContent += " \\u2014 " + (index + 1) + " of " + revealList.length + " on this diagram";
      nextButton.hidden = false;
    }
    reveal(element);
  }

  // Drops a reveal's offer of its other drawings, on any path that ends it: the list is
  // emptied, Next is hidden, and a bar still showing the revealed drawing loses its count.
  function dropReveal() {
    if (revealList.length > 1 && selected === revealList[revealAt]) {
      selectionText.textContent = revealLine;
    }
    revealList = [];
    nextButton.hidden = true;
  }

  nextButton.addEventListener("click", function () {
    if (revealList.length > 1) {
      selectRevealed((revealAt + 1) % revealList.length);
    }
  });

  if (host) {
    window.addEventListener("blur", holdTip);
    host.on("reveal", revealModel);
    if (host.state && host.state.reveal) {
      revealModel(host.state.reveal);
    }
  }
})();
"""


def page_script_sha256() -> str:
    """The SHA-256 of :data:`PAGE_SCRIPT`, base64, as a CSP ``'sha256-…'`` source spells it.

    The VS Code tab allows the page's one script by this hash alone; the script is written exactly
    as :data:`PAGE_SCRIPT` holds it, so the constant's hash is the element's. A page written by
    another release carries another script, which the extension refuses rather than runs.
    """
    return base64.b64encode(hashlib.sha256(PAGE_SCRIPT.encode("utf-8")).digest()).decode("ascii")


def page_file(view: str) -> str:
    """The file name of *view*'s page."""
    return f"{view}.html"


def page_href(view: str) -> str:
    """The relative link to *view*'s page, safe inside an attribute and as a URL."""
    return quote(page_file(view), safe="")


def svg_href(view: str) -> str:
    """The relative link to *view*'s SVG file."""
    return quote(f"{view}.svg", safe="")


def view_heading(svg_text: str) -> str:
    """The view's heading: the SVG document's own ``<title>``, unescaped."""
    match = _HEADING.search(svg_text)
    return html.unescape(match.group(1)) if match else ""


def _target_prefix(view: str) -> str | None:
    """The prefix a composition box on *view* descends into, or ``None`` when it does not."""
    if view.startswith("overview-"):
        return "overview-"
    if view.startswith("ibd-") or view == "nesting":
        return "ibd-"
    return None


def click_through(
    view: str, svg_text: str, views: Sequence[str], stems: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Composition type -> the page it opens, for every composition box drawn on *view*.

    A type whose page is not among *views* is left out. *stems* maps a composition's name to its
    view stem; a name it does not hold is its own stem.
    """
    stems = {} if stems is None else stems
    prefix = _target_prefix(view)
    if prefix is None:
        return {}
    existing = set(views)
    targets: dict[str, str] = {}
    for match in _GROUP_TAG.finditer(svg_text):
        attributes = {name: html.unescape(value) for name, value in _ATTRIBUTE.findall(match.group(1))}
        type_name = attributes.get("data-type")
        if attributes.get("data-kind") != "composition" or type_name is None or type_name in targets:
            continue
        target = f"{prefix}{stems.get(type_name, type_name)}"
        if target in existing:
            targets[type_name] = page_href(target)
    return targets


def _frame_name(svg_text: str) -> str | None:
    """The name of the composition whose frame *svg_text* draws, or ``None`` when it draws none."""
    match = _FRAME_TAG.search(svg_text)
    if match is None:
        return None
    attributes = {name: html.unescape(value) for name, value in _ATTRIBUTE.findall(match.group(0))}
    return attributes.get("data-name")


def _level_href(prefix: str, parent: str, member: str, existing: set[str], stems: Mapping[str, str]) -> str | None:
    """*parent*'s page of the kind *prefix* names, opened at *member*; ``None`` when that page is not written."""
    target = f"{prefix}{stems.get(parent, parent)}"
    if target not in existing:
        return None
    return f"{page_href(target)}#{quote(member, safe='')}"


def way_up(
    view: str,
    svg_text: str,
    views: Sequence[str],
    stems: Mapping[str, str] | None = None,
    placements: Mapping[str, Sequence[tuple[str, str]]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The way up from a composition's page: the frame name's targets and the header's markup.

    The targets map each placing composition to its page of the same kind, at the member that
    places it. The markup is the chain from the root, each level a link and the view's own
    last -- or, where a level is placed in several compositions or the walk repeats, *placed in:*
    and the view's own parents.
    """
    stems = {} if stems is None else stems
    placements = {} if placements is None else placements
    prefix = _target_prefix(view)
    name = _frame_name(svg_text)
    if prefix is None or view == "nesting" or name is None:
        return {}, []
    existing = set(views)
    parents = [
        (parent, _level_href(prefix, parent, member, existing, stems)) for parent, member in placements.get(name, ())
    ]
    targets: dict[str, str] = {}
    for parent, href in parents:
        if href is not None:
            targets.setdefault(parent, href)
    if not parents:
        return targets, []
    chain: list[tuple[str, str]] = []
    seen = {name}
    current = name
    while True:
        above = list(placements.get(current, ()))
        if not above:
            break
        if len(above) > 1 or above[0][0] in seen:
            chain = []
            break
        parent, member = above[0]
        chain.insert(0, (parent, member))
        seen.add(parent)
        current = parent
    if not chain:
        listed = []
        for parent, href in parents:
            listed.append(_anchor(href, parent) if href is not None else f"<span>{escape(parent)}</span>")
        return targets, ['<span class="placed-in" id="placed-in">placed in: ' + ", ".join(listed) + "</span>"]
    levels = []
    for parent, member in chain:
        href = _level_href(prefix, parent, member, existing, stems)
        levels.append(_anchor(href, parent) if href is not None else f"<span>{escape(parent)}</span>")
    levels.append(f"<span>{escape(name)}</span>")
    return targets, ['<span class="chain">' + CHAIN_SEPARATOR.join(levels) + "</span>"]


def _related(view: str, views: Sequence[str]) -> list[tuple[str, str]]:
    """The navigation links beside the index: the sibling view, the nesting, the SVG file."""
    existing = set(views)
    related = []
    for prefix, sibling_prefix, words in (
        ("ibd-", "overview-", "overview"),
        ("overview-", "ibd-", "internal block diagram"),
    ):
        sibling = sibling_prefix + view[len(prefix) :]
        if view.startswith(prefix) and sibling in existing:
            related.append((page_href(sibling), words))
    if view != "nesting" and "nesting" in existing:
        related.append((page_href("nesting"), "nesting"))
    related.append((svg_href(view), "SVG file"))
    return related


def _anchor(href: str, text: str) -> str:
    return f'<a href="{escape(href)}">{escape(text)}</a>'


def strip_sources(svg_text: str, sources: Sequence[ModelEntry]) -> tuple[str, dict[str, ModelEntry]]:
    """*svg_text* without its temporary source attributes, and each drawn element's model entry by ordinal.

    The ordinal counts every ``[data-role]`` start tag in document order, as the page script
    does, mapped or not. Every entry in *sources* must be named exactly once, on a
    ``[data-role]`` element; otherwise this raises rather than write a page that opens the
    wrong file.
    """
    pieces: list[str] = []
    entries: dict[str, ModelEntry] = {}
    used: set[int] = set()
    last = 0
    ordinal = 0
    for match in _START_TAG.finditer(svg_text):
        tag = match.group(0)
        roled = _ROLE_ATTRIBUTE.search(tag) is not None
        found = _SOURCE_ATTRIBUTE.search(tag)
        if found is not None:
            index = int(found.group(1))
            if not roled or index in used or index >= len(sources):
                raise ValueError(f"a source attribute the renderers wrote does not name one drawn element: {tag!r}")
            used.add(index)
            entries[str(ordinal)] = sources[index]
            pieces.append(svg_text[last : match.start()])
            pieces.append(tag[: found.start()] + tag[found.end() :])
            last = match.end()
        if roled:
            ordinal += 1
    if len(used) != len(sources):
        raise ValueError("a model entry the renderers collected was written on no drawn element")
    pieces.append(svg_text[last:])
    return "".join(pieces), entries


def elements_index(views: Sequence[tuple[str, Mapping[str, ModelEntry]]]) -> dict[str, dict[str, Any]]:
    """Storage name -> ``{"views": [...], "members": {member: [...]}}``, over every view's model entries.

    Views are listed in the index's own group order (:data:`_GROUPS`). A refused entry names no
    file and is listed nowhere.
    """
    ordered = [item for _group, prefixes in _GROUPS for item in views if item[0].startswith(prefixes)]
    placed = {view for view, _entries in ordered}
    ordered.extend(item for item in views if item[0] not in placed)
    index: dict[str, dict[str, Any]] = {}
    for view, entries in ordered:
        for _ordinal, entry in sorted(entries.items(), key=lambda pair: int(pair[0])):
            file = entry.get("file")
            if not isinstance(file, str):
                continue
            slot = index.setdefault(file, {"views": [], "members": {}})
            if view not in slot["views"]:
                slot["views"].append(view)
            member = entry.get("member")
            if isinstance(member, str):
                drawn = slot["members"].setdefault(member, [])
                if view not in drawn:
                    drawn.append(view)
    return index


def _json_block(data: Mapping[str, Any]) -> str:
    text = json.dumps(dict(data), sort_keys=True, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_view_page(
    project_name: str,
    view: str,
    svg_text: str,
    views: Sequence[str],
    stems: Mapping[str, str] | None = None,
    placements: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    model: Mapping[str, ModelEntry] | None = None,
) -> str:
    """The HTML page of one view: *svg_text* inlined, the shared style and script, the links.

    A link to a page not among *views* is not made. *model* is the view's model block (``None``
    writes an empty one).
    """
    heading = view_heading(svg_text)
    targets = click_through(view, svg_text, views, stems)
    up_targets, up_markup = way_up(view, svg_text, views, stems, placements)
    navigation = [_anchor(INDEX_PAGE, project_name)]
    navigation.extend(_anchor(href, text) for href, text in _related(view, views))
    if targets:
        navigation.append("<span>internal block diagrams:</span>" if view == "nesting" else "<span>contains:</span>")
        navigation.extend(_anchor(href, type_name) for type_name, href in targets.items())
    navigation.extend(up_markup)
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(heading)} — {escape(project_name)}</title>",
        f"<style>\n{PAGE_STYLE}</style>",
        "</head>",
        f'<body data-view="{escape(view)}">',
        "<header>",
        f"<h1>{escape(heading)}</h1>",
        "<nav>" + " ".join(navigation) + "</nav>",
        '<div class="controls">',
        '<input type="search" id="search" placeholder="Search names" aria-label="Search names">',
        '<button type="button" id="search-previous" title="Previous match">&#8593;</button>',
        '<button type="button" id="search-next" title="Next match">&#8595;</button>',
        '<output id="search-count" for="search"></output>',
        '<button type="button" id="zoom-in" title="Zoom in">+</button>',
        '<button type="button" id="zoom-out" title="Zoom out">&#8722;</button>',
        '<button type="button" id="zoom-fit" title="Show the whole view">Fit</button>',
        '<button type="button" id="reload" title="Read the page again from disk">Reload</button>',
        "</div>",
        "</header>",
        '<main id="canvas">',
    ]
    return (
        "\n".join(lines)
        + "\n"
        + svg_text
        + "</main>\n"
        + '<aside id="connectors" hidden><div class="panel-head"><span id="connectors-head"></span>'
        + '<button type="button" id="connectors-close" title="Close the list">&#215;</button></div>'
        + '<ol id="connectors-list"></ol></aside>\n'
        + '<div id="selection" hidden><span id="selection-text"></span>'
        + '<button type="button" id="selection-next" title="Select the next drawing of this element" hidden>'
        + "Next</button>"
        + '<button type="button" id="open-in-model" title="Open this element\'s YAML file beside the diagram" hidden>'
        + "Open in model</button></div>\n"
        + '<footer id="status" role="status"></footer>\n'
        + '<div id="tip" role="tooltip" hidden></div>\n'
        + f'<script type="application/json" id="{LINKS_SCRIPT_ID}">{_json_block(targets)}</script>\n'
        + f'<script type="application/json" id="{UP_SCRIPT_ID}">{_json_block(up_targets)}</script>\n'
        + f'<script type="application/json" id="{MODEL_SCRIPT_ID}">{_json_block(model or {})}</script>\n'
        + f"<script>{PAGE_SCRIPT}</script>\n"
        + "</body>\n</html>\n"
    )


def render_index(
    project_name: str,
    views: Sequence[tuple[str, str]],
    report_text: str,
    elements: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """``index.html``: *views* (id and SVG text) grouped by kind in the order given, then the report.

    *elements* is :func:`elements_index`'s answer, written as the ``swcraft-elements`` data
    block (``None`` writes an empty one); the index carries no executable script.
    """
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(project_name)}</title>",
        "<style>",
        "body { margin: 0 auto; max-width: 960px; padding: 16px; font: 14px Helvetica, Arial, sans-serif; }",
        "a { color: #1a5fb4; }",
        "li { margin: 2px 0; }",
        ".file { color: #555555; font-size: 12px; }",
        "pre { background: #f6f6f6; padding: 8px; overflow-x: auto; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(project_name)}</h1>",
    ]
    for group, prefixes in _GROUPS:
        members = [(view, svg_text) for view, svg_text in views if view.startswith(prefixes)]
        if not members:
            continue
        lines.append("<section>")
        lines.append(f"<h2>{group}</h2>")
        lines.append("<ul>")
        for view, svg_text in members:
            page_link = _anchor(page_href(view), view_heading(svg_text))
            file_link = f'<span class="file">({_anchor(svg_href(view), "SVG")})</span>'
            lines.append(f"<li>{page_link} {file_link}</li>")
        lines.append("</ul>")
        lines.append("</section>")
    lines.extend(
        [
            '<section id="report">',
            "<h2>Report</h2>",
            f"<pre>{escape(report_text)}</pre>",
            "</section>",
            f"<p>{_anchor(REPORT_FILE, REPORT_FILE)}</p>",
            f'<script type="application/json" id="{ELEMENTS_SCRIPT_ID}">{_json_block(elements or {})}</script>',
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines) + "\n"
