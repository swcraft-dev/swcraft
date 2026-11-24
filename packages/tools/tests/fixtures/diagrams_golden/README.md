# `fixtures/diagrams_golden/` — the diagram generator's whole output

One directory per model, each holding exactly what `generate_diagrams` writes into a
`views_root` for it: every view's `.svg` and `.html`, `index.html` and `report.txt`, byte for
byte.

| Directory | Model | Project name passed |
|---|---|---|
| `basic/` | `examples/basic/model` | `swcraft-basic-example` (its manifest's `name`) |
| `showcase/` | `fixtures/showcase/model` (the showcase repository's model, copied — its README) | `vehicle-control-showcase` (the showcase manifest's `name`) |
| `compositions/` | `fixtures/compositions/model` | `Composition fixture` |
| `diagrams/` | `fixtures/diagrams/model` | `Diagram fixture` |

Written by `python packages/tools/tests/test_diagram_generation.py --freeze` (needs Node), which runs the
generator itself into these directories, and **never by hand**. The generator removes only files
of its own naming, so this README survives a re-freeze.

They are **change detectors**, not correctness proofs (qualification policy item 5): a diff here
is the generator changing, and whether the new picture is right is read in a browser, never
inferred from the test going green again. `diagrams/`'s SVG files are also held equal to the
renderers' own goldens under `fixtures/diagrams/svg/`, so a re-freeze here cannot drift from those
unseen.

What they do not reach: another platform. They were frozen on macOS with Node.js 26.8.2; that the
same bytes come out on Linux and Windows rests on one rule (every coordinate rounded to two
decimals before rendering) and the first CI run's measurement.

## Re-freezes

Each re-freeze after the first freeze, numbered, with the bytes it moved and why; the log named
beside it carries the argument written before the fixtures were rewritten and the diff check.

### 1. The overview's arrowhead enters its box square-on

An overview's arrowhead took the spline's tangent at its end (`orient="auto-start-reverse"`) and
met the target box skewed. It now takes a fixed angle per side of the target box the line ends on
(0, 90, 180 or 270 degrees), one fixed-orient marker per side. Moved: each overview's `<defs>` line
(one marker became four), each bundle path's `marker-end` reference, and — as their consequence —
each overview's byte count in `index.html` and `report.txt`. No coordinate, no IBD, no sequence
view and no `nesting` view moved. The same bytes moved in `fixtures/diagrams/svg/overview-*.svg`.

### 2. The port glyphs follow the VFB figure

The glyphs were UML's ball and socket for both interface kinds, told apart by a fill and a dot. They
now follow AUTOSAR's VFB port figure: a sender-receiver provide port is a pentagon arrow 16 × 12
with its point on the tip, a require port a two-prong fork 8 × 12 opening on the tip; a
client-server provide port an empty circle 12 × 12, a require port a half circle 6 × 12 with no
dot. Moved: each port's `<g class="glyph">` (the stem and the shape) and — as their consequence —
each IBD's byte count in `index.html` and `report.txt`. No port square, no line, no coordinate, no
overview, no sequence view and no `nesting` view moved. The same bytes moved in
`fixtures/diagrams/svg/ibd-*.svg`.

### 3. A port label never runs over its glyph

A label's width was estimated at a flat 0.6 em a character, so an all-capitals name overran its box,
and a port's label was anchored at its box's start, so the overrun of an east port's label landed on
the square and the glyph. The width is now estimated per character class — capitals 0.67 em, lower
case, digits and `_` 0.55 em, every other character 0.6 em — by one function the extractor and the
renderers share, and a port's label is anchored at its port's side: `text-anchor="end"` at the inner
end of an east port's box, `"start"` at a west port's. Moved: **coordinates** — every member box,
port, line and frame ELK computes from a label box, in every IBD and overview, and every box the
`nesting` and `sequence-*` grids size, which is every view (all of these fixtures' names are
mixed-case, so most views came out a few pixels narrower); each port label's `x` (east ports) and
its new `text-anchor`; each page's inlined copy; each byte count in `index.html` and `report.txt`.
Nothing but numbers and the one attribute moved. The same bytes moved in `fixtures/diagrams/svg/`,
after `fixtures/diagrams/laid-out/` was re-recorded.

### 4. The count square and the line can be rested on

An overview's count square was a bare `rect` and `text` with no `<title>`, in the labels layer the
page gives no pointer events, and a line was a 1 px stroke a pointer could not rest on. The count
square is now one `<g class="edge-count">` per bundle carrying its edge's own `<title>`, which the
page alone in the labels layer gives pointer events; every edge path carries an `id` (`edge-<n>`,
its ordinal in the view) and is followed by its hit area, a `<use>` of that path with a 10 px stroke
at `opacity` 0, never a second path repeating `d`. The path's `stroke-width` moved to its edge group,
so the hit area's width reaches its clone. Moved: every edge line of every IBD and overview (the
`id`, the added `<use>`, the moved `stroke-width`), each overview's count square (its `rect` and
`text` joined into the titled group on one line), one CSS line in every page — the `nesting` and
`sequence-*` pages included, which moved in nothing else — and each byte count in `index.html` and
`report.txt`. No coordinate moved, and nothing in a `nesting` or `sequence-*` SVG. The same bytes
moved in `fixtures/diagrams/svg/ibd-*.svg` and `overview-*.svg`.

