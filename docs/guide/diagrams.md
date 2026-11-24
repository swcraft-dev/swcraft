<!-- draft: not yet reviewed by the product's author -->

# Diagrams

<!-- purpose: When you want to see the model drawn: the views SWCraft writes and how to open them -->

SWCraft draws the model as UML/SysML views and writes them as files: an SVG drawing and an HTML
page for every view, an index page over them, and a report of anything it could not draw. Nothing
is served and nothing listens on a port. In VS Code the pages open in a tab of the editor, where a
drawn element leads to its file in the model and back; outside VS Code you open the index in a
browser, and every page works from the file it is in. The SVG files are the pictures to share: they
carry no link and no file name.

Nothing about a diagram is edited by hand. If a view is wrong, the model is wrong, and the fix is
in the model.

## Generating the views { #generating-the-views }

<!-- purpose: Generating the diagram files for a project, and where they are written -->

```sh
swcraft generate diagrams --project swcraft.yaml
```

That writes into the directory your manifest declares as `views_root` — `views/` in a project made
by `swcraft init`. The command prints one line per view, saying whether its files were **written**
or **kept** because nothing in them changed, one line per stale file it removed, how long the
layout took and where the report is:

```text
Diagram ibd-Root: written
Diagram overview-Root: written
Diagram nesting: written
Diagram deployment: written
Diagram sequence-task10ms_consumer_cycle: written
Layout: 0.10 s
Report: <project>/views/report.txt
Generated diagrams: <project>/views (5 views)
```

The drawings are laid out when the files are written, by Eclipse ELK, which SWCraft carries inside
its own installation and runs on Node.js. The Node.js the SWCraft workstation archive installs is
used first; without one, `node` on your `PATH` is. Finding none, or a Node.js older than 14.8.0,
the command stops with one sentence saying so.

**Nothing is written until every file is ready.** A model that cannot be read, a Node.js that is
missing or too old, or a layout that fails leaves `views_root` exactly as it was — and does not
create it when it is absent. The same holds for a view whose name cannot be a file name, or a
composition or sequence name declared twice: the command refuses them by name and points you at
`swcraft check-model`, which reports the model behind them. Three refusals `check-model` does not
share: a view whose file name would be longer than the 255 bytes a file system holds — a very long
sequence name, `sequence-` and `.html` included, or a name that fits until a second name equal to it
but for capitalisation gives it a `~1` — is refused naming the view; model text holding a character
no SVG can carry — a control character, spelled in YAML as an escape such as `"\x01"` — is refused
naming its code point; and a link or a folder in `views_root` named like a file the command writes is refused naming it, so
a link you made is never replaced. A lone surrogate, spelled as an escape such as `"\uD800"`, does
not get that far: reading the model refuses the file that holds it, naming the file, like any other
model file that cannot be read. The command never writes through a link in any case: each
changed file is written as a new file and moved into place under its name.

**Two names that differ only in capitalisation get two sets of files.** `Chassis` and `chassis`
are two compositions, but on Windows and macOS `ibd-Chassis.svg` and `ibd-chassis.svg` would be one
file, so the second in the model's order is written as `ibd-chassis~1.svg` (and `.html`) — the
same suffix SWCraft gives a model file in that case. The suffix belongs to the file alone: the page,
its links and the report name the view by its file, and the drawing still says `chassis`.

**A later run removes only what an earlier run wrote.** A view that no longer exists — a sequence
you deleted, a composition you renamed — has its two files removed. Any other file in `views_root`
is left alone: a `README.md`, a `.gitkeep`, a directory of your own. Emptying the directory is
`swcraft clean-outputs`'s job, which a build runs first.

## The views { #the-views }

<!-- purpose: Which views SWCraft draws, their files, what each one holds and when it is written -->

