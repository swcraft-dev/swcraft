# `fixtures/diagrams/model` — the diagram extractor's invented model

A committed model tree used **in place**, on the precedent of `fixtures/compositions/model`:
`conftest.GOLDEN_MODEL_ROOTS` does not name it, and the only committed files generated from it
are the renderer's recorded layouts and SVG goldens beside it (§ *The recorded layouts and the SVG
goldens* below) and the generator's whole output under `fixtures/diagrams_golden/diagrams/`, whose SVG
files `packages/tools/tests/test_diagram_generation.py` holds equal to the ones here. It exists so the diagram extractor (`writer/diagrams/graph.py`) has one small
model holding every shape its views have to draw, which neither `examples/basic` (one connector,
one level) nor `fixtures/compositions` (no self-connector, no sequence) holds alone. It passes
`check-model`; the shapes a valid model cannot hold are written into a copy under `tmp_path` by
`packages/tools/tests/test_diagram_graph.py`.

What every element in it is for:

| Element | Why it is here |
|---|---|
| `compositions/Vehicle.yaml` | the root (`root: true`), so the nesting tree has a top and a connector naming no composition has a home; one member is a composition, the other (`Diag`) a component whose **member name differs from its type** (`DiagServer`), so a box label and an endpoint resolution both see the difference |
| `compositions/Chassis.yaml` | the nested composition: two boundary ports, three members and a **delegation in each direction** — `Chassis_rDiag` inward from a require port, `Chassis_pSpeedOut` outward to a provide port |
| `swc/chassis/Sensor.yaml` | two provide ports; its runnable declares `writes` on one of them, so a sequence's data messages are **narrowed** to that port |
| `swc/chassis/Controller.yaml` | six ports, among them `pLoop`/`rLoop` for the self-connector and `rDiag` for the client side of the client-server pair; its runnable declares no `writes`, so every provide-port connector becomes a message |
| `swc/chassis/Logger.yaml` | the **unconnected port** (`rLog`), drawn all the same |
| `swc/chassis/DiagServer.yaml` | the server side of the client-server pair, placed in the root and wired **across the nesting** into `Chassis` |
| `connectors/speed_link.yaml`, `raw_link.yaml` | two connectors between one member pair, so the bundled overview counts two |
| `connectors/control_loop.yaml` | the **self-connector**: both ends on `Controller`, the one item drawn unusually and reported as a loop |
| `connectors/diag_link.yaml` | the root's connector; it declares **no** `composition` and names a composition-typed endpoint |
| `interfaces/ISpeed.yaml`, `IDiag.yaml` | one sender-receiver and one client-server interface, so both port glyph families are reached |
| `tasks/Task_10ms.yaml`, `sequences/speed_cycle.yaml` | the one scenario: two steps, the second with a note |
| `requirements/REQ_DIAGRAMS_001.yaml`, `data_types/` | what `check-model` requires of any model and nothing the diagrams read. The requirement's own `description` still says `check-model` warns about each declared component that satisfies none: it predates contract `12.0.0`, which removed that warning, and is kept unchanged as known-stale test data: the RST column of `test_diagram_switch_byte_identity.py`'s record was measured over it. That column is history no case reads since the RST generator left, so no byte-identity record holds the description any more. |
| `hardware/CPU.yaml`, `CORE0.yaml`, `PARTITION0.yaml` | what `check-model` requires of any model, and the `deployment` view's frame: the processor holding its core, the core holding the unrated partition `Task_10ms` names, which declares `trust: trusted` so the view's trust mark has a golden |

## The recorded layouts and the SVG goldens

The SVG renderer (`writer/diagrams/svg.py`) never calls the layout bridge, so its tests
(`packages/tools/tests/test_diagram_svg.py`) read layouts recorded once from this model:

| Directory | What it holds | Written by |
|---|---|---|
| `laid-out/` | the four ELK graphs the extractor builds from `model/` (`ibd-Chassis`, `overview-Chassis`, `ibd-Vehicle`, `overview-Vehicle`), laid out through the real bridge and written as the bridge returned them — ELK's own bookkeeping key `$H` included | `python packages/tools/tests/test_diagram_svg.py --record-layouts` (needs Node) |
| `svg/` | the renderer's SVG of each recorded layout, byte for byte; and the two views drawn from Python grids rather than ELK layouts — `nesting.svg` (`writer/diagrams/nesting.py`) and `sequence-speed_cycle.svg` (`writer/diagrams/sequence.py`) — rendered straight from the extractor's records, which need no recording — and `deployment.svg` (`writer/diagrams/deployment.py`), drawn the same way by the nesting renderer | `python packages/tools/tests/test_diagram_svg.py --freeze-svg` |

Both are written by those two commands and never by hand, and both are **change detectors**, not
correctness proofs: a diff in `svg/` is the renderer changing, and whether the new picture is right
is read in a browser, never inferred from the test going green again. Two checks keep `laid-out/`
honest: without Node, every key and value the extractor builds today must still be in the recorded
graph (ELK only adds positions); with Node, laying the extractor's graphs out again must answer the
recorded bytes. A change to `graph.py` that moves a graph therefore fails one of them until the
layouts are re-recorded and the goldens re-frozen, in that order, with the reason in the commit.

**Re-recordings of `laid-out/` that moved no SVG.** 2026-11-08 (diagrams in a VS Code tab):
every node's and edge's `swcraft` metadata gained an `opens` key, the file a click on that
element opens (`graph.OPENS_KEY`); ELK carries it through untouched. The four recordings moved by
those keys alone and `svg/` did not move (`fixtures/diagrams_golden/README.md`, section 17).

## Re-freezes of `svg/`

Recorded here from contract `9.0.0` on; the earlier ones are in `fixtures/diagrams_golden/README.md`
§ *Re-freezes*, whose `diagrams/` SVG files are the same bytes as the ones here.

1. **The `deployment` view gains the partition level** (contract `9.0.0`). A task names a partition
   and runs on that partition's core, so `deployment.svg` draws `PARTITION0` (unrated, so headed
   with its name alone, in its own fill `#f3e5f5`) between `CORE0` and `Task_10ms`: one more
   `member partition` group with its `<title>`, one more label, every box below the core moved one
   level in (12 px right, 26 px down), and the canvas grew from 364 x 190 to 388 x 228 px. No other
   file `--freeze-svg` writes moved, and `deployment.svg` is still byte-identical to
   `fixtures/diagrams_golden/diagrams/deployment.svg` (the same section 11 there).
2. **A trusted partition carries a trust mark** (contract `9.1.0`). `PARTITION0` declares
   `trust: trusted` and stays unrated, so `deployment.svg` heads it `PARTITION0 — trusted` and its
   `<title>` gains the line `trusted`: 2 345 → 2 365 bytes, the box and the 388 x 228 px canvas
   unchanged. No other file `--freeze-svg` writes moved, and `deployment.svg` is still byte-identical
   to `fixtures/diagrams_golden/diagrams/deployment.svg` (section 12 there).