### 5. A line lights up with its two ports, and a click keeps it

A line's `<title>` named its two ports, but nothing tied the line to the port elements, which lie
in another layer and carried no `id`. Every IBD port now carries an ordinal `id` (`port-<n>`) and
every overview member box one (`member-<n>`), and every IBD edge and overview bundle carries
`data-source` and `data-target`, its two ends' ids; the page gained a `highlight` style, a status
line under the picture and the script that lights a line with its two ends — or an end with every
line it has — shows the lit element's own `<title>` in the status line, and pins it on a click
until a click on empty space. A sequence step needed no new attribute: its `data-source` and
`data-target` already name its two lifelines. Moved: each IBD and overview SVG (the ids and the two
attributes), each page's style, script and one `<footer>` line — the `nesting` and `sequence-*`
pages included, which moved in nothing else — and each byte count in `index.html` and
`report.txt`. No coordinate moved, and nothing in a `nesting` or `sequence-*` SVG. The same bytes
moved in `fixtures/diagrams/svg/ibd-*.svg` and `overview-*.svg`.

### 6. The frame's name is the way up

A composition's page led down — a member composition is a link to its own page — and nothing led
up: the page did not know which composition placed it. The extractor now reads, from the whole
model, every composition that places each one, and the frame carries it: `data-parents`, one
`<parent>#<member>` entry per placing composition, percent-encoded. The frame's name of a placed
composition is drawn behind an `↑` in a `<g class="frame-up">` carrying its own `<title>` (`up to
<Parent>`, or `placed in: A, B`), which the page presses like a box: one parent opens its page of
the same kind at `#<member>`, whose page marks that member with the existing highlight and pans to
it. The page's header gained, beside *contains:*, the chain from the root as plain anchors
separated by `›`, the view's own composition last and not a link — or *placed in:* where some level
is placed in several — and every page gained the way up's JSON block (`swcraft-up`), its style lines
and its script. Moved: each placed composition's IBD and overview SVG (the frame's attribute and the
wrapped label), each page's style, script and way-up block — the root's, the `nesting` and
`sequence-*` pages included, which moved in nothing else — each placed composition's header, and
each byte count in `index.html` and `report.txt`. No coordinate moved: the label keeps its laid-out
point and its laid-out width. The same bytes moved in `fixtures/diagrams/svg/ibd-Chassis.svg` and
`overview-Chassis.svg`, after `fixtures/diagrams/laid-out/` was re-recorded — the frame's metadata
gained `parents`, the only change in the four recordings.

### 7. The `deployment` view