| View | Files | What it holds | Emitted |
|---|---|---|---|
| internal block diagram | `ibd-<composition>.svg`, `.html` | one composition's members with every port, the connectors between them, and the composition's own boundary ports with their delegations drawn to its frame | per composition |
| overview | `overview-<composition>.svg`, `.html` | the same members as boxes, one line per connected pair carrying the number of connectors | per composition |
| nesting | `nesting.svg`, `.html` | the composition tree as boxes in boxes, starting at the root | when a composition declares `root: true` |
| deployment | `deployment.svg`, `.html` | the processor, its cores, the partitions on each core, the tasks in each partition and the components each task runs, with their safety levels | when the model declares a task |
| sequence diagram | `sequence-<sequence>.svg`, `.html` | one scenario: the task, the components it activates, and what they send | per declared sequence |

Beside them, `index.html` — titled with the `name` your manifest declares — lists every view,
grouped as compositions, nesting, deployment and sequences, and shows the report; `report.txt` is the report on
its own.

The internal block diagram answers *how is this wired, port by port*; the overview answers the same
question at a scale you can still read when a composition holds hundreds of connectors; the nesting
view answers *what sits inside what*; the deployment view answers *what runs where*; a sequence
diagram answers *what happens, in what order*.

**The SVG files are the drawings to reuse** — attach one to a mail, put one on a wiki page, open one
in a vector editor. The HTML pages are for looking: each carries its own drawing inside it, which is
what lets it search that drawing when opened straight from a file.

## Reading an internal block diagram { #reading-an-internal-block-diagram }

<!-- purpose: Reading an internal block diagram: members, every port, connectors and boundary delegations -->

Each member of the composition is a box, labelled with the member's name, or `<member> : <type>`
when the member's name differs from the component or composition type it places. **Every port its
type declares is drawn**, connected or not — a port hanging on a box with no line is information,
not noise. Ports that require sit on a box's left side and ports that provide on its right.

A port is drawn after AUTOSAR's VFB port figure: a small square on the border, and outside it a
shape as high as the square that says both the interface kind and the direction — never a fill:

- a **sender-receiver provide** port (a sender) is an arrow on a short stem, pointing away from
  the square;
- a **sender-receiver require** port (a receiver) is a fork of two prongs on a stem, opening away
  from it;
- a **client-server provide** port (a server) is an empty circle on a stem;
- a **client-server require** port (a client) is a half circle on a stem, opening away from it.

Every one of the four sits on a stem, so the square, the stem and the shape read as one glyph
however small the picture is drawn.

**The square is the port; the shape says its type.** A port is drawn with a shape only when the
model declares its interface's kind; with no type there is no shape, and the diagram invents none.
A port on an **external interface** — one declared outside the project, which the model knows only
by its path and the kind the reference named ([Working with the model](model.md)) — is drawn with
the client-server shapes when that kind is `CLIENT-SERVER-INTERFACE` and the sender-receiver shapes
when it is `SENDER-RECEIVER-INTERFACE`; rest the pointer on it and its text ends in `(external)`,
where a declared port's says `(sender-receiver)` or `(client-server)`. Every other port is the
**bare square**, the port with no shape beside it:

- a port on an external interface of any other kind — mode-switch, and the two general values
  `PORT-INTERFACE` and `DATA-INTERFACE` among them — whose text also ends in `(external)`;
- a port whose interface the model does not declare, or names but cannot find, whose text ends in
  `(undeclared interface)`;
- a port of an **external component** — a component placed in a composition whose type is
  declared outside the project — which declares no interface: the model knows only its name and
  direction, and its text ends in `(external component, interface not declared)`. Where the import
  saw one interface on the port's other ends, it records it on the port as `observed_interface`;
  that is a hint, not a declaration, so the diagram does not draw it — `swcraft query` shows it.

**A dashed line is a connection nobody checked.** A connector or a delegation is drawn dashed where
the model check cannot judge whether its two ports fit: an end on an external component's port, two
different external interfaces, a declared interface against an external one, or a port that
declares no interface against an external one. The connection may be right or wrong — the other
file would tell, and the model does not hold it. Rest the pointer on a dashed line and its text
says why, on the line under its name. A solid line is one the check judges: where it finds the two
ports do not fit — an external interface of the other kind, or two directions that disagree —
`swcraft check-model` reports it, and the line stays solid. An overview's numbered line stays
solid even when some of its connectors are dashed on the internal block diagram, and a sequence
diagram's dashed arrows keep their own meaning (a data flow after a step, below); they say nothing
about whether a connection was checked.

