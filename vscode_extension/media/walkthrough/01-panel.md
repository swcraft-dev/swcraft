# Where the panel is

SWCraft lives in its own **Activity Bar** container — the vertical strip of icons down the far
edge of the window, the one that holds Explorer, Search and Source Control. The SWCraft icon
joins that strip when the extension is installed.

**If you cannot see it, you are not doing it wrong.** The strip only shows as many icons as it
has room for; the rest move into the `…` overflow at the bottom of it (*Additional Views*).
Two reliable ways in that do not depend on hunting for an icon:

- open the Command Palette (`Cmd+Shift+P` on macOS, `Ctrl+Shift+P` elsewhere) and run
  **View: Show SWCraft**;
- drag the icon up the Activity Bar once you have found it — VS Code remembers the position per
  profile, so it stays where you put it.

## What the view shows

The first line is the project itself, so you always know what the commands below it will act on:

| Line | What it means |
|---|---|
| `SWCraft project detected` + the path | a `swcraft.yaml` was found at the workspace root |
| `No SWCraft project detected` — *Run Onboard Project* | a folder is open, but there is no manifest at its root |
| `No workspace open` — *Open a folder to get started* | nothing is open yet |

Under it are **three sections**. Model and Experiments are always expanded; Setup is expanded
until the project is set up, and then folds away (see below):

- **Model** — Build, Check Model, Diagrams, Test Tables, Change Report, Resource Report (its
  answer opens in the **Resources** view below the panel), Architecture Metrics, Export Component,
  Import Component, Import Requirements.
- **Experiments** — Allocation (CARMA), which runs the allocation experiment on the model and
  opens its report in the Markdown preview, and Trace Links, whose **Labelling** and **Review**
  each take the documents you choose; the **Trace Links** view below the panel then shows the links
  proposed between their requirements and the model's elements, where you tick, reject and apply them.
- **Setup** — Install Toolchain, Update Toolchain, Onboard Project.

What depends on an element is asked from the element's own file: right-click in it and choose
**SWCraft: Impact of This Element**, and the answer opens in the **Impact** view below the panel.

`Build` is at the top because it ends at the gate: it runs what `moon run :build` runs — clean the
outputs, check the model, generate the ARXML, the diagrams and the requirements — and finishes
with `swcraft validate`. `Check Model` sits directly under it because it asks the model half of the
same question without writing anything, and it is the cheaper one to run while you edit.
`Diagrams` generates the diagrams and shows them in a tab of the editor. `Test Tables` generates the
tables the test team verifies against and opens them in a read-only tab; its first press asks for
the test team's annotations folder, or *none yet*. `Export Component`,
`Import Component` and `Import Requirements` are last in the Model section because they are the
model commands that ask a question before they run (which component; which file).

The Setup section **begins with the toolchain**, because that is the order of doing things.
`Install Toolchain` asks for the workstation archive you were given and runs the archive's own
installer, which brings the Python it needs with it; it is what every health finding sends you to, and it is what must
exist before `Onboard Project` has an executable to run at all. `Update Toolchain` sits directly
under it — the same action with one comparison in front of it, so a newer archive installs and an
older one is refused by name. Both work with no project open.

Once the toolchain is installed and the open project is onboarded, Setup **collapses by itself**:
there is nothing left in it to do. It is still there — click it to open it again. If one of those
things goes missing later (the toolchain, the `swcraft.yaml`, or the agent profile Onboard Project
copies in), Setup opens again the next time the panel refreshes, because a setup step is then the
next thing to do.

## Help, in the `…` at the top of the SWCraft view

Two commands are not part of the tree, because they are help rather than steps: **Open Guide** and
**Check Editor Tooling**. They are in the `…` menu at the top of the SWCraft view, where VS Code
keeps a view's help and diagnostics. VS Code shows a view's title actions, that `…` among them,
when the pointer is over the view or the view has focus.

- **Open Guide** serves the user guide that came with your install and shows it inside VS Code,
  in the Simple Browser. It works with no project open.
- **Check Editor Tooling** is what you reach for when something is already wrong: it reports which
  of the extensions SWCraft is packaged with this window actually has, where the installed
  `swcraft` is and what version it reports, and whether the bundled schemas shipped — and it says
  in the report itself what it could not check.

Nothing in the panel configures the MCP server. The extension registers it with VS Code itself,
for every open folder that has a `swcraft.yaml` at its root, so there is no file to open and none
to repair.

## What happens when you click one

Most items spawn the installed `swcraft` and print its output verbatim into the **SWCraft** output
channel. Nothing is re-rendered, except the model check of **Check Model** and **Build**: its
findings go to the **Problems** panel, and the channel ends with one summary line. Every **Check
Model** run that starts ends by taking you to **Problems** itself, and the status bar at the bottom of the
window counts the findings — `SWCraft: 0 errors · 35 warnings`; click that count to open Problems
again. **Build** runs its six commands one after another into that same channel, and the first one
that fails ends the run. **Export Component** opens the ARXML file it exported in an editor tab.
**Diagrams** runs nothing afterwards; a save only marks the diagrams out of date. **Resource
Report** fills the **Resources** view — each core and memory with its use, limit, headroom and
status, its consumers a click away from their files — opens it, opens the report in a tab and
leaves only the log in the channel; a later change to the model marks the view and the item out of
date; **Refresh**, the item or a green **Build** runs it again. **Refresh** updates the view only,
and a green Build fills the view without opening it or a report tab. **Architecture Metrics**
compares the working tree with `HEAD`, or with the revisions the `swcraft.metricsSince` setting
names, and opens its report in the Markdown preview; it is no gate and writes nothing into the
model. The two
toolchain items spawn something else again — the archive's own installer, whose
output is streamed into the channel as it goes, because the run is minutes long. Either way the
extension never reads or writes the architecture model itself — `swcraft` is the only thing that
touches it. **Import Requirements** opens its dry run's report first, in a tab shaped like the
change-set review tab, with *Write* and *Cancel* under it, and imports only after you click
*Write*; the import's report replaces the dry run's in the same tab. **Import Component** shows
two reports in its tab the same way, the dry run's with *Write* and *Cancel*, then the import's
with *Check Model*; when the project already has the component and the file changes it, the tab
offers *Show Update* instead, whose report shows what an update would replace and keep, and
*Update* applies it.

After a run the tree item shows what happened and when — `built 12:39`, `checked 12:39`,
`exported 12:41`, `imported 12:41`, `failed 12:41`. A status clears itself after about thirty seconds; a failure
keeps its message in the item's tooltip until then, and the full text stays in the output
channel — except **Resource Report**'s, whose report is in the **Resources** view while the channel
keeps only its log.

Every one of these commands, and the two in the `…` menu, is also in the Command Palette,
prefixed `SWCraft:` — all but the Resources view's **Refresh**, which runs the same report as
**Resource Report**, the one the Palette holds.
