# SWCraft for VS Code

SWCraft turns a YAML architecture model into AUTOSAR ARXML, UML/SysML diagrams and Sphinx-Needs
requirements. This extension is its companion in the editor: it runs the installed `swcraft`
toolchain from the Command Palette and from a panel of its own, registers SWCraft's MCP server so
Copilot's agent mode can read and change the model through it, ships a JSON Schema per model layer
so the YAML files complete and validate as you type, follows a model file's references (Go to
Definition, a hover card, Find All References), outlines a model file, finds any element by name
(Go to Symbol in Workspace), shows a references / impact / diagram / guide line above an element, links
element names in descriptions and import reports, lists the model check's findings in the Problems
panel with two quick fixes for requirement links, and opens the user guide that came with your
install.

**The extension never edits the architecture model itself.** It writes no model file: a change
goes through the `swcraft` CLI, through the MCP tools — which is what keeps the audit trail and the
review step honest — or through your own editing. A quick fix on a model-check finding is that:
an edit of the open file, made only when you pick it, that you apply and save.

## What you need before you start

- **The SWCraft toolchain.** One installation per machine, from the workstation archive you were
  handed — either by running its wrapper (`swcraft_install.cmd` or `swcraft_install.sh`) in a
  terminal, or from here with **SWCraft: Install Toolchain**, which asks for the archive (the zip, or
  the directory it was unpacked into) and runs the archive's own wrapper with its progress, one
  line per step, in the SWCraft output channel; the output of the programs it starts is in
  `install.log` in the installation directory, whose path the run prints when a step fails. Nothing needs to be on the machine first: the archive carries its own
  Python, which the wrapper checks and runs the installer on. To open a zip the command uses
  Windows' own `tar`, and `unzip` from `PATH` elsewhere — a Linux machine without `unzip` is
  refused in one sentence, and picking the unpacked directory instead needs no program. The
  extension embeds neither the installer nor a wheel, so what it installs is exactly what a
  terminal would. Every command here spawns that executable; nothing is vendored.
  On Windows the installer edits your user `PATH`; on macOS and Linux it prints one `export PATH=`
  line for you to put in a shell profile. The extension itself does not need `PATH` — it spawns an
  absolute path — but your terminal does.
- **A recent enough VS Code.** The floor is declared in this extension's own manifest
  (`engines.vscode`) rather than restated here, and it is at or above the floor of every pack
  member below. Below it the Extensions view refuses the `.vsix` as incompatible — update the
  editor and install it again.
- **No Node.js of your own, when SWCraft came from the workstation archive.** `swcraft generate
  diagrams` — what **SWCraft: Diagrams** and **SWCraft: Build** run — lays the diagrams out on
  Node.js, and it uses the one the archive's installer places beside `swcraft` before it looks on
  `PATH`. A toolchain installed some other way needs `node` on `PATH`; without one, the diagrams
  step refuses in one sentence naming what it needs, and every other command still works.

## What one install brings

SWCraft ships as an **extension pack**. On a machine that can reach a gallery, installing it also
installs two third-party extensions — and the workstation archive carries the same two as
`.vsix` files, which its installer hands to VS Code before SWCraft's own, so a machine installed
from the archive gets them with no gallery at all:

| Member | What it gives | What its absence costs |
|---|---|---|
| `redhat.vscode-yaml` | reads the JSON Schemas SWCraft ships, so model files get completion, hover and per-file diagnostics | the schemas become inert — there is no other reader. Model files still open as YAML with highlighting; completion, pre-filled defaults, hover descriptions and every per-file diagnostic stop |
| `redhat.vscode-xml` | outline, breadcrumbs, symbol navigation, formatting and tag auto-close on `.arxml` | the lesser of the two: VS Code's built-in XML support is grammar-only, so highlighting, bracket matching and folding survive. `swcraft validate` is the authoritative ARXML check either way, and it runs outside the editor |

**Neither is a dependency.** Every SWCraft command works with neither of them installed —
the extension spawns the CLI and never calls either one's API — which is why they are a
recommended bundle you can remove individually rather than something VS Code refuses to start
without. On a machine that cannot reach a gallery, a `.vsix` of SWCraft installed on its own
succeeds with SWCraft alone and says nothing about it, so ask your own machine what arrived:
**SWCraft: Check Editor Tooling** names every member as present, absent or outside its floor, with
the exact id to install. The copies the archive carries are a floor, not a pin: a VS Code that can
reach the Marketplace updates them like any other extension and keeps the newer one. A member the
archive's installer could not install is one sentence naming it, and the run carries on.

## The commands

All of them are `SWCraft: …` in the Command Palette (**Impact of This Element** and **Show in
Diagram** only while an element file of the model is the active editor). In the SWCraft view, all
but four are items of the tree; **Check Editor Tooling** and **Open Guide** are entries in the `…`
at the top of the view instead (see *The SWCraft view*), and **Impact of This Element** (see *The
Impact view*) and **Show in Diagram** are asked from an element's file. The Model ones come first, Setup next, the two help commands
last.

- **SWCraft: Build** — runs what `moon run :build` runs, with no moon: `swcraft clean-outputs`,
  `check-model`, `generate arxml`, `generate diagrams`, `generate needs` and `validate`, in that order, into
  one output, and the first step that fails ends the run. Its last step, `swcraft validate`, is the
  gate; it runs with `--report`, so it also writes `build-report.md` in the manifest's `dist_root` (beside the ARXML in a
  scaffolded project) — the verdict
  and which check gave it, what was built, the warnings, the cores and memories, and the ARXML
  elements changed against the previous `_old` ARXML. When that step ran, passed or failed, the
  report opens in the Markdown preview, as Change Report's does. Nothing opens, with one line in the
  output channel saying so, when the step says the report was not written or that its path holds a
  control character, when the path it prints lies outside the workspace folder or the file is no
  longer on disk, or when the Markdown preview itself cannot open. When the model check stopped the
  run with an error, no report opens and the **Problems** panel takes the focus, as Check Model
  does. Warnings alone move no focus. The report changes no exit code and is no qualification
  evidence.