A port's name is written beside it, inside the box it belongs to, so a long name runs into the box
and never over the shape.

**A connector is a line from a provide port to a require port**, ending where each shape ends — on
the arrow's point, on the fork's prongs, on the circle's edge and on the half circle's mouth.
The composition's own boundary ports sit on its frame, pointing **outwards** — they are the
composition's ports toward whatever is outside it, so their shapes face the environment exactly as a
member's face out of its box — and **each delegation is a line from the boundary port to the member
port** it stands for, drawn inside the frame and ending at the back of the boundary square, so what
reaches the outside of the composition is visible rather than implied. A member that is itself a composition is a box with its
boundary ports; its insides are on its own page.

A component connected to itself is drawn as a loop, the way the layout routes it, and named in the
report.

## Reading an overview { #reading-an-overview }

<!-- purpose: Reading a composition overview: one line per pair of members, with the number of connectors -->

The overview draws the same members without ports. Two members that exchange anything are joined by
**one** line, whatever the number of connectors between them, and the line carries that number in a
small square. Resting the pointer on the square, or on the line, says which pair it joins and how
many connectors it stands for; **clicking the square opens the list**, beside the
picture and scrolling however long it is. Each row is a whole connector, not just its name — the
same line the internal block diagram shows when you rest the pointer on it, `<name>: <component>.<port>
-> <component>.<port>`, with the interface after it and the connector's description underneath
where one is written. The list closes as every list on these pages does ([Looking at
them](#looking-at-them)). It is the page to start from on a large composition, and the
internal block diagram is the one to open once you know which pair you want.

## Reading the deployment view { #reading-the-deployment-view }

<!-- purpose: Reading the deployment view: the processor, its cores, partitions, tasks and runnables -->

The deployment view is the floor below the compositions: where the software runs. The processor is
the outermost box; inside it each core, headed with its name and — when the core declares one — the
safety level it is qualified to host; inside each core, the partitions on it, each headed with its
name and — when the partition declares one — its own level, followed by `trusted` when the
partition is trusted (`ASIL D · trusted`, or `trusted` alone on a partition without a level);
inside each partition, the tasks that run in it; and inside each task, the components it runs, each
labelled with its **effective** safety level: the one it declares, or else the highest of the
requirements it satisfies, through its own `satisfies` or its ports' and runnables'. A core that declares no level, a partition that declares no level or
`QM` (or a decomposed `QM(Y)`) and is not trusted, and a component whose level is `QM` or `QM(Y)`, carry no label. A decomposed level is printed as written: `ASIL B(D)`. A partition no task runs in is drawn as an empty box. Memories are not drawn: a memory is not
where a task runs.

A model without tasks has no deployment view, and no files are written for it. A task whose
partition the model does not declare, a partition whose core the model does not declare — with the
tasks in it — and a component no task runs cannot be put in a box; each is named in the report.

## Reading a sequence diagram { #reading-a-sequence-diagram }

<!-- purpose: Reading a sequence diagram: one task's steps, in the task's own order -->

A scenario walks exactly one task. Its steps are the `(component, runnable)` pairs that task
executes, in the task's own order, and you may leave steps out but not reorder them — so a
sequence diagram is a readable excerpt of a real schedule rather than a story someone told about
one.

The frame is headed `sd <name>`, followed by the scenario's `title` when it has one. The first
lifeline is the task, with its period; then one lifeline per **occurrence** of a component, in the
order a step first names it, labelled the way that step writes it — the component's name for a
step written with `swc`, the path for one written with `instance`. Two steps naming the same
occurrence, one each way, share a lifeline; two occurrences of one component stand on two. Each
step is a solid arrow from the task to the component, labelled with the runnable — and, when the
step has a `note`, `<runnable> — <note>`, one step with a note rather than two steps. Below a step,
a dashed arrow runs from the component to every other component in the scenario that one of its
**provide** ports is connected to, labelled `<port> -> <port> : <interface>`; when the runnable
declares `writes`, only the ports it writes through are drawn. A connector names the component at
its far end, not the occurrence, so when two lifelines stand for that component the arrow is not
drawn: which of the two receives is not in the model, and the report lists the message instead.

## Looking at them { #looking-at-them }

<!-- purpose: Opening the diagrams, and what pointing at a picture and a click on it show you -->

Inside VS Code press **Diagrams** in the SWCraft panel, which generates the views and shows the
index in a tab of the editor ([In VS Code](vscode.md#diagrams-and-the-shortest-way-to-see-one) says
more, and [In VS Code, to the model and back](#in-vs-code-to-the-model-and-back) below what the tab
adds). Outside it, open `index.html` from the file in a browser: everything below but that section
works there too.

Every page pans with a drag and zooms with the wheel, the **+** and **−** buttons or the `+`, `-`
and `0` keys; **Fit** returns to the whole view. **Search** marks every drawn element whose name
contains what you type — a port, a component, a connector, a message — and moves to the first;
*Enter* and *Shift+Enter* or the arrow buttons step through the rest, and *Escape* clears. What
search reads is what you see when you rest the pointer on an element, and also the list a click
opens — an overview's count square's connectors, a port's whole interface — so the number on a
line is still found by the name of any one of its connectors, and by the name of a port or an
interface it joins. Since a port and a message now say what their interface carries, search also
finds the names of data elements, operations, arguments and types: `uint8` marks every port and
every data message whose interface names it. A message lists only its interface's first eight
lines, so a name past them is not found on the message.

**Resting the pointer on a line lights it up, blue and thicker, together with its two ends** — on
an internal block diagram its two ports, on an overview its two boxes and its arrowhead, on a
sequence diagram its two lifelines and its arrowhead, filled or open as the message's own — and
the line under the picture shows what the line is. A dashed line stays dashed when it is lit.
**The pointer on any port lights it** — its square and its shape, equally thick — and, when it is
connected, every line of that port and the ports at their other ends: who this port talks to. The
whole port takes the pointer, the square and its shape alike. A click keeps what is lit while you
move on; a click on empty space lets it go. Search keeps its own orange and red.

**What you rest the pointer on says what it is, at once**, in a small box next to the pointer: a
port's owner, name, direction and interface, a line's two ends and, for a dashed one, why it is
not checked, and the same for a box, a frame or the way up. A port, and a data message on a
sequence diagram, also say what their interface carries, one line each, between the first line and
the description: a sender-receiver interface's data elements with their types, `· speed: uint8`,
a client-server interface's operations with their arguments, `· ClearDtc(in group: uint8, out
result: uint8)`, and an external interface's member names alone, since the model holds no type for
them. At most eight such lines are shown; past eight, a ninth says how many are left — on a port
`and 3 more — click the port to list all 11`, on a message `and 3 more`. It follows the pointer whether or not a
click has kept something lit, and it goes when the pointer leaves, on a press and on *Escape*. A
text too long for the window is cut at the window's edge; the description it comes from is in the
model. The SVG files, opened on their own, show the same texts as the browser's own tooltip,
interface lines included; a port's whole list is in the file too, but a viewer shows nothing of it,
and the click the ninth line names opens the list on the HTML page only. A line, a port and a
sequence diagram's message do not have to be hit exactly: the pointer only needs to be within a few
pixels of it, at every zoom, and near a port the port wins over the edge of the box it sits on. Two
ports closer together than that on the screen still need a zoom to tell apart; a box's or a frame's
edge has no such margin, and a label takes no pointer.

**A click opens a list beside the picture** in two places: on an overview's count square it lists
the connectors the line stands for ([Reading an overview](#reading-an-overview) says what a row
holds), and on a port whose interface has more than eight lines it lists all of them, headed by the
port's first line. The list scrolls however long it is. A second click on the same square or port,
a click on empty space or the list's own **×** closes it; a click on the port still keeps it lit,
as on any port. A port of eight lines or fewer opens no list, and a message opens none.

A member that is itself a composition is a link: click it to open that composition's own page, and
the page's header links the index, the other view of the same composition, the nesting view and
the page's own SVG file.

**The way up.** On the page of a composition that another composition places, the composition's
own name at the top of its frame carries an `↑`: click it to open the page of the composition it is
placed in — the same kind of view — where the member you came from is lit and centred. A composition
placed in several others lists them all as *placed in* and lets you choose; the root, and a
composition nothing places, have no way up. The header also carries the whole chain from the root
down to the page you are on, each level a plain link, so the way up works without the script too.

**A page does not update itself.** Saving a model file does not generate the views again: in VS
Code the panel's **Diagrams** item turns *out of date* instead, and pressing it generates them
([In VS Code](vscode.md#out-of-date-never-regenerated-behind-your-back) describes the states). The
tab in VS Code shows the new drawing after a press or a **Build**; a page opened from a file in a
browser shows it when you press the page's **Reload** button, or your browser's reload.

### In VS Code, to the model and back { #in-vs-code-to-the-model-and-back }

<!-- purpose: Opening an element's YAML from a diagram, and finding a model element on its diagram -->

In VS Code the diagrams open in **one tab of the editor**, which every press, link and jump reuses.
Everything above works there as it does in a browser, and the tab adds the way between the picture
and the model files:

- **From the diagram to the model.** Ctrl+click (Cmd+click on macOS) a drawn element — a component,
  a port, a connector, a delegation, a task, a core, a lifeline or a message — and its YAML file
  opens in the editor beside the diagram, at the element's line: a port's, a runnable's or a
  delegation's own line in its owner's file, a connector in its own file. A plain click selects the
  element instead, and the line under the picture then shows it with an **Open in model** button,
  which does the same as the Ctrl+click. While an element is selected, that line is the only one
  that names it: the status line below it, which names what is lit, stays empty, and what the
  pointer rests on is in its tooltip. A click on empty space lets the selection go, and the status
  line names what the pointer rests on again. A member that is itself a composition keeps its plain click
  for its own page, so its file opens with the Ctrl+click, or from the frame of its own page. An
  overview's count square and its line open no file: they stand for many connectors.
- **From the model to the diagram.** Above the name of every element a diagram draws, the editor
  shows a `diagram` link, and **SWCraft: Show in Diagram** (`swcraft.showInDiagram`, in the editor's
  context menu and the Command Palette) asks the same for the element at the cursor — a port or a
  runnable when the cursor is inside one. The diagram opens with the element **selected and centred**,
  exactly as if you had clicked it; the search box is left for your own searches. An element drawn
  on several diagrams asks which one, unless the tab already shows one of them; one drawn twice on
  the same diagram (a component two tasks run) says *1 of 2 on this diagram* with a **Next** button.
  A composition's and a sequence's `diagram` link opens that composition's or sequence's own page.
- **The links stay in the tab**: the header's links, the click-through into a member composition,
  the way up and the index. **← Back** and **Forward →** in the header walk what you opened; no key is
  bound to them. A page's SVG file opens in VS Code's image preview and `report.txt` in an editor.
- **The tab follows VS Code's colour theme**: in a light or a dark theme the drawing takes LikeC4's
  published palette — blue components with light labels, compositions as a light blue tint with a
  solid blue border on every view, grey lines, on a white or a near-black canvas — and in a
  high-contrast theme the theme's own outlines. A port is drawn in its line's grey, filled with the
  tint of the composition around it; port names inside a composition box are grey, inside a
  component box light. The selection and what you point at are outlined in amber, the
  current search match in red and the other matches in light red; a port you point at, pin or select
  turns amber too, its outline thicker than an unselected port's and thinner than its lit line, at any
  zoom. A high-contrast theme keeps its own marks. Only the
  tab is coloured: the SVG files and the pages opened in a browser stay white.

The tab says when its page cannot be trusted, and never regenerates behind your back. These are all
the lines it shows:

- *The model changed after these diagrams were generated.* — the line at the top of the page, with a
  **Generate** button that runs the press; it goes when the views are current again.
- *These diagram pages were generated by another SWCraft release — press Generate* — a page from
  another release is not shown at all until it is generated again. After an update of the extension
  without a reload of the window, the tab checks once more against the updated extension before it
  says so.
- *The SWCraft toolchain this window runs is another release than this extension (…)* — **Generate**
  wrote the pages again and they are still another release's: the `swcraft` the window runs (the
  `swcraft.executable` setting, else the one on your `PATH`) is not the release of the extension, so
  pressing **Generate** again would not help. Make the two the same release, then press **Generate**.
- *This diagram page is not one SWCraft generated — press Generate* — the file was changed by hand
  or written by another tool; it is not shown, and **Generate** writes it again.
- *This diagram page is no longer in the diagrams' directory — press Generate* — the page was
  removed or moved since it was listed.
- *This SWCraft extension cannot read its diagram-page-script.json — reinstall it, then press
  Generate* — the extension's installation is damaged.
- *`<name>` is declared in more than one file — Check Model names them* — a Ctrl+click on an element
  whose name two files declare opens nothing; **Check Model** names both files.
- *`<name>` is not on this diagram as last generated*, or *… is not on any diagram as last
  generated* — the element was added after the last generation; *not on any diagram* offers
  **Generate Diagrams**.
- *`<name>` cannot be shown: no diagrams list the model's elements yet* — **Show in Diagram** found
  no diagrams, or diagrams an earlier release generated, which do not list the elements; it offers
  **Generate Diagrams**.

**What the tab does not reach.** It shows what the last generation drew, so an element added since is
not on it until a press. Layers no diagram draws — interfaces, data types, requirements — have no
`diagram` link; an interface is reached on the picture through a port. A member deeper than a port
or a runnable (a data element of a port, a runnable's access) opens at its port's or runnable's
line. A window reload closes the tab; the next press opens it again. A model file opened through a
link from outside the model folder may not be recognised and answers *not on any diagram*. In a
remote window (SSH, WSL, a container) and on Windows the tab has not been tried by hand.

## What was not drawn { #what-was-not-drawn }

<!-- purpose: Finding what a view could not draw, and why, in the diagrams report -->

Nothing the model declares is left out in silence. `report.txt` lists every view with its size and
then, counted and grouped by reason, everything a view could not place — a connector naming a
member the composition does not have, a port its type does not declare, a step naming a runnable
that is not declared, a task in a partition that is not declared, a partition on a core that is not
declared, a component no task runs — and, in a
section of its own, every component connected to itself, drawn as a loop. The report carries no
timings, so two runs over the same model produce the same report.

On a model `swcraft check-model` accepts, the only entries you will see are the loops, the
components no task runs — a component that is not scheduled yet is legal — and a sequence message
whose receiving component stands on two lifelines, which names both; anything else in the list is
a finding `check-model` also reports.

## What a diagram will not tell you { #what-a-diagram-will-not-tell-you }

<!-- purpose: What a diagram does not tell you: validity, the ARXML, and anything it does not draw -->

- **Whether the model is valid.** A view that draws cleanly can come from a model
  `swcraft validate` refuses, and `validate` is the gate. [The checks](reference/rules.md) is what
  it runs.
- **Anything about the ARXML.** No generator reads the diagrams, and the diagrams read the model,
  not the generated ARXML, so a view is not evidence about what was generated for the toolchain.
  The pages carry no qualification claim.
- **Whether a lonely box is a mistake.** A component with no connector draws as a box whose ports
  hang free, and generates perfectly happily; SWCraft has no check that says a component must be
  connected to something, because plenty of real ones are not yet, and none that says it must
  satisfy a requirement either: an unconnected box is no finding at all. The check it does have is
  about the requirements: a requirement **that no element satisfies** is a warning. So read an
  unconnected box as a question to ask, never as a failure the tool already reported.