3. **The 0.16.2 drawing** (contract `10.1.1`). Each of the two IBDs gained one `rect.port-hit`
   (opacity 0) last in every port group and `stroke-dasharray:none` in its hit-area rule, and each
   of the two overviews its hit-area rule's `stroke-dasharray:none` and its lit arrow marker
   filled `#1a5fb4` rather than `#000000`. No coordinate, glyph or label moved, and neither
   `nesting.svg`, `deployment.svg` nor `sequence-speed_cycle.svg` moved: the model holds no stub
   port, no undeclared interface and no line the validator does not judge. `laid-out/` did not
   move (the extractor's graph is unchanged for this model). The four files are still
   byte-identical to `fixtures/diagrams_golden/diagrams/`'s (section 13 there).
4. **A port's hover lists its interface's content** (2026-11-02; no contract version moved). The
   renderers take the extractor's interface map, and `--freeze-svg` hands them the model's own
   (`graph.build_graphs(model).interface_contents`): every port title of the two IBDs gained its
   interface's member line after its head — `· speed: uint8` for `ISpeed`, `· ReadStatus(out
   status: uint8)` for `IDiag` — and each data message of `sequence-speed_cycle.svg` gained
   `· speed: uint8`. `ibd-Chassis.svg` 11 896 → 12 106 bytes, `ibd-Vehicle.svg` 4 056 → 4 138,
   `sequence-speed_cycle.svg` 4 841 → 4 873. Only `<title>` texts of ports and data messages
   moved: no coordinate, no other element, and no `<desc class="member-list">`, because no
   interface of `model/` is over the eight-line cap (the capped form is tested on a synthetic map
   over `laid-out/`, which keeps `model/`'s RST digest out of it). The overviews, `nesting.svg` and
   `deployment.svg` did not move, nor did `laid-out/`: the member lines travel beside the ELK
   graphs, never in them. The seven files are still byte-identical to
   `fixtures/diagrams_golden/diagrams/`'s (section 14 there).
5. **A lit sequence message points with a blue head** (2026-11-05; no contract version moved).
   `sequence-speed_cycle.svg`'s `<defs>` line gained the lit twins of its two heads after them —
   `swcraft-arrow-lit` (the filled head, `fill="#1a5fb4"`) and `swcraft-arrow-open-lit` (the open
   head, `stroke="#1a5fb4"`), the same geometry as the unlit ones — 4 873 → 5 333 bytes. Nothing
   else in it moved: every message's `marker-end` still names its unlit head, the page's style
   swaps it. No other file `--freeze-svg` writes moved (a block diagram already defines
   `swcraft-arrow-lit` and gains no open twin), nor did `laid-out/`. The file is still
   byte-identical to `fixtures/diagrams_golden/diagrams/sequence-speed_cycle.svg` (section 15
   there).
6. **A line ends inside its arrowhead** (2026-11-06; no contract version moved). A pointing line's
   square end showed beside the head's point, so every arrowhead marker is anchored half a head
   behind its tip (`refX="8"` → `refX="4"`, every marker of every `<defs>` line) and every line
   that points stops `svg.HEAD_RETREAT` (4 px) short of where it arrives, along its last segment:
   in `sequence-speed_cycle.svg` the two activations (`215` → `211`, `389.8` → `385.8`), the data
   message (`394.8` → `390.8`) and the self-message's return (`399.8` → `403.8`); in
   `overview-Chassis.svg` its two bundles' last points, in `overview-Vehicle.svg` its one. Each
   head's tip lands where the line used to end. No byte count moved (every changed number kept its
   length), nor did any other coordinate, element, `nesting.svg`, `deployment.svg`, an IBD (no
   marker) or `laid-out/`. The three files are still byte-identical to
   `fixtures/diagrams_golden/diagrams/`'s (section 16 there).
7. **Each label carries its element's kind** (2026-11-09; no contract version moved). The VS Code
   tab colours a box's label as the box's own text, and a label lies in a layer apart from its box, so the renderers mark it: every `member-label`, `frame-label` and
   `lifeline-label` gains `data-kind` (the kind of the element it names; a block diagram's frame
   `composition`, a sequence's frame `sequence`), and a member's port label the second class
   `in-member` (`class="port-label in-member"`). No colour, coordinate or other element moved, nor
   `laid-out/`. Byte counts: `deployment.svg` 2 365 → 2 487, `ibd-Chassis.svg` 12 106 → 12 286,
   `ibd-Vehicle.svg` 4 138 → 4 238, `nesting.svg` 2 425 → 2 561, `overview-Chassis.svg` 4 256 → 4 346,
   `overview-Vehicle.svg` 2 777 → 2 847, `sequence-speed_cycle.svg` 5 333 → 5 415. The seven files
   are still byte-identical to `fixtures/diagrams_golden/diagrams/`'s (section 25 there).
8. **A member's port label carries its member's kind** (2026-11-09; no contract version moved).
   A port label inside a member composition takes the port label grey of the outside while one
   inside a component keeps the component's text colour, so the tab must tell the two apart: every `in-member` port label gains its
   member's `data-kind` (`class="port-label in-member" data-kind="component"` or `"composition"`). No
   colour, coordinate or other element moved, nor `laid-out/`. Byte counts: `ibd-Chassis.svg`
   12 286 → 12 484, `ibd-Vehicle.svg` 4 238 → 4 308. The two files are still byte-identical to
   `fixtures/diagrams_golden/diagrams/`'s (section 26 there).