- **SWCraft: Check Model** (0.5.1) — the same model checks `swcraft validate` runs, with nothing
  written and no ARXML generated, so it also compiles no AUTOSAR XSD. That compilation is what
  `validate` spends its time on when the machine has a licensed schema — five to six seconds on one
  measured Windows workstation, against about half a second for the model checks and ARXML
  generation together — which is why the fast check is its own command. It is an edit-loop
  convenience and **not** a gate: `swcraft validate` remains the only acceptance oracle. Its
  findings — and those of Build's model-check step — also appear in the **Problems** panel, errors
  red and warnings yellow, each at the line it names (see *The model check's findings* below). Every
  Check Model run that starts, successful or not, opens the **Problems** panel itself, and a
  failure's notification points there too; a status-bar item counts SWCraft's findings from the
  last model check (`SWCraft: 0 errors · 35 warnings`, a run that could not check the model at all
  being its one error); clicking it opens Problems again. The count is as stale as the entries it
  counts, and `0 errors` is not a passed `swcraft validate`.
- **SWCraft: Diagrams** — runs `swcraft generate diagrams`, which writes the model's diagrams into
  `views_root` as files — an internal block diagram and an overview per composition, the nesting of
  the compositions, the deployment of the tasks on the cores (when the model declares a task), a
  sequence diagram per scenario, one page per view, an index and a report of
  anything a view could not draw — and shows the index in one tab of the editor, reused by every
  press. The pages pan, zoom and search by themselves; nothing is served and nothing listens on a
  port. In the tab, Ctrl+click (Cmd+click on macOS) on a drawn element, or *Open in model* under
  the picture, opens its YAML file beside the diagram at its line; the links between the pages stay
  in the tab, with *Back* and *Forward*; and the tab follows VS Code's colour theme while the files
  stay white. A saved model change regenerates nothing: the item turns *out of date* (a yellow dot,
  and a count on the SWCraft icon) and the tab shows a line with *Generate*, until you press it
  again; the tab then shows the new drawing. To look at a page in a browser, open `index.html` from
  the file; to share a picture, take its SVG file. A model the generator refuses shows its refusal
  in the SWCraft output channel and opens nothing. It
  refuses in one sentence, running nothing, when `views_root` is absolute, is the project directory
  itself or resolves outside it.
- **SWCraft: Show in Diagram** — from a model file in the editor (its context menu, or the
  `diagram` line above an element's name): opens the diagram that draws the element at the cursor
  in the diagrams' tab, with the element selected and centred as if you had clicked it; an element
  on several diagrams asks which one. It reads the diagrams as last generated and generates
  nothing; an element they do not draw says so and offers *Generate Diagrams*, and so does a project
  whose diagrams list no element yet (none generated, or generated by an earlier release). Like
  Impact of This Element, it is not in the panel, because it needs an element and a cursor.
- **SWCraft: Test Tables** — runs `swcraft generate test-tables`, which writes the tables the test
  team verifies against — component placement and runnable scheduling, five files under
  `dist_root/test-tables/` — and opens them in a read-only tab: one sub-tab per file, each row
  coloured by its `review_state`, nothing editable. The first press asks for the test team's
  annotations folder (a folder, or *none yet*), remembered in `swcraft.testTables.annotations`;
  **Change…** in the tab asks again. A refused run shows one line in the tab and the command's own
  lines in the SWCraft output channel. A saved model change does not mark the tables out of date:
  press it again. The tab is a view of the files, no evidence of a test.
- **SWCraft: Change Report** — one click, no question: `swcraft change-report` with no revision
  arguments, i.e. `HEAD` against the working tree, writes a Markdown report of what the change does
  to the model and opens it in the Markdown preview. The report writes nothing into the model. A run
  that printed no report path, a report gone from disk before it could open, or a preview that
  cannot open is one line in the SWCraft output channel. `swcraft diff` stays a terminal command.
- **SWCraft: Impact of This Element** — `swcraft impact --format json`: what depends on an element,
  directly or not, drawn in the **Impact** view (see *The Impact view* below). It is asked from the
  element's file — the editor's context menu or the Command Palette, neither offered outside the
  model's layer files — about the element the line under the cursor names, else the file's own; the
  *impact* lens above an element's `name:` line and a dependent's row in the view ask it too. The
  output channel keeps the log only, never the answer.
- **SWCraft: Resource Report** — `swcraft resource-report --format json --report`: fills the
  **Resources** view (see *The Resources view* below), opens it and opens the report in a tab, the
  Markdown preview of `resource-report/resource-report.md` in the manifest's `dist_root`: each
  core's and memory's use, limit, headroom and status, its consumers and its findings. The output channel keeps the log only —
  the command line, the command's error stream and one closing line — never the report. That
  command exits 0 even when something is over budget, and the extension deliberately does not
  invent a warning the CLI did not give: a red row is the report's status, not a failed check.
- **SWCraft: Architecture Metrics** — one click, no question: `swcraft metrics --report`, against
  the revisions in `swcraft.metricsSince` (`--since-head` when it is empty). It writes
  `metrics/metrics.md` under the manifest's `dist_root` and opens it in the Markdown preview: what
  state the architecture is in and where it is heading. If `HEAD` cannot be read, the report says why
  and the run still succeeds; a revision you named that cannot be read fails the run. It writes
  nothing into the model and is no gate.
- **SWCraft: Export Component** — offers the project's components and writes a component-scoped,
  self-contained ARXML export of the one you pick: the component itself, the interfaces its ports
  reference and their transitive data types — no composition, no connectors, no other components.
  The output path defaults to `<dist_root>/components/<name>.arxml` (`<name>~1.arxml` when
  another component's name differs from it only in case). The exported file opens in an editor
  tab; when it cannot be opened, the SWCraft output channel says why and a notification names the
  path when the export printed one. *Import Component*, next, brings such an export back in.
- **SWCraft: Import Component** — `swcraft import component`: brings one component's ARXML file
  into the model. It asks for an `.arxml` file, always runs the import with `--dry-run` first and
  opens its report in the tab *Component Import Report*, in the same form as the requirements
  import's tab below: the report, rendered Markdown headed by the file's name, with *Write* and
  *Cancel* under it. Only *Write* runs the same command without `--dry-run`; its report replaces the
  dry run's in the same tab, which then offers *Check Model*, run only on a click. *Cancel* closes the
  tab and writes nothing. When the project already has the component and the file changes it (the
  command's exit status 3), the tab offers *Show Update* and *Cancel*: *Show Update* runs the dry run
  with `--update` and shows what the update would replace and keep, with *Update* and *Cancel* under
  it, and only *Update* applies it. Any other refused dry run ends with *Nothing was written.* and no
  button. The extension never passes `--replace-access-lists`, so an update keeps the project's
  read, write and call lists; it reads no model file and decides nothing: the CLI writes the files.
- **SWCraft: Import Requirements** — `swcraft import requirements`: brings requirements from the
  requirements team's `needs.json` into the model. It asks for the file, then for the mapping — the
  one used last (*last used*), *Choose another mapping file…* or *SWCraft's own names (no mapping)*,
  the choice remembered at once in `swcraft.requirementsImport.mapping` — then for the delivery's
  version, in a box filled with the file's own `current_version` (Enter keeps it and passes no
  `--baseline`; a typed value is passed as `--baseline`; with no version in the file the box is empty
  and an answer is required). Both runs carry the same `--mapping` and `--baseline`. A delivery is
  the complete set: a requirement the file no longer holds is in the report's *Removed* list. It
  always runs the import with `--dry-run` first and opens its report in the tab *Requirements Import Report*, never saved, in the
  form of the change-set review tab: the report on top, *Write* and *Cancel* under it, with the dry
  run's count in the report's summary line. The report is the Markdown file the
  command writes (`--report` with a `.md` path in a temporary folder outside the project, deleted
  once read), rendered by VS Code's built-in Markdown support (plain text, with a line saying so,
  when that is turned off).
  One click decides. Only *Write* runs the
  same command without `--dry-run`; *Cancel* closes the tab, as *Reject* does in the review tab, and
  applies nothing, and so does closing the tab. The buttons are disabled while the import runs. The
  import's report replaces the dry run's in the same tab, the two buttons gone, and the tab offers
  *Check Model* beside the import's count, run only on a click. A command line `Warning:` line stands in a yellow block above
  the report. A refused dry run is shown in the report, ending with *Nothing was written.*, with no
  buttons; a refused or failed applied run is shown there too and offers nothing; either way the
  item reads failed and a warning names it. The extension reads no model file for this and decides nothing: it
  runs the CLI, which writes the requirement files.
- **SWCraft: Allocation Experiment (CARMA)** — `swcraft experiment allocation`: which components
  could move to another partition, and what each move would buy. It runs with the configuration file
  the setting `swcraft.experiments.allocation.config` names (see *Settings*), or with the
  experiment's defaults when the setting is empty; the run shows in the SWCraft output channel, and
  on success the report opens in the Markdown preview. A refusal stays in the channel and opens
  nothing.
- **SWCraft: Install Toolchain** — see *What you need before you start*.
- **SWCraft: Update Toolchain** — the same run with one comparison in front of it: an archive
  **older** than the installed toolchain is refused, naming both versions, because going back a
  release deliberately is a terminal action and not a button. An installed version that cannot be
  read is not a reason to refuse, since that is the state this command repairs. A run that changed
  the version offers *Reload Window* — unless the archive's `.vsix` was not installed, which
  happens when the `code` command line is not on the path VS Code was launched with; the run still
  succeeds and tells you to install the `.vsix` from the archive by hand instead.
- **SWCraft: Onboard Project** — see *Onboarding a project* below.
- **SWCraft: Check Editor Tooling** — the health surface. It reports every pack member as present,
  absent or outside its floor; the installed toolchain, in six ordered findings (it is absent; it is
  there but will not start; it runs, exits 0 and prints no version this can read; it reports a
  version different from this extension's; a bare `swcraft` resolves **nowhere** on this machine —
  the one every healthy macOS and Linux install produces until the printed `export PATH=` line is in
  a shell profile; and a bare `swcraft` resolves **somewhere else**, another installation ahead of
  this one); the bundled schema inventory; and, in a window whose project holds a `swcraft.yaml`,
  the **Copilot agent profile** — *missing* (counted as a problem), *differs from this release's*
  (a warning, not counted, since you may have edited it on purpose; **Onboard Project** replaces it)
  or this release's; and the **Copilot model guard**, `.github/hooks/swcraft.json` — *missing* or
  *differs from this release's* (each a warning, not counted; **Onboard Project** writes it) or this
  release's, and a warning in every state when a bare `swcraft` resolves nowhere or is not this
  extension's version, since the guard runs that `swcraft`. When it resolves nowhere, or is a
  release from before the guard existed, the warning says Copilot can call no tool in the project,
  SWCraft's included, until this release's `swcraft` is on the path; it says so conditionally when
  the version cannot be read or the hook file differs from this release's, not at all when there
  is no hook file (then nothing runs), and for another release that has the guard it says that
  release's guard is the one deciding (if your hook file differs, that it is if the hook still runs
  it; with no hook file, only that no hook runs it). It writes the report to the SWCraft output channel
  and raises a notification carrying the problem count. It cannot tell "not installed" from
  "installed but disabled" — VS Code lists enabled extensions only — so the two arrive as one
  *absent* verdict naming both. The report ends by saying that a healthy pack is not validation
  evidence, and it prints what it cannot see.
- **SWCraft: Open Guide** — serves the user guide your install carries and shows it inside VS Code,
  in an editor tab beside your model (its toolbar can move it to your browser; when VS Code cannot
  show it in a tab, it opens in your default browser). It needs no project and no network.

The number of commands is deliberately not written here: it is derived from the manifest and
checked against the registration and the panel, so a new one cannot leave a stale count behind.

## The SWCraft view

The Activity Bar gets a SWCraft icon with a view of its own, so every command is one click away
rather than a palette search. The tree shows the detected project (or "no SWCraft project
detected" / "no workspace open") and three sections: **Model** (Build, Check Model,
Diagrams, Test Tables, Change Report, Resource Report, Architecture Metrics, Export Component, Import
Component, Import Requirements — the three that ask a question last),
**Experiments** (Allocation (CARMA) and Trace Links — right after Model, because they ask a question
of the same model) and **Setup** (Install Toolchain, Update Toolchain, Onboard Project — the
toolchain first, because it is what must exist before onboarding has an executable to run). Model
and Experiments are always expanded. Setup is expanded until the toolchain is installed and the
open project is onboarded (its `swcraft.yaml` and the Copilot agent profile Onboard Project copies
in are there), then collapses by itself; click it to open it again, and it opens again by itself at
the next refresh after one of those goes missing. A `swcraft.executable` setting that holds a bare
command name, found on your `PATH`, does not count as an installed toolchain here, so Setup stays
expanded in that case.

The two help commands, **Open Guide** and **Check Editor Tooling**, are not in the tree: they are
in the `…` at the top of the SWCraft view, which VS Code shows when the pointer is over the view or
the view has focus.

Below the tree, the **Pending Change Sets** view lists every change set an assistant is waiting on
you to review, one row per question, with the count on the view's badge; a click opens or reveals
its review tab (*The MCP server, and the agent in Copilot Chat* below).

Each tree item shows that command's last run as a verb and a clock time — `built 12:39`,
`opened 12:39`, `exported 12:41`, `imported 12:42`, `failed 12:41` — cleared after thirty seconds; a failure keeps
its message as the tooltip. The full output is in the SWCraft output channel either way, except
**Resource Report**'s: its report is in the Resources view and its tab, and the channel keeps only its log.
**Impact of This Element** is no tree item; its answer is drawn in the Impact view, and the channel
keeps only its log too.

## The Resources view

**Resources** sits below the SWCraft view and the Trace Links view, collapsed until it is used;
before the first run it holds a sentence and a **Resource Report** button. **Resource Report** fills
it on request, with or without a Build, opens it and opens the report in a tab. The `↻` in its
title bar updates the view only, and a green **Build** fills it as well, without opening it and
without a report tab. The view is for finding your way; the tab is the report to read.
After a run it shows the sections
*Cores*, *Memories* and, when the report names an unresolved reference between the software and
the hardware, *Topology*. A core or memory row reads `<used> of <limit> · headroom <headroom>`,
with a green tick within budget, a red cross over budget and a yellow warning
when a `strict` resource has a consumer that declares no budget. Expanded, it lists its consumers
largest first — the ones that declare `0` folded into one row — and its findings.

A click opens the row's file as a preview tab, the focus kept in the view: a core or a memory at
its `threshold` line (else `size`), a task at its `wcet`, a component at its `memory:` entry for
that memory — the first line when the key is not there — and a finding at the element it names.

The report tab shows `resource-report/resource-report.md`, which a press writes in the manifest's
`dist_root`. A core's table names each task's partition, and a second table gives each partition's
share of the core; a partition has no budget of its own, so a share carries no verdict. A memory's
budget and headroom read in whole bytes, rounded down, in this tab, the build report, the Change
Report and the view; the exact figures stay in the terminal's text output, the assistant's answers
and every finding or verdict sentence, and the decision stays on the exact figure. An open tab does
not follow **Refresh** or a green **Build**, and a Build removes its file; only a press rewrites
it. A project pinned to another SWCraft release fills the view and gets no tab, and the output
channel says why.

**Resource Report** and **Refresh** run the report whenever you press them. A
press during a report's own run is refused with *a run is in progress; try again when it ends*; a
press during the refresh after a green **Build** waits for it: **Refresh** then opens the view, and
**Resource Report** runs the report once more for its tab.

Only a green **Build** re-runs the report by itself, once, after it has finished: a failed Build
and Check Model run none, a Build in a terminal or CI refreshes nothing, and the run is skipped
without a word while a report or a toolchain install is in progress,
or after the folder the Build ran in stopped being the window's first. **Install Toolchain** and
**Update Toolchain** refuse while that run goes, as they do during the Build itself. The status bar
shows the run, and the **Resource Report** item then reads `reported` with the time, like any other
run, with no yellow dot. A
change under the model folder or to `swcraft.yaml` that VS Code's file
watcher reports — a save, a checkout or a terminal edit while the window is open — marks the view
`out of date — Refresh`, and marks the **Resource Report** item with the same yellow dot and count
on the SWCraft icon as an out-of-date ARXML. The view keeps its report in this window's memory only,
so a reload shows the button again, even after a green Build. A change saved while a run is still going marks the report that
run shows. A change is dated by the file's modification time, so a file copied in with its old time
kept (`cp -p`, `rsync -t`, an unpacked archive) leaves the report marked current, as it does the
ARXML and the diagrams. A failed run keeps the last report and says `the last run failed — see the SWCraft output
channel`.

## The Impact view

**Impact** sits below the Resources view, collapsed until it is used; before the first question it
holds one sentence and no button. **SWCraft: Impact of This Element** — the editor's context menu
or the Command Palette in an element file, or the button in the view's title bar — asks about the
element the line under the cursor names (a reference: what Go to Definition opens from that line;
in a runnable's `reads`, `writes` or `calls`, the element holding the member), else the file's own
element. The answer is the whole walk of `swcraft impact`: a root for the element asked, with its
layer and its number of dependents; one node per layer that holds a dependent, in the model's layer
order, with its count, collapsed above twenty; and each dependent with how the walk reached it
(`via port rIn`). A name two layers declare gives two roots.

A click opens the dependent's file at its `name:` line (`id:` in a requirement; under a
requirement, a direct dependent at the line naming it) as a preview tab, the focus kept in the view. The button on a dependent's row
asks about it, and a new question replaces the tree. A name written where the model declares no
reference — in a `description`, a connector end's `port:`, a task entry's `runnable:` — asks about
the file's own element, and a port or a data element is never asked about itself, only the element
holding it. The command line reads the saved files: an unsaved rename of the element is answered
with *save first*.

The view's description reads `out of date — Refresh` after a change, under the same rules and with
the same limits as the Resources view's, and `the last run failed — see the SWCraft output channel`
after a failed run, the last tree kept. No panel item and no count on the SWCraft icon follow it.

## The walkthrough — "The first hour with SWCraft"

Reachable from Help → Welcome and from the *Welcome: Open Walkthrough* command. Seven steps, each
one a page you read and a thing you do, in the order a new project needs them: find the panel;
onboard a project; edit a layer file; build, which ends with the gate; see the model as diagrams;
talk to the model through the agent; open the guide. Step six is the one that needs more than the
panel: a signed-in Copilot account for the agent.

## Onboarding a project

**SWCraft: Onboard Project** prepares a standalone SWCraft project folder in one step. It asks for
a target folder — independent of whatever workspace is open, and it works with no folder open at
all — runs `swcraft init` if that folder has no `swcraft.yaml` yet (skipped entirely for a folder
that already has one, such as a model project you cloned), and copies in four files from its own
bundled copies — the Copilot agent profile and instructions, the Copilot model guard
(`.github/hooks/swcraft.json`) and the Codex model guard (`.codex/hooks.json`), the model
project's own files, the ones `swcraft init --integration moon` writes. It is re-runnable, and
re-running it is how a project picks up the current copies of the first three: they are
overwritten outright. Codex's hook file is written only where there is none or where it holds
SWCraft's guard and nothing else, an older version of it included: Codex reads one
`.codex/hooks.json` per project, so one holding anything more, or that does not parse, is yours,
and onboarding keeps it and says so in one line, in the output and in its closing message — add
the SWCraft guard's entry (`swcraft guard --host codex`) to it by hand. Codex runs a project's hook only in a
git repository, so for a folder that is none onboarding adds one more line, in the same two
places: `git init` makes it one.

Since 0.23.0 it first asks which chat clients the project is for — GitHub Copilot, Codex, Claude
Code and Kiro — and writes the chosen clients' files only, with Copilot ticked to begin with in VS
Code and Kiro ticked inside Kiro. Kiro's three are `.kiro/settings/mcp.json` (SWCraft's server line,
`SWCRAFT_REVIEW=panel`), `.kiro/steering/swcraft.md` (SWCraft's instructions, generated) and
`.kiro/hooks/swcraft-guard.json` (the model guard, `swcraft guard --host kiro`), each written where
the project has none and replaced where it is still SWCraft's own; one of your own is kept and named
with the guide's *In Kiro* page. Kiro's chat does not see the server this extension registers, so
these files are how it reaches SWCraft; Kiro loads them once the folder is trusted and the window
reloaded.

A project that has no agent profile — one you imported, or one set up before the extension carried
it — gets a notice when the window opens. **Add the SWCraft Architect agent** writes the profile and
writes `.github/copilot-instructions.md`, `.github/hooks/swcraft.json` and `.codex/hooks.json` only
where the project has none; it never overwrites a file and never runs `swcraft init`. A project that already has a profile
gets no notice, so one set up before the guard existed takes it from **Onboard Project**. **Not for this workspace** is remembered for this workspace; closing
the notice without a choice asks again the next time.

It writes no MCP configuration — this extension registers the server itself, so there is nothing to
generate, merge or repair. A project upgrading from 0.6.x can delete its own `.vscode/mcp.json`.

Before it writes anything it checks that the installed `swcraft` actually runs. If it does not, you
get a "SWCraft CLI not found" error naming the path it tried and pointing at **SWCraft: Install
Toolchain**, and onboarding stops without touching the target folder.

## Editing the model: the bundled schemas

The extension ships one JSON Schema per model layer and one for `swcraft.yaml`, and associates them
with your model files by directory (`**/tasks/*.yaml`, `**/swc/**/*.yaml`, `swcraft.yaml` by
basename). A custom `model_root` needs no settings and no `$schema` line in the YAML. A third kind,
`requirements-mapping.schema.json`, is for the requirements import's mapping file and is attached by
name, to every file whose name ends in `needs-mapping.yaml`.

With `redhat.vscode-yaml` installed, a model file offers the declared field set and enum members on
completion with the defaults pre-filled, shows each field's description on hover, and underlines an
unknown key, a wrong scalar type, an invalid enum member or a missing required key as you type.

**What the schemas do not check, and why it matters.** A schema sees one file. It cannot see a
cross-file reference, a duplicate name, the AUTOSAR identifier rules, a resource budget or any of
the model rules that span elements — every one of those is refused by `swcraft check-model` and
`swcraft validate`, never by the editor. Treat the squiggles as typing assistance and the gate as
the answer. The one place the editor is *stricter* than the product is deliberate: it flags an
unknown key that the loader silently ignores.

## Following a reference

The schemas cannot see across files; the extension itself can follow a reference. On a field that
names another element — a port's `interface`, a data element's type, a connector end's component,
a task's partition, an element's `satisfies` and a requirement's `derived_from` (an entry leads to
the requirement it names, whatever version follows the `@`; an entry naming an element, such as
`hardware:CORE0`, leads to that element), a component's `memory` and `data_type_mappings`, a
memory's `accessible` cores, and every other reference the model declares — **Go to Definition**
(F12, or Ctrl/Cmd-click) opens the element's file at its `name:` line, and hovering the line shows
the element's kind, name, `description` and the first lines of its file. In a runnable's `reads`,
`writes` and `calls`, Go to Definition on `port:` opens that port in the same file, and on
`element:` or `operation:` the data element or operation inside the port's interface. On an
element's own `name:` line (`id:` in a requirement), **Find All References** (Shift+F12) lists the
elements that refer to it directly — on a requirement, the elements that satisfy it and the
requirements derived from it, each at the line of the entry that names it.