A model that declares a task gains one view, `deployment`: the processor outermost, each core
inside it headed with its name and — when declared — its ASIL capability, each task inside the core
it runs on, and each component the task runs as a leaf labelled with its effective ASIL (none for
`QM`). It is drawn by the nesting view's renderer, generalised to take the view's id, heading, fills
and leaf, so the `nesting` view did not move. Added: `deployment.svg` and `deployment.html` in
`basic/`, `diagrams/` and `showcase/` (`compositions/` declares no task and did not move at all);
each of those three `index.html` gained a *Deployment* section between *Nesting* and *Sequences*,
and each `report.txt` (with the index's copy of it) the view's size line and, in `basic/` and
`diagrams/`, a `not-run` record for each component no task runs. No other file moved — no other
view's SVG or page, since no page links the new view. The same SVG is
`fixtures/diagrams/svg/deployment.svg`.

### 8. The size budget: shorter ids, one hit-area rule

The hover work spent the +10 % the lines' hover was allowed on a large tool export, so the two
cuts its log named first were taken: every ordinal id lost its word — `e<n>` for a line (was
`edge-<n>`), `p<n>` for a port (`port-<n>`), `m<n>` for an overview's member box (`member-<n>`),
in the `id`, the hit area's `href` and each edge's `data-source` and `data-target` — and the hit
area's `stroke-width="10"` left every `<use>` for one rule of the document's own
`<style>g.edges use{stroke-width:10px}</style>`, written after its `<title>` in every view that
draws a line and in no other. Moved: every IBD and overview SVG and its page, and each byte count
in `index.html` and `report.txt`. The `nesting`, `deployment` and `sequence-*` views draw no hit
area and did not move. No coordinate moved: with exactly those two moves undone every moved SVG
and page answers its previous bytes. The same bytes moved in the four `fixtures/diagrams/svg/`
IBDs and overviews; the recorded layouts did not.

### 9. A reading of real tool exports

Four changes to the drawing — items 1, 2, 4 and 5 below, with item 3's highlight carried in the
page style — reach these directories at once, so they are one re-freeze.
**Item 1**: every port glyph gained the
stem the VFB sketch seats under it, so a port's ELK box is as wide as its own glyph — per
`(direction, interface kind)`, where it was the one constant 28 — and **every IBD coordinate
moved**. **Item 5**: a frame's left and right padding are derived from its boundary ports' labels,
which moves an IBD again wherever a boundary name is long, and moves no overview (its frame draws
no port). **Item 2**: the overview is routed orthogonally rather than with splines, so every
bundle's path data moved and the four fixed-angle markers became one head that follows the line;
every marker gained `markerUnits="userSpaceOnUse"`, which is the one thing that reaches a
`sequence-*` SVG. **Item 4**: a bundle's connector names left both `<title>` elements for a
`<desc>` on the count square, and every page gained the panel that lists them, with **item 3**'s
black highlight in its style — so every page of every directory moved, the `nesting`, `deployment`
and `sequence-*` ones included. Every byte count in `index.html` and `report.txt` moved with the
files, and no view appeared or vanished. The same bytes moved in `fixtures/diagrams/svg/`, after
`fixtures/diagrams/laid-out/` was re-recorded. Before the re-freeze, the generators as they stood
before the change — exported with `git archive` into a scratch tree and run over the same models through
`PYTHONPATH` — answered every digest `test_application_types_byte_identity.py` held, which is the
proof that nothing else had moved.

### 10. A second reading of real tool exports

Both items of the reading of 2026-09-23 reach these directories together, so they are one
re-freeze.
**Item 6**: an overview bundle carries the whole connector rather than its name, so every count
square's `<desc>` holds a **row** — the connector's own IBD line, the interface its source end
declares, and its description after a tab — and every page's panel style and script moved with it,
which is why the `nesting`, `deployment` and `sequence-*` pages moved too and their SVGs did not.
**Item 7**: a composition's boundary port points **out** of its frame, so its `borderOffset` is the
member's, its glyph is drawn outside the frame, its delegation ends on the inner face of its square,
the frame's derived side padding measures from that face rather than from the whole box, and the
IBD's wrapping root reserves `graph.ROOT_PADDING` — **every IBD coordinate moved again**, and no
overview coordinate did, an overview's frame drawing no port and its root keeping ELK's own padding.
Every byte count in `index.html` and `report.txt` moved with the files, and no view appeared or
vanished. The same bytes moved in `fixtures/diagrams/svg/`, after `fixtures/diagrams/laid-out/` was
re-recorded. Before the re-freeze a checkout of the state before the change answered every
digest `test_application_types_byte_identity.py` held and every golden here, which is the proof that
nothing else had moved.

### 11. The `deployment` view gains the partition level

Contract `9.0.0`: a task names a partition and runs on that partition's core, so the `deployment`
view nests processor → core → partition → task → component. Each partition is a box in its own
fill (`#f3e5f5`) under the core it names, headed with its name and, when rated, its level (`ASIL D`
the way a component's is; nothing for an unrated or `QM` partition); a partition no task names is
an empty box. The three models here declare one unrated partition each — `PARTITION0` on `CORE0`
in `basic/` and `diagrams/`, `Partition0` on `Core0` holding both tasks in `showcase/` — so each
`deployment.svg` and `deployment.html` gained one `member partition` group with its `<title>` and
one label, every box below the core moved one level in (12 px right, 26 px down), and the canvas
grew 24 px wide and 38 px high. Each `report.txt` moved by the view's size line alone, and so did
each `index.html`, which carries a copy of the report — the index links the view by name, and
that copy is why it moved; nothing else in it moved. No record
moved: none of the three models has an undrawn partition or task. `compositions/` declares no
task and did not move at all, and no other view's SVG or page moved in any directory. The same
SVG is `fixtures/diagrams/svg/deployment.svg`, still byte-identical to `diagrams/deployment.svg`
(that directory's README, § *Re-freezes of `svg/`*).

### 12. A trusted partition carries a trust mark

Contract `9.1.0`: a partition declares its trust class, and the `deployment` view appends `trusted`
to the heading of a `trusted` partition — after its level (`ASIL D · trusted`), or alone when it
declares none or `QM` — and nothing for the two confined classes. `fixtures/diagrams/model`'s
`PARTITION0` now declares `trust: trusted` (it stays unrated), so in `diagrams/` the partition's
label reads `PARTITION0 — trusted` and its `<title>` gains the line `trusted`: `deployment.svg` went
from 2 345 to 2 365 bytes (+20), `deployment.html`, which inlines it, from 20 688 to 20 708 (+20),
and `report.txt` and `index.html` moved by the view's size line alone (`2345 bytes` → `2365 bytes`,
same length). The box did not widen and the canvas stayed 388 x 228 px; no record moved. `basic/`
and `showcase/` declare no trust and did not move, `compositions/` declares no task, and no other
view's SVG or page moved in any directory. `fixtures/diagrams/svg/deployment.svg` moved with it and
is still byte-identical to `diagrams/deployment.svg` (that directory's README, § *Re-freezes of
`svg/`*, item 2). No ARXML or RST digest moved (`test_diagram_switch_byte_identity.py`).

### 13. The 0.16.2 drawing: blue, the whole port, an immediate tooltip

For the page and the renderer (2026-10-14): the highlight is blue
`#1a5fb4` rather than black, a port is lit whole (its square and its glyph equally thick) and any
port is a subject, a page shows a title at once in a tooltip of its own (`#tip`, bounded by the
window: a text longer than it is cut, not scrolled) and removes the native titles after caching
them, a port can be rested on anywhere in its box, and a line the
validator does not judge is dashed. Moved: every `.html` of every directory (the page's style and
script, and the `#tip` element after the status line), 2 807 bytes each plus whatever its inlined
SVG moved; every IBD `.svg`, one `rect.port-hit` (opacity 0) last in each of the 33 port groups
of the four directories, and in a view that draws a line its hit-area rule's
`stroke-dasharray:none` (+22 bytes) — `showcase/ibd-VehicleControl.svg` 7 591 → 8 059,
`diagrams/ibd-Chassis.svg` 11 014 → 11 896; every overview `.svg`, the lit arrow marker's fill
(`#000000` → `#1a5fb4`, the same length) and, where it draws a line, the same rule (+22); and the
IBD and overview size lines in `report.txt` and in `index.html`'s copy of it. No coordinate, glyph,
label or title moved, and no `nesting`, `deployment` or `sequence-*` SVG: no golden model holds a
port on a stub, an undeclared interface or a line the validator does not judge, so neither the
bare square nor the dash appears here (the `compositions` tree's `ExternalStub` has no port). The
diff was read file by file: with exactly those three SVG moves undone every moved SVG answers its
previous bytes, and every page and `index.html` differs from its previous bytes only inside its
`<style>`, its `<script>`, the `#tip` element, that SVG and the byte counts. No view appeared or
vanished. The four IBD and overview SVGs of `fixtures/diagrams/svg/` moved by the same bytes in
the drawing's own task (that directory's README, § *Re-freezes of `svg/`*, item 3) and are still
byte-identical to `diagrams/`'s. No ARXML or RST digest moved. In a scratch worktree with exactly
those page and renderer changes undone the generators answered every golden here and every digest
the byte-identity records held. The tooltip's bound (`max-width`, `max-height`, `overflow: hidden`,
`overflow-wrap`) moved the 24 pages by 100 bytes each in the
`#tip` rule alone; the goldens and the records' diagram digests were re-taken over it.

### 14. A port's hover lists its interface's content

2026-11-02: a port's hover lists what its interface carries — a
data element `name: type` per line, an operation `Name(dir arg: type, …)` per line, an external
stub's bare member names — each behind `· `, at most eight and then `and N more — click the port to
list all M`; a sequence data message lists the same lines with `and N more` alone; a port over the
cap carries every line in a `<desc class="member-list">`, and a click on it opens the list panel
the overview's count square opens. Moved: every `.html` of every directory (the page's script, 584
bytes each, plus whatever its inlined SVG moved); every IBD `.svg` whose ports name a declared
interface — `basic/ibd-Root.svg` 3 827 → 3 863, `compositions/ibd-Root.svg` 4 426 → 4 476,
`compositions/ibd-Sink.svg` 3 065 → 3 097, `compositions/ibd-Sub.svg` 8 388 → 8 492,
`diagrams/ibd-Chassis.svg` 11 896 → 12 106, `diagrams/ibd-Vehicle.svg` 4 056 → 4 138,
`showcase/ibd-VehicleControl.svg` 8 059 → 8 158; `diagrams/sequence-speed_cycle.svg` 4 841 →
4 873, its two data messages (the `basic` and `showcase` sequences draw activations alone and did
not move); and those views' size lines in `report.txt` and in `index.html`'s copy of it. The diff
was read file by file: every moved SVG answers its previous bytes with each `<title>`'s text
blanked, every moved title is a port's or a data message's and gained a `· ` line, and every page
and `index.html` differs from its previous bytes only inside its `<script>`, its inlined SVG and the
byte counts. No golden gained a `<desc class="member-list">` — no golden model holds an interface
over the cap — no view appeared or vanished, no overview, `nesting` or `deployment` SVG moved, and
`fixtures/diagrams/laid-out/` did not move. The `fixtures/diagrams/svg/` files moved by the same
bytes (that directory's README, § *Re-freezes of `svg/`*, item 4) and are still byte-identical to
`diagrams/`'s. No ARXML or RST digest moved (`test_diagram_switch_byte_identity.py`, unchanged);
the byte-identity records' diagram digests moved as their fourteenth move, after their recorded
digests were answered by the generators before the change.

### 15. A lit sequence message points with a blue head

A lit sequence message's head takes the
highlight's blue as its line already did. The sequence renderer defines the lit twin of each of its
two heads — `swcraft-arrow-lit` (filled) and `swcraft-arrow-open-lit` (open), both `#1a5fb4` — and
the page's style swaps a lit message's `marker-end` to the twin of the head it carries: an
activation to the filled one, a data message, the self-message included, to the open one. Moved:
every `.html` of every directory (the style's two rules, 123 bytes each, plus whatever its inlined
SVG moved); every sequence `.svg` by its `<defs>` line, 460 bytes each —
`basic/sequence-task10ms_consumer_cycle.svg` 2 977 → 3 437, `diagrams/sequence-speed_cycle.svg`
4 873 → 5 333, `showcase/sequence-baseline_50ms_cycle.svg` 5 224 → 5 684; and those views' size
lines in `report.txt` and in `index.html`'s copy of it. The diff was read file by file: every page
differs from its previous bytes only in the style's message rules, its inlined SVG's `<defs>` line
(sequence pages alone) and nothing else; every moved SVG only in its `<defs>` line; every
`index.html` and `report.txt` only in a sequence view's byte count. No IBD, overview, `nesting` or
`deployment` SVG moved, no view appeared or vanished, and `fixtures/diagrams/laid-out/` did not
move. The `fixtures/diagrams/svg/` file moved by the same bytes (that directory's README,
§ *Re-freezes of `svg/`*, item 5) and is still byte-identical to `diagrams/`'s. No ARXML or RST
digest moved (`test_diagram_switch_byte_identity.py`, unchanged); the byte-identity records'
diagram digests moved as their fifteenth move, after their recorded digests were answered by the
generators before the change.

### 16. A line ends inside its arrowhead

2026-11-06: on a lit sequence message the line's square end showed past the head's point, and on
an unlit one less plainly. A butt line end is as wide as its stroke and the head has no width at its tip, so every
pointing line now stops `svg.HEAD_RETREAT` (4 px, where the head is as wide as the widest stroke the
page gives a line, the search's 4 px) short of where it arrives, and every arrowhead marker is
anchored there (`refX="8"` → `refX="4"`), putting the tip back on the old end. Moved: every sequence
and overview `.svg` by its `<defs>` line and by the last point of each line that carries a head —
the sequences' activations, data messages and self-message returns, the overviews' bundles — and
every `.html` that inlines one of them by the same lines; ten SVGs and ten pages in all
(`basic/overview-Root`, `basic/sequence-task10ms_consumer_cycle`, `compositions/overview-Root`,
`-Sink`, `-Sub`, `diagrams/overview-Chassis`, `-Vehicle`, `diagrams/sequence-speed_cycle`,
`showcase/overview-VehicleControl`, `showcase/sequence-baseline_50ms_cycle`). The diff was read line
by line: 23 changed lines are `<defs>` lines differing only in `refX`, and 35 are pointing lines
differing only in their path's last point, 4 px back along the last segment. No file changed size,
so no byte count in `report.txt` or `index.html` moved; no IBD, `nesting` or `deployment` SVG moved,
no view appeared or vanished, and `fixtures/diagrams/laid-out/` did not move. The
`fixtures/diagrams/svg/` files moved by the same bytes (that directory's README, § *Re-freezes of
`svg/`*, item 6) and are still byte-identical to `diagrams/`'s. No ARXML or RST digest moved
(`test_diagram_switch_byte_identity.py`, unchanged); the byte-identity records' diagram digests moved
as their sixteenth move, after their recorded digests were answered by the generators before the
change.

### 17. Each page names what its drawn elements open

Diagrams in a VS Code tab (2026-11-08): a diagram opened in a VS Code
tab opens the YAML file of the element a reader clicks, so the generator writes the storage names.
Every view page gains one JSON data block after the way-up block, `<script type="application/json"
id="swcraft-model">`, mapping a drawn element's ordinal among the view's `[data-role]` elements to
`{"file": <model-root-relative storage name>, "member": <path> | null}` (or `{"refused": …}`), and
`index.html` gains one before `</body>`, `id="swcraft-elements"`, mapping each storage name to the
views that draw it and, per member path, the views that draw that member. Moved: one added line in
each of the 28 `.html` files of the four directories and nothing else; the diff was read file by
file — 28 lines added, none removed. **No `.svg` and no `report.txt` moved**: the extractor's entries
reach the page through a temporary `data-swcraft-source` attribute the page assembly strips before
anything reads the text, and `test_diagram_generation.py` holds every golden view's stripped text
equal to its renderer's plain call. `fixtures/diagrams/laid-out/` was re-recorded first, because
the extractor's metadata carries each element's entry through ELK (that directory's README); its
diff is the `opens` keys alone, and `fixtures/diagrams/svg/` did not move. No ARXML or RST digest
moved (`test_diagram_switch_byte_identity.py`, unchanged); the byte-identity records' diagram
digests moved as their seventeenth move, the file counts unmoved.

### 18. The page script's host seam, the open gesture, the reveal and the hit band

Diagrams in a VS Code tab (2026-11-08): the page script that every view page inlines gains the seam a VS Code tab's bridge
plugs into (`window.swcraftHost`: the links, the way up and **Reload** go through it when it is
there, through `window.location` exactly as before when it is not), the Ctrl/Cmd+click that opens
a drawn element's file, the selection and its bar under the picture with **Open in model**, the
reveal of Show in Diagram through the search's own marks, and the **hit band** — once at start
every port, line and message gains a transparent clone of its hit shape, 8 CSS px wide at every
zoom, without a `data-role`, so a small target is hit where the eye places it; a page rule, so a
browser has it too. The page style gains the band's rule, the `selected` outline and the bar's
rules, and every mark and highlight rule that reaches a rect or a path excludes the band. Moved:
the 24 view pages of the four directories, each by its `<style>` block, one added line before the
status line (`<div id="selection" hidden>…</div>`, hidden, with the button hidden in it) and its
`<script>` block, and nothing else — compared page by page with those three removed from both
sides, equal. **No `.svg`, no `index.html` and no `report.txt` moved** (the index carries no
script); `fixtures/diagrams/svg/` and `laid-out/` did not move. No ARXML or RST digest moved; the
byte-identity records' diagram digests moved as their eighteenth move, the file counts unmoved.

### 19. The tooltip is held hidden after an open

Diagrams in a VS Code tab (2026-11-08): in a real VS Code tab Chromium re-sends `pointerover` at the element just opened when
the button is released, and the editor beside then takes the focus, so the tooltip came back after
a Ctrl/Cmd+click and stayed. With a host the page script now holds the tooltip hidden after it
posts `open` and on the window's `blur`, until a `pointermove` away from where the hold began;
without a host nothing holds it. Moved: the 24 view pages of the four directories, each by its
`<script>` block alone (22 lines added, 1 changed). **No `.svg`, no `index.html` and no
`report.txt` moved.** No ARXML or RST digest moved; the byte-identity records' diagram digests moved
as their nineteenth move, the file counts unmoved.

### 20. The reveal's marks are transient, and its name is the search box's value

Diagrams in a VS Code tab (2026-11-08): with
a host, the first click on the canvas — on an element or on empty canvas, a press released without
a move — clears a Show in Diagram reveal's marks, the search box's value and the reveal's status
line, and still does what that click does; a drag clears nothing, and a search the user types stays.
The revealed name is now the search box's real value, wholly selected, instead of its placeholder.
Moved: the 24 view pages of the four directories, each by its `<script>` block alone (24 lines
added, 8 removed). **No `.svg`, no `index.html` and no `report.txt` moved.** No ARXML or RST digest
moved; the byte-identity records' diagram digests moved as their twentieth move, the file counts
unmoved.

### 21. The reveal is a selection, never a search

Diagrams in a VS Code tab (2026-11-08; a jump is a jump, and a search is a search): with a host, Show in Diagram selects
the element exactly as a click on it does — pinned or outlined, the bar with **Open in model** — and
centres it; the search box, its marks and the status line are not touched, and an element drawn more
than once on the view gets *1 of n on this diagram* and a **Next** button in the selection bar. The
previous section's transient marks and search value are gone with the marks they served. Moved: the
24 view pages of the four directories, each by its `<script>` block and the selection bar's one new
button (`#selection-next`), nothing else (checked with both removed from the two sides: equal).
**No `.svg`, no `index.html` and no `report.txt` moved.** No ARXML or RST digest moved; the
byte-identity records' diagram digests moved as their twenty-first move, the file counts unmoved.

### 22. The page script's robustness fixes

Diagrams in a VS Code tab (2026-11-08), no visible behaviour moved:
the page reads its model block only with a host, and a block that is not JSON is read as no entry,
so pan, zoom and search still start (before, the parse threw at load); the selection bar's null
checks and the guard on `host.on` are gone, since the generator always writes the bar and the host
contract requires `on`. Moved: the 24 view pages of the four directories, each by its `<script>`
block, nothing else (checked with the script removed from the two sides: equal). **No `.svg`, no
`index.html` and no `report.txt` moved.** No ARXML or RST digest moved; the byte-identity records'
diagram digests moved as their twenty-second move, the file counts unmoved.

### 23. The reveal's offer dropped on every path

Diagrams in a VS Code tab (2026-11-08): a Ctrl/Cmd+click that opens after
the reveal of an element drawn twice emptied the reveal's list but left *1 of 2 on this diagram* and
**Next** on the bar, and Next did nothing; now every path that ends the offer (`dropReveal`) hides
Next and takes the count off the line, the selection staying. The open and Next buttons are wired
through the variables already holding them. Moved: the 24 view pages of the four directories, each
by its `<script>` block, nothing else (checked with the script removed from the two sides: equal).
**No `.svg`, no `index.html` and no `report.txt` moved.** No ARXML or RST digest moved; the
byte-identity records' diagram digests moved as their twenty-third move, the file counts unmoved.

### 24. The capped port list's close button ends the selection

Diagrams in a VS Code tab (2026-11-08): the close button of
a capped port's member list cleared the pin's highlight but left the selection and the bar, so
**Open in model** still targeted a port no longer shown selected; it now ends the pin and the
selection together (`select(null, null)`), as a second click on the same port always did. Moved:
the 24 view pages of the four directories, each by its `<script>` block, nothing else (three added
lines, the same in every page). **No `.svg`, no `index.html` and no `report.txt` moved.** No ARXML
or RST digest moved; the byte-identity records' diagram digests moved as their twenty-fourth move,
the file counts unmoved.

### 25. Each label carries its element's kind

2026-11-09: the diagrams' VS Code tab takes LikeC4 1.60.0's palette with two changes, and one of
them — in the light theme a port label inside a component box takes the box's own text colour — and LikeC4's own split of a compound's heading from an element's
label need the tab to tell a label by the box it labels. The labels are drawn in a layer of their own,
apart from their boxes, so the stylesheet could tell them only by a label's anchor or its place; the
renderers now write the mark instead: every `member-label`, `frame-label` and `lifeline-label` gains
`data-kind`, the kind of the element it names (a block diagram's frame `composition`, the nesting and
`deployment` views' boxes their own kind, a sequence's frame `sequence`, a lifeline `task` or
`component`), and the label of a member's port gains the second class `in-member`. **The files'
colours did not move**: the SVG files and the standalone pages stay white; only the tab reads the
marks. Moved: every `.svg` that draws such a label (all 24 of the four directories), each page by its
inlined SVG, and the size lines in `report.txt` and in `index.html`'s copy of it. Nothing else: the
diff was read file by file with the new attribute and class removed from both sides — every file
answered its previous bytes except the byte counts in `report.txt` and `index.html`. No coordinate
moved, no view appeared or vanished, and `fixtures/diagrams/laid-out/` did not move;
`fixtures/diagrams/svg/` moved by the same bytes (that directory's README, § *Re-freezes of `svg/`*,
item 7). No ARXML or RST digest moved; the byte-identity records' diagram digests moved as their
twenty-fifth move, the file counts unmoved.

### 26. A member's port label carries its member's kind

In the VS Code tab's light and dark
themes a port label inside a member **composition** takes the grey a port label has outside a box,
while one inside a member **component** keeps the component's text colour (high contrast unchanged).
Both are `in-member` labels drawn in the labels layer, apart from their member's box, so the renderer
now writes the member's kind on each: `class="port-label in-member"` gains `data-kind`
(`component` or `composition`), the convention section 25 set for the box labels. **The files' colours
did not move.** Moved: every block diagram `.svg` with a member's port (`basic/ibd-Root`,
`compositions/ibd-Root`, `ibd-Sink`, `ibd-Sub`, `diagrams/ibd-Chassis`, `ibd-Vehicle`,
`showcase/ibd-VehicleControl`), each page by its inlined SVG, and the size lines in `report.txt` and in
`index.html`'s copy of it. The diff was read file by file with `data-kind` removed from both sides:
every file answered its previous bytes except those size lines. No coordinate moved, no view appeared
or vanished, `fixtures/diagrams/laid-out/` did not move; `fixtures/diagrams/svg/` moved by the same
bytes (that directory's README, § *Re-freezes of `svg/`*, item 8). No ARXML or RST digest moved; the
byte-identity records' diagram digests moved as their twenty-sixth move, the file counts unmoved.

### 27. The root composition's description loses a claim about a tool export

2026-11-13, the open copy: `fixtures/compositions/model/compositions/Root.yaml`'s description no
longer states what a private export's top level holds (it says the shape is one a tool export's top
level can have). Moved: only that sentence inside `compositions/ibd-Root` and `overview-Root` (each
`.svg` and its page) and, as their consequence, those views' byte counts in `compositions/index.html`
and `report.txt`. No coordinate moved, no view appeared or vanished, no other directory moved, and
`fixtures/diagrams/` is a different model and did not move. The byte-identity records' compositions
cells moved with it and were re-pinned with a dated note.

### 28. With a host, the status line no longer repeats the selection

2026-11-15: in the VS Code tab a click on an element with an entry
showed its title twice, in the selection bar and in the status line under it. The page script now
writes the status line in one place (`renderStatus()`): the title of what is lit, except that with a
host it stays empty while something is selected; `light()` and every exit of `select()` call it. A
browser (no host) shows what it showed. Moved: the 24 view pages of the four directories, each by its
`<script>` block, nothing else (the diff read line by line: only the script's added and removed lines,
the same in every page). **No `.svg`, no `index.html` and no `report.txt` moved.** No ARXML or RST
digest moved; the byte-identity records' diagram digests move as their twenty-seventh move, the file
counts unmoved.
Re-frozen once more the same day for one comment of the script (a reveal no longer claims to leave
the status line alone): the same 24 pages by the same two lines each, nothing else; the records'
twenty-seventh move is taken on that script.
