# See the model as diagrams

**Model → Diagrams** (or `SWCraft: Diagrams`) runs `swcraft generate diagrams`, which writes the
model's diagrams as files into the directory the manifest declares as `views_root` — `views/` in the
shipped example — and then shows `index.html` in a tab of the editor. Nothing is served and nothing
listens on a port: every page is a file, with its drawing inside it, and the tab reads it. To look at
a page in a browser, open it from the file; to share a picture, take its SVG file.

The layout is computed when the files are written, by Eclipse ELK running on Node.js. An
installation from the SWCraft workstation archive carries that Node.js, so there is nothing else to
install; without it, the command looks for `node` on `PATH` and, finding none, refuses in one
sentence that names what it needs. A model the generator refuses shows its refusal in the
**SWCraft** output channel, and nothing in `views_root` changes.

## The views, and which question each answers

| View | What it holds | Emitted |
|---|---|---|
| internal block diagram (`ibd-<composition>`) | one composition's members with every port, the connectors between them, and the composition's own boundary ports with their delegations drawn to its frame | per composition |
| overview (`overview-<composition>`) | the same members as boxes, one line per connected pair carrying the number of connectors | per composition |
| nesting (`nesting`) | the composition tree as boxes in boxes, no lines | when a composition declares `root: true` |
| deployment (`deployment`) | the processor, its cores, the tasks on each core and the components each task runs, with their safety levels | when the model declares a task |
| sequence diagram (`sequence-<sequence>`) | one scenario: the task's lifeline, the components in order of first appearance, and a step per runnable | per declared sequence |

The internal block diagram answers *how is this wired, port by port*; the overview answers the same
question at a scale you can read when a composition holds hundreds of connectors; the nesting view
answers *what sits inside what*; the deployment view answers *what runs where*; a sequence diagram answers *what happens, in what order*.

## Reading a page

Every page pans with a drag and zooms with the wheel, the **+** and **-** buttons or the keys, and
**Fit** returns to the whole view. **Search** marks every drawn element whose name contains what you
type and moves to each in turn. A member that is itself a composition is a link: click it to open
that composition's own page. The index lists every view.

**From the picture to the model and back.** In the tab, Ctrl+click (Cmd+click on macOS) on a drawn
element — or click it and press **Open in model** under the picture — opens its YAML file beside the
diagram, at its line. The other way, the `diagram` line above an element's name in a model file, or
**SWCraft: Show in Diagram** in the editor's context menu, opens the diagram with that element
selected and centred. The links between the pages stay in the tab, with **← Back** and
**Forward →**, and the tab follows your colour theme.

A saved change to the model regenerates nothing: the **Diagrams** item turns *out of date* instead,
and the open tab shows a line saying so, with a **Generate** button. Press either, and the tab shows
the new drawing; the page's **Reload** button reads the page shown from the disk again. **Build** removes `views_root` with the other outputs and writes it again; a
build that fails before that step leaves it absent, and the next **Diagrams** press writes it back.

## What was not drawn

Nothing the model declares is left out in silence. `report.txt`, beside the pages and shown on the
index, lists every view with its size and then, counted and grouped by reason, everything a view
could not place — a connector naming a member the composition does not have, a sequence step naming
an undeclared runnable — and, in a section of its own, every component connected to itself, which is
drawn as a loop.

**And the standing rule holds here too:** a diagram is not evidence. The pages carry no
qualification claim, are not read by `validate` or `check-model`, and are not an input to any
generator.