The **Outline** view, the breadcrumbs and Go to Symbol in Editor (Ctrl/Cmd+Shift+O) show the
element, its collections and their members as the file holds them. **Go to Symbol in Workspace**
(Ctrl/Cmd+T) lists every element of the model by the name of its file, with the folder it is
stored in beside it. Above an element's `name:` line a row reads `references` (click for Find All
References on the element — its own line included, as Shift+F12 lists it; the row shows no count,
so it appears at once and needs nothing saved first), `impact` (**Impact of This Element** on the element, its answer in the Impact view) and, for a composition or a sequence
whose diagram page exists, `diagram` (opens that page — straight away when the diagrams were
generated in this window and nothing in the model or `swcraft.yaml` has changed since, otherwise after regenerating
them; **SWCraft: Diagrams** always regenerates), and last `guide`, which opens the user guide's
reference page for the file's layer in the guide tab, at the section of the element's kind (the top
of the page when the file's `kind:` cannot be read). A name in a `description` — `interfaces/IBrakeTemperature`, or just
`IBrakeTemperature` when exactly one layer holds an element file of that exact name — and the
names in an import report written with `--report` become links to the element's file; a component
import report links its folder-qualified names only.

Each answer is computed when you ask, from the **saved** files; nothing is indexed. Find All
References asks `swcraft impact`, so it needs the toolchain installed. A line written in a form the
extension does not read (a flow value, a value continued over lines, an escape, an anchor or alias,
a key written twice) gets no navigation rather than a wrong jump. The guide's VS Code page,
*Following a reference*, has the details.

## The model check's findings

**SWCraft: Check Model** and **SWCraft: Build** put every finding of the model check into the
**Problems** panel — an error red, a warning yellow, `swcraft` as the source and the check's name
as the code — at the line the finding names: a stale `- REQ-12@3` under a port's `satisfies` is
marked on that line. A line the extension cannot find (a list written on one line, a file edited
since the run) falls back to the element's `name:` line, and a finding about no one element to the
first line of `swcraft.yaml` — as does a run that could not check the model at all and names no
model file, as one error whose message is the reason. A finding on a field's value underlines the
value, quotes included, and the key when the value is not on the key's line. A run refused because
a model file is malformed has one error per field on that file, each the refusal's own line
(`Invalid task YAML in …/Task_bad.yaml: period: Input should be a finite number`), at the field's
line where it can be found and the file's first line otherwise. A Check Model run refused because the toolchain pin does not match is
that one error at the `toolchain_version` line instead, and at the first line only when that line
cannot be found. **Build**'s model check places them the same way; its first step refuses on the pin, before its model check, so a pinned
Build ends at its notification with no Problems entry. The entries are the model check's own findings, placed in the editor,
and they describe the **saved** model at the last run: a save runs nothing, and the next Check
Model or Build replaces them.

The check's name in each entry is a link: a click opens the user guide inside VS Code on its
checks page, at that check's row, starting the guide first when it is not running. The guide is
the one your installed SWCraft carries, so its page lists the checks of the release that found
the problem; a check it does not list opens the page at its top. A run that could not check the
model names no check, so its one error has no link.

Two quick fixes come with the requirement-link warnings: *Confirm link to REQ-12 (version 4)* on a
link whose requirement has a newer version, and *State the version of REQ-12 (version 4)* on a link
with none. Each rewrites that one entry in the open file when you pick it; you save. Nothing is
offered for an error, for a link in a one-line list, or when the line has changed since the run.
To see them, put the cursor on the entry and press Ctrl+. (Cmd+. on macOS), or use *Quick Fix* on
the entry in the Problems panel or in its hover; no light bulb appears on its own. A save never
applies a fix, whatever `editor.codeActionsOnSave` says, and no fix is marked preferred, so the Auto
Fix keystroke does not apply one you did not pick.

## ARXML files

The extension associates `.arxml` with VS Code's built-in `xml` language, so an ARXML file is
recognised by path rather than by whether it happens to start with an XML declaration — which is
what makes a fragment or a hand-trimmed file open as XML too. With `redhat.vscode-xml` installed
that association is what gives it the outline, navigation and formatting.

SWCraft binds **no** XSD and ships no XML settings of its own. The AUTOSAR schemas are licensed and
this extension cannot carry them, and an in-editor red squiggle that looks like the gate and is not
would be worse than none. `swcraft validate` XSD-checks the generated ARXML against a licensed
schema when the machine has one and structurally when it does not.

One consequence you will see: with Red Hat XML installed, opening a generated `model.arxml` can
produce two markers on a perfectly correct file — `cvc-elt.1.a: Cannot find the declaration of
element 'AUTOSAR'` and `schema_reference.4: Failed to read schema document …`. LemMinX is reporting
a schema it could not find, not a defect in the file. `swcraft init` scaffolds the answer into the
project's `.vscode/settings.json`:

```json
{
  "xml.validation.filters": [
    { "pattern": "**/*.arxml", "noGrammar": "ignore", "schema": { "enabled": "never" } }
  ]
}
```

That turns a check off; it binds nothing. If you are licensed to hold the schemas and want in-editor
diagnostics against them, set `xml.fileAssociations` yourself and drop the filter. Note that
assigning `xml.validation.filters` replaces Red Hat XML's own two default filters, and that the key
is inert if that extension is not installed.

The scaffolded file also carries `"cSpell.ignorePaths"`, naming the model, `dist` and views folders
of the project's `swcraft.yaml` (`model/**`, `dist/**`, `views/**` by default), so that Code Spell
Checker, where it is installed, stops marking every model name as an unknown word. `swcraft init`
never edits a settings file that already exists: add the key by hand to an earlier project's.

## The MCP server, and the agent in Copilot Chat

The extension **registers SWCraft's MCP server with VS Code itself** — there is no file to
generate and none to repair. It publishes one server per open workspace folder whose root holds a
`swcraft.yaml`: the resolved `swcraft` executable, `mcp-serve --project <absolute manifest path>`,
with the folder as the working directory. Copilot's agent mode finds it with no configuration at
all; pick **SWCraft Architect** from the chat-mode dropdown after onboarding, and answer the first
per-tool permission prompt once as *Allow in this Workspace*.

The definition's environment sets exactly two keys: `SWCRAFT_REVIEW=panel` and `PYTHONUTF8=1`.
The first is the review boundary. Under the server's default `stage-only` mode nothing is asked and
**nothing commits** — a change set is computed, validated and abandoned. Under `panel` the server
renders the delta it computed and the extension opens a **SWCraft review tab** with that text and
**Approve** and **Reject**; only Approve commits, and Reject or a cancelled call abandons. The
question waits until you answer it, the assistant cancels it, or the assistant's session ends. A tab closed unanswered is reopened from the **Pending Change
Sets** view.
The same tab reviews every assistant that uses this server, `swcraft agent` in a terminal included,
so VS Code has to be open on the project while one works. The second key is there because an MCP
conversation is a pipe and Windows Python writes one in the locale encoding unless UTF-8 mode is
on.

Two things worth knowing about that registration. VS Code *merges* a server definition's
environment over the extension host's, so those two keys are what SWCraft **sets**, not the whole
environment the server ends up with. And only `<folder>/swcraft.yaml` is probed: a manifest nested
deeper in a workspace registers no server.

Running the server from a terminal instead is one line — `swcraft mcp-serve --project <path>` —
and there you set `SWCRAFT_REVIEW` yourself, or you get `stage-only` and nothing commits.
This registration covers VS Code only; another MCP host is configured by hand with that same
command line.

Ask model questions in **SWCraft Architect**, not in Copilot's general Agent mode. Agent mode reaches
the SWCraft tools too, but beside the terminal and file editing, so a question whose answer is too
large for one reply can end with it offering to run a script that parses your model files — with
nothing of SWCraft in between. The project's Copilot instructions tell every mode not to, but
instructions are guidance; only the agent's tool list takes the terminal and file edits away. In
the general Agent mode, set Copilot's Auto model choice to optimize for intelligence: optimized for
balance it can route a question to a lighter model that reads the model's files instead of asking
SWCraft.

The agent's profile grants SWCraft's tools and `view`, read-only, for one purpose: Copilot hands a
tool's answer over whole only up to about 8 KB and saves a larger one to a temporary file, which the
agent reads with `view`. The **model guard**, `.github/hooks/swcraft.json`, is a Copilot
`PreToolUse` hook that runs `swcraft guard` before every tool call, in every Copilot mode. Of the
file and search tools it knows, it denies a read of the model directory and a search of the model
directory or of a folder above it, the project folder included; a call among them it cannot judge
is denied too, and the saved answer is allowed. Where the project's `swcraft.yaml` is there but
cannot be read or is not valid, every file read and search but the saved answer is denied. It does not judge a terminal command, a file edit,
a tool outside its tables, or a read whose path arrives in a field it does not know; a search whose
folder arrives in such a field is judged as a search of the project folder, and is denied
(Copilot's Agent mode sends a read as `Read`, a search as `Grep` with its folders as a list, each
one judged, and SWCraft's own tools as `SWCraft-<tool>`); it reads only
the `swcraft.yaml` in the folder Copilot names, so a workspace rooted above the project is not
guarded; and it takes a `~` or `file://` path literally. Where no `swcraft` is on the path, or the
one there is from before the guard existed, the hook cannot run, and Copilot then denies **every**
tool call, SWCraft's included, until this release's `swcraft` is on the path; where hooks are
switched off (`chat.useHooks`) nothing runs and nothing guards the model. The full list is the
guide's *The model guard* section.

Codex has a model guard of its own, `.codex/hooks.json`, which runs `swcraft guard --host codex`
before each shell command and file edit: it denies an edit of a file in the model directory and a
shell command naming the model directory or a path in it, a command it cannot read as one, and an
edit of `swcraft.yaml` or `.codex/` — so Codex cannot change the pinned release or the manifest for
you. Codex runs it only once you trust it in Codex's `/hooks` list, and again after the file
changes; the extension cannot see that, so no Health row reports it. A hook that cannot start makes
Codex run the tool anyway, and a `swcraft` from before `--host` makes Codex block every shell
command and edit. What it does not reach is the guide's *The model guard under Codex* section.

Kiro's model guard, `.kiro/hooks/swcraft-guard.json`, runs `swcraft guard --host kiro` before every
Kiro tool call: it denies Kiro's reads, searches, edits, deletions and shell commands on the model
directory, edits of the guard's own files and the folders holding them, and any tool it does not
know, and it answers Kiro with exit code 2 and one line. A hook that cannot start, or runs past its
time limit, lets Kiro run the tool; an older `swcraft` blocks Kiro's file and shell tools. No Health
row reports it. What it does not reach is the guide's *In Kiro* page, under *The model guard under
Kiro*.

When a
whole table is too large even for a saved answer, the refusal names the command that prints it in
full, `swcraft query --request FILE --project PATH`, for you to run.

## Settings

The extension contributes settings of its own for five things only — the executable to run, one
configuration file per experiment, the revisions Architecture Metrics compares against, the
mapping file Import Requirements last used, and the annotations folder Test Tables last used:

- **`swcraft.executable`** — an absolute path to the `swcraft` executable to run. Leave it empty to
  use the workstation installation (`%LOCALAPPDATA%\SWCraft\current\Scripts\swcraft.exe` on
  Windows, `~/.swcraft/current/bin/swcraft` elsewhere). Set it only to run against a SWCraft
  checkout; when it is set, nothing else is looked for. It is a machine-scoped setting, so a
  workspace cannot set it for you: a value in a workspace's settings is ignored, and you set it in
  your user settings.
- **`swcraft.experiments.allocation.config`** — the configuration file **Allocation (CARMA)** runs
  with: a path relative to the project directory, passed to `swcraft experiment allocation` as
  `--config`. Leave it empty to run the experiment with its defaults. It is a resource-scoped
  setting, so a workspace's `.vscode/settings.json` may name it — it names a file the command reads
  as data, never a program to run.
- **`swcraft.experiments.trace.config`** — the configuration file the **Trace Links** search runs
  with: a path relative to the project directory, passed to `swcraft experiment trace` as `--config`.
  Leave it empty to search with the defaults. Resource-scoped, like the allocation one.
- **`swcraft.metricsSince`** — the git revisions **Architecture Metrics** compares the working tree
  against, a list of strings, empty by default; each entry is passed as one `--since=<rev>` word, the
  first being the base. Leave it empty to compare against `HEAD`. Resource-scoped, so a workspace may
  set it; a revision is data, never a command.
- **`swcraft.requirementsImport.mapping`** — the mapping file **Import Requirements** last used,
  passed to `swcraft import requirements` as `--mapping`: a path relative to the project directory,
  or an absolute one for a file outside it. The import writes it into the workspace folder's
  `.vscode/settings.json` when you pick a mapping file and clears it when you pick *SWCraft's own
  names*; only that folder's value is read. Resource-scoped, so a team may commit it; it names a file
  the command reads as data, and the command refuses one inside the model directory.
- **`swcraft.testTables.annotations`** — the test team's annotations folder **Test Tables** last
  used, passed to `swcraft generate test-tables` as `--annotations`: a path relative to the project
  directory, or an absolute one for a folder outside it. The first press writes it into the workspace
  folder's `.vscode/settings.json`; *none yet* is stored as an empty string, so the next press does
  not ask, and with no value at all the next press asks. Only that folder's value is read.
  Resource-scoped, so a team may commit it; it names a folder the command reads and never writes,
  and the command refuses one inside the model, `dist_root` or `views_root`.

Everything else you might expect — an XSD binding, a YAML schema store override, a formatter
default — is deliberately absent, so the pack members keep whatever configuration you gave them.

## The user guide

The whole guide came with your installation. **SWCraft: Open Guide**, in the `…` at the top of the
SWCraft view, serves it on this machine and shows it inside VS Code in an editor tab — search and navigation included, no network needed, and it describes the
release you actually have rather than whichever one is newest. From a terminal it is
`swcraft docs`.

Two refusals carry **Learn more**, which opens the guide's section on what to do: a window with no
folder or no `swcraft.yaml` (*Turning a folder into a project*), and a command refused because the
project pins another SWCraft release (*Upgrading*, *The procedure*). No other notification carries
one. When the assistant answers from the guide, its answer ends with a link to the section it read;
a click opens the guide at that section. A link whose section is not one the guide could hold (a
forged or damaged one) opens nothing and says so in the SWCraft output channel. A check's name in
Problems, **Learn more**, a section link and **Open Guide** all open the guide in the
window's one guide tab, reusing it when it is open; a tab moved to another window, a second guide
tab opened by hand and the fallback to the default browser still give another tab. The guide stops
with the window, so a guide tab VS Code puts back after a reload shows a page that cannot be
reached; the next guide link opens a new tab, and the old one is yours to close. A Remote window,
VS Code running in a web browser, and a link clicked while another VS Code window is in front,
have not been measured.

## Known limits

- **The review tab is as strong as the assistant's confinement.** A change set commits when its
  decision file is written into the SWCraft home's `reviews` folder, and the tab is the only writer
  of one among tools that stay inside the workspace. An assistant that may write anywhere in your
  home — a terminal command you let it run, an agent sandbox that covers the home, one with full
  access — can write that file itself and approve its own change set. Keep the assistant confined
  to the workspace while it works with SWCraft.
- **One project per workspace.** The manifest is looked for at the workspace root only. A monorepo
  with several `swcraft.yaml` files is not supported: open the folder that contains the manifest
  you want to work on.
- **Trusted workspaces only.** In Restricted Mode the extension is disabled — no view container, no
  commands — and VS Code shows the stated reason. That is deliberate rather than an oversight:
  every command that reaches the model runs an executable against the open folder's own manifest
  and model, and the registered MCP server starts a second process against the same folder, which
  is precisely what Workspace Trust exists to gate. Trust the folder to get the view and its
  commands back. Virtual workspaces are unsupported for the neighbouring reason: the CLI is a local
  process working on real files.
- **The diagrams are files, and a page does not update itself.** A save regenerates nothing; it
  marks the ARXML and the diagrams *out of date* on the Build and Diagrams items, and the diagrams'
  tab shows a new generation after the next press or Build. The record of the last successful generation belongs
  to the VS Code workspace, not to the files, so a file written from a terminal, or changed while
  the window was closed, reads *out of date* until the item is pressed; and a file removed from a
  terminal redraws nothing until the next save or press. **SWCraft:
  Build** removes and rewrites `views_root`, so a page reloaded during a build can find its file
  gone for a moment. Within one window, Build and Diagrams take
  turns; a second window on the same folder, or a `swcraft` command in a terminal at the same time,
  does not wait for them. In a remote window (SSH, WSL, a container) the tab reads the pages on the
  remote machine; that case has not been measured. A window reload closes the diagrams' tab.
- **A reinstall stops nothing.** *Install Toolchain* and *Update Toolchain* refuse to start while
  Build or Diagrams is writing, and those refuse while a toolchain run is going. SWCraft's MCP
  server is not this extension's to stop. The installer moves each runtime folder aside before
  replacing it, so a folder still in use is left intact; on Windows a reinstall while the server
  runs has not been measured.
- **Opening a zip needs a program.** *Install Toolchain* and *Update Toolchain* open a picked zip
  with Windows' own `tar` or with `unzip` from `PATH` elsewhere; a Linux machine without `unzip` is
  refused, and the unpacked directory is the route that needs nothing.
- **The pack is installed, not verified.** VS Code resolves the two members, and one that fails to
  install fails silently. *Check Editor Tooling* is the answer to that, and it too only asks whether
  a member is installed, enabled and inside its declared floor — it never activates or exercises
  one, and it cannot see what a member does on the network. Red Hat XML fetches referenced DTDs and
  XSDs by default (`xml.downloadExternalResources.enabled`), which is worth knowing on an audited
  network.
- **Navigation checks nothing.** Go to Definition, the hover, Find All References, the Outline, Go
  to Symbol, the references / impact / diagram / guide line and the links add no diagnostic and are not
  evidence of a correct model. If they are off, the extension
  says so once when it starts (its reference table is missing or damaged — reinstall the
  extension); *Check Editor Tooling* does not look at that table.
- **Check Model and Build need a toolchain of the same release.** Both ask the model check for its
  findings in a form an older toolchain does not know, so with one Check Model and Build's model
  check step fail with one entry on the first line of `swcraft.yaml` saying the toolchain and the
  extension are from different releases. Run *Update Toolchain*.
- **No editor diagnostic is evidence.** `swcraft validate` is the sole authoritative model and
  ARXML/XSD gate. Red Hat YAML flagging a model file and Red Hat XML validating an ARXML against a
  schema you bound yourself are both usability assistance from third-party software, and a green editor says nothing about the model.
  The SWCraft entries in the Problems panel are the model check's findings at the last run, not a
  gate either: they can be stale, and an empty panel is not a passed `swcraft validate` — nor is
  the status bar's `SWCraft: 0 errors`, which counts those same entries.
