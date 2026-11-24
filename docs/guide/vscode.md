<!-- draft: not yet reviewed by the product's author -->

# In VS Code

<!-- purpose: Finding your way around SWCraft in VS Code: the panel, the views, the review and the commands -->

This page is about where things are. Every SWCraft feature is reachable from the command
line, and the extension adds no model behaviour of its own — what it adds is a place to click,
a way to move between the model's files, and a set of things the editor can tell you while you
type. The first hour is much easier when
somebody has told you which strip of icons the panel lives in, so that is what this page
does, in the order you will need it.

Nothing here is required. If you never install the extension, the model, the commands and
every check behave identically.

## Install the extension { #install-the-extension }

<!-- purpose: Installing the SWCraft extension, or checking that the installer already did -->

**You have probably already installed it.** The extension travels inside the same archive as
the rest of SWCraft, and the installer hands it to VS Code as its last step — so a machine
that ran the installer with VS Code on it has the extension, at exactly the version the
commands it runs are. When VS Code was not there, or was installed afterwards, the archive's
`.vsix` is still in the unpacked directory and the Extensions view takes it: the `…` menu at
its top right, **Install from VSIX…**. Running the installer again does the same thing.

It needs VS Code 1.110 or newer.

The extension does not carry SWCraft itself — it runs the installation your machine has, and
finds it without being told where: `%LOCALAPPDATA%\SWCraft\current` on Windows,
`$HOME/.swcraft/current` elsewhere. The one setting that overrides that, `swcraft.executable`, is
there for somebody developing SWCraft against a checkout, and an ordinary install never sets
it. It is read from your user settings only: a value in a workspace's settings is ignored, and the
Settings editor offers it under *User* alone.

Two more extensions come with it. The archive carries them and the installer hands them to VS
Code just before SWCraft's own, so a machine with no access to the VS Code Marketplace gets them
too; installing the extension from a `.vsix` on its own brings them from the Marketplace instead,
because it is packaged as an extension pack:

| Extension | What SWCraft uses it for | Without it |
|---|---|---|
| **Red Hat YAML** | completion, hovers and per-file diagnostics on model files | model files open as plain YAML; nothing else changes |
| **Red Hat XML** | reading the generated ARXML with folding and structure | the ARXML opens as plain XML |

Both are optional in the sense that removing one costs you an editor convenience and
nothing else. None of them is ever consulted by a check. The copies in the archive are a starting
point rather than a fixed version: when your VS Code can reach the Marketplace it updates them
like any other extension. If one of them does not install, the installer says so in one sentence,
naming the file to install by hand, and carries on.

Neither of them needs Node.js. The diagrams do — they are laid out on it when they are
generated — and the installation places one beside `swcraft`, which the diagram generator uses
before any Node.js on your path, so there is nothing to set up.

## The panel { #the-panel }

<!-- purpose: The SWCraft panel: where each button is and which command it runs -->

**SWCraft has its own icon in the Activity Bar** — the vertical strip down the far edge of
the window, the one holding Explorer, Search and Source Control.

**If you cannot see the icon, you are not doing it wrong.** The strip only shows as many
icons as it has room for, and pushes the rest into the `…` overflow at its foot
(*Additional Views*). This is the single thing people lose the most time to. Two ways in
that do not depend on spotting an icon:

- open the Command Palette (`Cmd+Shift+P` on macOS, `Ctrl+Shift+P` elsewhere) and run
  **View: Show SWCraft**;
- once you have found the icon, drag it up the strip. VS Code remembers where you put it.

SWCraft wakes up when it finds a `swcraft.yaml` at the root of the open folder — or when you
run **SWCraft: Onboard Project** from the palette, which is how you reach it in a folder that
is not a project yet. Either way the panel's first line tells you what it found, so you never
have to guess which project a button is about to act on:

| First line | What it means |
|---|---|
| `SWCraft project detected` and a path | a `swcraft.yaml` was found at the workspace root |
| `No SWCraft project detected` — *Run Onboard Project* | a folder is open, but there is no manifest at its root |
| `No workspace open` | nothing is open yet |

Under it are three sections. Model and Experiments are always expanded; Setup is expanded until
the project is set up, and then folds away:

- **Model** — Build, Check Model, Diagrams, Test Tables, which generates the tables of placement and
  scheduling the test team verifies and opens them in a read-only tab ([its page](test-tables.md)),
  Change Report ([its page](change-report.md)),
  Resource Report, which fills the **Resources** view below the panel and opens the report of each
  core's and memory's budget in a tab ([below](#the-resources-view)), Architecture Metrics, which opens
  the report of the architecture's state and where it is heading in a tab ([its page](metrics.md)),
  Export Component, Import Component ([below](#importing-a-component)), Import Requirements
  ([below](#importing-requirements)).
- **Experiments** — Allocation (CARMA), which asks where a component could move and opens the
  answer in the Markdown preview ([below](#experiments)), and Trace Links, which searches for the
  links between your requirements and your model's elements and shows them in the **Trace Links**
  view below the panel ([below](#trace-links)).
- **Setup** — Install Toolchain, Update Toolchain, Onboard Project.

What depends on an element is not a tree item: you ask it from the element's own file, with
**SWCraft: Impact of This Element**, and the answer is drawn in the **Impact** view below the
Resources view ([below](#the-impact-view)).

Setup collapses by itself once three things hold: the toolchain is installed, a `swcraft.yaml` is
at the root of the open folder, and the project is onboarded (the Copilot agent profile Onboard
Project copies in is there). It is still there — click it to open it again. If one of the three
goes missing later, Setup opens again the next time the panel refreshes, because a setup step is
then the next thing to do. A `swcraft.executable` setting that holds a bare command name, found on
your `PATH`, does not count as an installed toolchain here, so Setup stays expanded in that case.

Two more commands are not in the tree, because they are help rather than steps: **Open Guide**
([below](#the-guide-without-leaving-the-editor)) and **Check Editor Tooling**
([below](#when-something-is-missing)). They are in the `…` menu at the top of the SWCraft view,
which VS Code shows when the pointer is over the view or the view has focus.

Below the panel, after **Trace Links**, **Resources** and **Impact**, the **Pending Change Sets**
view lists every proposal an assistant is waiting for you to review, with their count on its badge
([below](#review-in-vs-code)).

Most items run the matching command-line command and print its output verbatim into the
**SWCraft** output channel — nothing is re-rendered — except the model check of **Check Model**
and **Build**, whose findings go to the **Problems** panel and whose last line in the channel is
the extension's summary of the run, and **Resource Report**, whose answer fills the
**Resources** view and opens in a tab, and is never printed into the channel. **Build** runs six of those commands one
after another, and the first one that fails ends the run. When all six pass, the channel then also
shows one `swcraft resource-report` run, its command line and closing line and never its report:
that run fills the Resources view and opens no tab ([below](#out-of-date-a-failed-run-and-the-log)). A model check whose only findings are
warnings has not failed: **Check Model** succeeds and **Build** goes on to its next step, and the
`WARNING:` lines sit in the output channel with the rest of the command's output. Both commands
also list every finding of their model check in the **Problems** panel, at the line it names —
[below](#the-red-squiggles-are-not-the-gate). Every **Check Model** run that starts — successful or
not — ends by taking you to the **Problems** panel, and a failure's notification points there too;
with no `swcraft.yaml` found it starts no run and says so instead. **Build** ends in one of three
places, once its status and any failure's notification are shown:

- its model check stopped it with an error, or could not check the model at all: it takes you to
  the **Problems** panel, as Check Model does, and opens no report — this Build wrote none, and its
  first step removed the previous one;
- its last step, `swcraft validate --report`, ran — whether the ARXML then passed its check or
  failed it: the build report that step wrote, `build-report.md` in the manifest's `dist_root`
  (`dist/` in a scaffolded project), opens in the Markdown preview, as **Change Report**'s report
  does. [The command-line page](cli.md#the-build-report) says what the report holds. When the report
  could not be written or opened, the output channel says so;
- it failed anywhere else — at its first step or at a generator: the notification and the output
  channel, nothing more.

Warnings alone move nothing: the report opens, and the **Problems** panel keeps their entries
without taking the focus. No tree item reopens the report later: it stays on disk until the next
**Build**'s first step, `moon run :build` or `swcraft clean-outputs` removes it, and a later
`swcraft validate --report` replaces it. A report preview left open from an earlier **Build** keeps
showing that report when the next Build stops before its last step; the notification is the
answer then, not the preview.

The status bar at the bottom of the window counts SWCraft's findings from the last model check — `SWCraft: 0 errors · 35
warnings`; click the count to open Problems again. VS Code's own problem counts in the status bar
add up every extension's entries; this one counts SWCraft's alone, and a run that could not check
the model at all counts as its one error. Like the entries it counts, it describes the last run and
is not the gate: `0 errors` is not a passed `swcraft validate`. **Export Component** opens the ARXML
file it exported in an editor tab; if the file cannot be opened, a notification says the export
succeeded and names where it was written when the export printed that. One prints
no command output at all: **Check Editor Tooling**'s health report. The two toolchain items print something else again — the installer's own
output, because that is the program they run. **Open Guide**, in the `…` menu, keeps something
running after the click — the guide stays served until you close the window, logs quietly and
only opens its panel if something went wrong. **Diagrams** runs nothing afterwards — its tab reads
the files it wrote — and saving a
model file runs nothing either — it only marks what is out of date. Either way the extension never
writes a model file itself: the command line is the only thing that changes your model on its
own, **Import Component** and **Import Requirements** each run the command line's import only after
its dry run has shown you what it would change and you clicked **Write**, and a quick fix ([below](#the-red-squiggles-are-not-the-gate)) changes the open file
only when you pick it, for you to save.

While a command runs, the panel says so in three places and raises no popup: a thin
progress bar under the view's title, the item's text `running…`, and a text in the status bar at
the bottom of the window. Every command that runs the command line shows it — **Check Model**,
**Export Component**, **Import Component**, **Import Requirements**, **Test Tables**, **Change Report**, **Impact of This Element** (and
the Impact view's **Refresh**), **Resource Report** (and the
Resources view's **Refresh**, which runs the same report and opens no tab), **Architecture Metrics**,
**Allocation (CARMA)**,
**Trace Links** (and the view's **Apply…** and **Choose Another Element…**),
**Onboard Project**, **Check Editor Tooling**, **Install Toolchain** and **Update Toolchain** as
well as **Build** and **Diagrams** — and the status-bar text names the command: `Check Model — running…`.
**Check Editor Tooling** is an entry in the `…` menu, not a tree item, so it has no item to read
`running…` on: the progress bar and the status-bar text are its two places. Neither is **Impact of
This Element**, asked from an element's file: its two places are the same, its status-bar text
`Impact — running…`. **Build**
names the step it is on instead — `Build — 3/6 generate arxml` — and
moves on as each of its six steps starts; it reads `Build — waiting…` while it waits for a diagram
generation already running in the window. **Diagrams** reads `generating diagrams…` with no
fraction, because the command writes nothing and reports nothing until every view is drawn, and so
does every other command: none of them reports its progress, so none of them shows a fraction. A
command that asks you something first — which component to export, which file to import, which
name to trace, which folder to onboard, which archive to install from, the test team's folder on
the first **Test Tables** press — says nothing while it waits
for your answer; **Import Requirements** shows it once for its dry run and once more for the import
you approve, and nothing while it asks — both runs read `Import Requirements — running…`, so the
text alone does not tell you which of the two is running. **Import Component** does the same, with
one more run when you ask for an update's preview: every run of it reads `Import Component —
running…`.
No run can be cancelled from the panel. Choosing **Open Guide** in the `…` menu shows none of the
three: it starts a server that keeps running rather than a run that ends.

Afterwards the tree item says what happened and when — `built 12:39`, `ran 12:40`, `failed 12:41`. The
status clears itself after about half a minute, a failure keeps its message in the item's
tooltip until then, and the full text stays in the output channel for as long as you want it — except
**Resource Report**'s, whose report is in the Resources view and its tab while the channel keeps only its log,
and **Impact of This Element**'s, whose answer is drawn in the Impact view in the same way.

## Out of date, never regenerated behind your back { #out-of-date-never-regenerated-behind-your-back }

<!-- purpose: When Build or Diagrams says out of date: nothing regenerates when you save -->

Saving a model file regenerates nothing. Instead, **Build** and **Diagrams** each show the state of
the file they generate — **Build** the ARXML your manifest names as `outputs.arxml`, **Diagrams**
the diagram pages' `index.html` under `views_root`:

| State | What the item shows | When |
|---|---|---|
| not generated | the text `not generated` | the file is not there: a fresh clone, after `swcraft clean-outputs`, or after a **Build** that failed its model check |
| current | nothing extra | the last successful generation started after the model and the manifest last changed |
| out of date | a yellow dot and the text `out of date` | the file is there, and the model or `swcraft.yaml` changed after its last successful generation — or none is recorded |

The number on the SWCraft icon in the activity bar counts what is out of date — the ARXML, the
diagrams and the resource report the **Resources** view shows ([below](#the-resources-view)), from
`1` to `3`, its tooltip naming them — and disappears when none is; a file that is not there adds nothing to
it, and neither does a failed run by itself — but a Build that fails after it has written the
ARXML leaves the ARXML out of date, and that counts (the next paragraph says why). Hover over the
item to see what to press. While a run's own status is showing (`failed 12:41`), the item's text is
that status and the dot still shows the file's state; after it clears, the text reads `out of date`
again. An open diagrams' tab says the same about the diagrams in a line at the top of its page, with
a **Generate** button, and drops the line when they are current again.

Some cases read out of date on purpose. A **Build** that fails at its last step, the ARXML check,
leaves the ARXML on disk with no successful generation behind it. The record of the last successful
generation is kept by this VS Code workspace, not by the files, so switching branches, a file
changed while the window was closed, or an output a command in a terminal wrote all read out of
date until you press the item again. And a failed **Diagrams** press writes and removes nothing, so
pages that were out of date stay out of date.

One case reads current when it is not. A change is dated by the file's own modification time, so a
file moved or copied into the model with an old time kept — unpacked from an archive, or copied
with `cp -p` or `rsync -t` — or changed on a file system that keeps times only to a second or two,
can leave the files marked current. Press the item to renew them.

Every one of these is also in the Command Palette under the `SWCraft:` prefix, which is
usually faster than reaching for the panel once you know the name.

## Installing and updating SWCraft from the editor { #installing-and-updating-swcraft-from-the-editor }

<!-- purpose: Installing or updating SWCraft from the editor with an archive you were given -->

**Setup → Install Toolchain** (`swcraft.installToolchain`) and **Setup → Update Toolchain**
(`swcraft.updateToolchain`) are the archive's installer with the picking done for you. Both ask
what to point at — the `swcraft-<version>-<platform>.zip` you were handed, or a directory you
already unpacked — and then run the installer *out of that archive*. The extension carries no
copy of its own: what installs SWCraft is always the archive's own installer, so there is never
a second one to keep in step. Its output channel shows the installer's progress, one line per
step; the output of the programs the installer starts is in `install.log` in the installation
directory, whose path the run prints when a step fails ([Install](getting-started.md#install)).

The difference between the two is one question. *Update* compares the archive's version with
the one installed and refuses an older archive, because going backwards through this command
would be an accident rather than a decision; deliberate rollback is the previous archive's
installer, run the way you ran the first one. *Install* asks nothing and installs what you gave
it, which is also how a repair works — the same version again reinstalls in place.

Both commands leave running programs alone: nothing of the diagrams runs between two presses, and
SWCraft's assistant server, which may be running from the installation, is not stopped. The installer moves each runtime folder
aside before putting the new one in place, so a reinstall that cannot replace a folder still in
use stops with the previous folder intact. On Windows, a reinstall while those are running has not
been measured.

Two things follow the run. When the archive's version differs from the extension's, this window
is running the old extension against the new SWCraft, and the health check below reports exactly
that as a mismatch. The command offers **Reload Window** unless the archive's extension was not
installed — it is installed through the `code` command line, which is not always on the path VS
Code itself was launched with (on macOS, started from the Dock, usually it is not), and the run
says so and carries on. Then a reload would load the same extension again, so instead the command
names the `.vsix` in the archive and the *Install from VSIX* action in the Extensions view; take
that, and reload after it.

Neither command needs a Python on your machine: the archive brings its own, and its installer
runs on that. To open a **zip** the command uses a program your system already has — Windows' own
`tar` — and on macOS and Linux the `unzip` command. A Linux machine without `unzip` is refused
with one sentence; unpack the archive yourself and pick the **directory** instead, which needs no
program at all.

## Turning a folder into a project { #turning-a-folder-into-a-project }

<!-- purpose: Turning a folder into a SWCraft project from the editor, and what the onboarding writes -->

**Setup → Onboard Project** (`swcraft.onboardProject`) opens a folder picker, on the open
workspace folder when there is one, and sets the folder up in one pass. A project is never set
up inside another or around one. A folder inside an existing project — one with a parent folder
that already holds a `swcraft.yaml`, looked for up to the workspace folder, or up to the root of the
drive for a folder outside the workspace — is refused with a message naming the project's folder:
onboard that folder instead. A folder with no `swcraft.yaml` of its own — the one the scaffolder
would set up — that holds a project in a folder below it is refused with a message naming that
project's path: onboard that folder, or choose an empty one. A project's own folder is set up again
whatever it holds below it, examples included. Before it writes anything it
checks one thing — that the SWCraft it found actually runs — and stops with a message rather than leaving a half-configured folder
behind. That check is the one that fails on a machine where the toolchain was never installed,
and **Setup → Install Toolchain** is the answer to it.

**You choose the chat clients.** Before anything runs, a list asks which chat clients the
project is for — **GitHub Copilot**, **Codex (OpenAI)**, **Claude Code (Anthropic)** and **Kiro**, as
many as you like, with Copilot ticked to begin with (in Kiro, Kiro is) — and only the chosen
clients' files are written. Each client gets its own:

- **GitHub Copilot:** the assistant profile under `.github/agents/`, the Copilot instructions in
  `.github/copilot-instructions.md`, and Copilot's model guard, `.github/hooks/swcraft.json`.
- **Codex:** the instructions in `AGENTS.md`, the server line in `.codex/config.toml`, and Codex's
  model guard, `.codex/hooks.json`.
- **Claude Code:** `CLAUDE.md`, which has Claude Code read `AGENTS.md` (written too, the same file
  Codex reads), the server line in `.mcp.json`, and the guard's hook with the server's approval in
  `.claude/settings.json`.
- **Kiro:** the server line in `.kiro/settings/mcp.json`, the instructions in
  `.kiro/steering/swcraft.md`, and Kiro's model guard, `.kiro/hooks/swcraft-guard.json` — what they
  hold, and the steps Kiro needs once, are on [In Kiro](kiro.md#kiro).

Closing the list writes nothing, and so does accepting it with nothing ticked. A client you leave
out gets nothing, and none of its files already in the folder is changed or removed: choose it
later by running **Onboard Project** again. On the command line, `swcraft init --integration moon`
makes the same choice with `--client`, once per client (`--client codex --client claude`, or
`--client kiro`); without
it only Copilot's files are written ([Command line](cli.md#starting-a-project)).

Then, in order: it runs the project scaffolder, but only if there is no `swcraft.yaml` yet
(an existing manifest is never overwritten); and it copies in the chosen clients' files. Copilot's
three replace any copies already there, which is what you asked for by running it — but never
through a link: one of them that is a symbolic link, or a `.github` or `.github/hooks` folder that is
one, is kept as it is, with nothing written into it, and named in one line; move it aside and run the
command again. Codex's hook
file is the exception among the guards: Codex reads a single `.codex/hooks.json` per project, so one
that holds anything but SWCraft's guard is yours, and onboarding leaves it as it is and says so in
one line, in the output and in its closing message — add the SWCraft guard's entry (`swcraft guard
--host codex`) to that file by hand. One that holds only SWCraft's guard, an older version of it
included, is replaced.

For Copilot it writes **no chat configuration**, because none is needed. The extension registers
SWCraft's server with the editor itself, for every open folder whose root holds a
`swcraft.yaml`. Nothing about it is per-machine, and a colleague who clones the project has to do
nothing at all.

For Codex and Claude Code the files carry the server line, the guard's hook and the
instructions each client reads ([Codex and Claude Code in VS
Code](#codex-and-claude-code-in-vs-code) has what each holds). They name nothing per-machine, so
they go into version control with the project. Each is written where the project has none and
replaced where it is still SWCraft's own; a file of your own is kept, never merged, and the
onboarding output and its closing message name it with the guide section that says what to add by
hand. None of them is written through a link.

**Copilot with Codex or Claude Code.** Copilot reads a project's `AGENTS.md` and `CLAUDE.md` too,
and by default attaches them to every request in its chat — instructions written for the other two
clients, sent along with Copilot's own. So when Copilot is chosen together with Codex or Claude Code,
onboarding also writes `.vscode/settings.json` holding two settings that turn that off,
`"chat.useAgentsMdFile": false` and `"chat.useClaudeMdFile": false`. The file is written only where
the project has none (or completed, in a folder onboarding has just set up from nothing, where the
scaffolder wrote it a moment before); **an existing settings file is never edited**. Onboarding then
names the file and the two settings: add them to it by hand. `swcraft init` does the same and leaves
a settings file it keeps without them.

**Kiro with Codex or Claude Code** shows a warning of its own: Kiro reads `AGENTS.md` too, so it
reads SWCraft's instructions twice ([Kiro with Codex or Claude
Code](kiro.md#kiro-with-codex-or-claude-code)).

**Copilot with Claude Code.** That choice also shows one warning, and asks nothing — the choice
itself stands:

```text
Copilot also reads .mcp.json, so it may list the SWCraft server twice; see the guide's "Codex and Claude Code in VS Code" section (In VS Code).
```

`swcraft init` prints the same line after `Warning:`. What it is about is under [Which `swcraft` the
chats run](#which-swcraft-the-chats-run).

## While you type { #while-you-type }

<!-- purpose: The help the editor gives while you type a model file: completion, hovers and squiggles -->

Open any file under a layer directory and the editor starts helping immediately. The
extension ships one schema per layer and associates it **by path** — `**/tasks/*.yaml`,
`**/interfaces/*.yaml`, `**/swc/**/*.yaml`, and so on — so a project that renamed its model
directory still gets them. Two files outside the layers get the same help: `swcraft.yaml`, and a
requirements import's mapping file, by its name — any file whose name ends in `needs-mapping.yaml`
([Importing requirements](cli.md#importing-requirements)).

| Where | What you get |
|---|---|
| hovering a key | the field's description, and for an enumerated field its allowed values |
| completing a key | the field name, its description, and its **default** already filled in |
| completing a value | the members of an enumeration |
| an empty file | every field of that layer, plus a whole-element snippet with the required keys |
| a mistake | a diagnostic naming the offending property — unknown key, wrong type, value outside an enumeration, missing required key |

An unknown key is the one mistake where the editor and **Check Model** say the same thing: a key
the editor underlines as not allowed — in a model file or in `swcraft.yaml` — is also an error of the
model check (`model.unknown-key`, or `manifest.unknown-key` for the manifest), placed on the same
line, so a misspelt key stops Build instead of being dropped with its value.

A field's default reaches you through completion rather than hover; the description reaches
both. Everything the hovers say is also on the reference pages of this guide, laid out for
reading rather than for one key at a time — [The model layers](reference/model/index.md).

One curiosity worth knowing: `tasks/*.yaml` also matches a pattern in the public JSON Schema
Store, for Ansible task files. SWCraft's association wins, so your task files are checked
against SWCraft's schema and nothing else. You do not need to change any setting.

### Following a reference { #following-a-reference }

<!-- purpose: Following a reference from a model file to the element it names -->

The SWCraft extension also knows which fields name another element — a port's `interface`, a
data element's type, a connector end's component, a task's partition, an element's `satisfies` and
a requirement's `derived_from` — each entry leading to the requirement it names, whatever version
follows the `@`, while a `derived_from` entry naming an element, such as `hardware:CORE0`, leads to
that element — a component's `memory` and `data_type_mappings`, a memory's `accessible` cores,
and every other reference the model declares — and turns them into navigation:

| Where | What you get |
|---|---|
| **Go to Definition** (F12, or Ctrl/Cmd-click) on a reference line | the element's file, opened at its `name:` line |
| **Go to Definition** on a runnable's `reads`, `writes` or `calls` item | on `port:`, that port in the same file; on `element:` or `operation:`, the data element or operation inside the port's interface |
| **Go to Definition** on a runnable's `events` item | on a `data` event's `port:`, or an `operation` event's `port:`, that port in the same file; on a `data` event's `element:`, the data element inside the port's interface (or the external interface that lists it) |
| **Go to Definition** on a runnable's `reads_variables` or `writes_variables` item | on `variable:`, that variable in the component's own `variables`, in the same file |
| **Go to Definition** on a runnable's `can_enter` or `runs_inside` item | on `area:`, that exclusive area in the component's own `exclusive_areas`, in the same file |
| hovering a reference line | the element's kind, its name, its `description`, and the first lines of its file |
| **Find All References** (Shift+F12, or Peek References) on an element's own `name:` line (`id:` in a requirement) | every element that refers to it directly, each at its own `name:` line — on a requirement, the elements that satisfy it and the requirements derived from it, each at the line of the entry that names it (a component's own, its ports' and its runnables' entries each listed) |
| **SWCraft: Impact of This Element** in the editor's context menu, or in the Command Palette, in an element file | everything that depends on the element the line under the cursor names, else on the file's own element, directly or not, in the **Impact** view ([below](#the-impact-view)) |
| the **Outline** view, the breadcrumbs, and Go to Symbol in Editor (Ctrl/Cmd+Shift+O) | the element, its collections and their members as the file holds them — a component's ports, variables, exclusive areas and runnables, each runnable's events, reads, writes, calls, variable reads, variable writes, can-enter and runs-inside entries; an interface's data elements or operations and their arguments; and so on for every layer |
| **Go to Symbol in Workspace** (Ctrl/Cmd+T) | every element of the model by name, with the folder it is stored in beside it (`interfaces`, `swc/application`) |
| the line above an element's `name:` line | `references` (click for Find All References on this element), `impact` (**Impact of This Element** on this element, its answer in the Impact view) and `diagram` on every element a diagram draws: for a composition or a sequence whose diagram page exists it opens that page in the diagrams' tab — straight away when the diagrams were generated in this window and nothing in the model or `swcraft.yaml` has changed since, otherwise after regenerating them (the panel's **Diagrams** always regenerates); for any other element it runs **Show in Diagram**, which opens the diagram that draws it with the element selected and centred, asking which one when several do ([In VS Code, to the model and back](diagrams.md#in-vs-code-to-the-model-and-back)). The list of what is drawn is the last generation's: a `diagram` link appears or goes after a press, a **Build** or a reload of the tab, not on a save. Last in the row, `guide` opens this guide's reference page for the file's layer in the guide tab, at the section of the element's kind — an interface at *sender_receiver*, *client_server* or *external_interface*; a file whose `kind:` cannot be read at the top of the page |
| **SWCraft: Show in Diagram** in the editor's context menu, or in the Command Palette, in an element file | the diagram that draws the element at the cursor — the port or the runnable the cursor is in, else the file's own element — with it selected and centred, in the diagrams' tab; *not on any diagram as last generated* with **Generate Diagrams** when none does, and *cannot be shown: no diagrams list the model's elements yet* with the same button when no diagrams were generated, or an earlier release generated them |
| a `description` that names an element — `interfaces/IBrakeTemperature` or just `IBrakeTemperature` — and an ARXML import report (`import model` or `import component`) written with `--report` to a file not ending in `.md` (a `.md` report is Markdown, with its names escaped, and has no links) | the name becomes a link to the element's file |

Nothing is indexed and nothing is stored: each answer is computed when you ask. Definition and
hover read the target file from disk, so they see its **saved** content, not an unsaved edit in
another tab. Find All References asks the `swcraft` CLI (`swcraft impact`), which reads the saved
files of the whole model: it needs the toolchain installed, and while another model file does not
parse it shows the CLI's message instead of a partial list. An entry opens the referring
element's `name:` line, not the field inside it that names your element — except on a
requirement, where each entry opens the line that names it (a link in a one-line list opens the
element's `name:` line, and an element that also names the requirement in a list of one entry per
line is listed at those lines only). On a hardware element that a requirement's `derived_from`
names, such as `hardware:CORE0`, the requirement is listed at its `id:` line. If the CLI names a
referring element whose file the extension cannot find — a component stored more than eight
folders deep under `swc/` — the message names it rather than leaving it out of the list. If you
have changed the element's `name:` line and not saved, it asks you to save first, because the CLI
reads the saved file.

The `references` item shows no count and runs nothing until you click it, so the line appears with
the file and its `impact` and `diagram` answer at once, on a large model too. A click is Find All
References on the element, run at that moment, with everything said above: it lists the elements
that refer to it directly and, as Shift+F12 does, the element's own `name:` line among them, and
where the CLI cannot answer it shows the CLI's message. There is nothing to save first for the line
to appear. Go to Symbol in Workspace reads folder listings, not files, so an element
is listed under the name of its file. A bare name in a `description` becomes a link only when
exactly one layer of the model holds an element file of that exact name, and never to the
element the description belongs to; a folder-qualified name such as
`interfaces/IBrakeTemperature` is never ambiguous. In an import report the model importer's
quoted names and the folder-qualified names both link; a component import report links its
folder-qualified names only. A requirements import report gets no links, whether written with `--report` or opened by
**Import Requirements**. Messages in the output channel and the Problems panel are never links.

A line written in a way the extension does not read — a flow value such as `ports: [ … ]`, a
value continued over several lines, a quoted value with an escape, an anchor or alias, or a key
written twice — simply gets no navigation; it never jumps somewhere wrong. The files SWCraft itself writes
use the plain block style the extension reads. None of this checks your
model: navigation adds no squiggle, and the next section's rule holds for it too.

## The red squiggles are not the gate { #the-red-squiggles-are-not-the-gate }

<!-- purpose: Why the editor's red squiggles are not the gate, and which command is -->

This is the sentence to read twice.

**The editor's diagnostics are usability assistance. `swcraft validate` is the
authoritative gate** — and it is the last step of **Build**. A file with no squiggles in it
can still be refused by it, and that is normal rather than a bug in either one.

The editor's marks cover the files open in it only: close a file and its schema marks leave
Problems, and they come back when you open it again. **Check Model** checks the whole model, open
or not.

They are not two views of one answer. A schema sees exactly one document at a time, and
almost everything that makes an architecture correct is a statement about *more than one*
document: whether a port's interface exists, whether two components share a name, whether
the emitted AUTOSAR name is still legal after SWCraft has lengthened it, whether a task's
partition is a partition the hardware declares, whether the budgets hold. None of that is visible to a
per-file check, and all of it is visible to `swcraft validate`. [The checks](reference/rules.md) is
the full list.

The same rule runs in the other direction, and it surprises people once. Open the generated
`dist/model.arxml` with Red Hat XML installed and you may see

```text
cvc-elt.1.a: Cannot find the declaration of element 'AUTOSAR'
```

**Nothing is wrong with the file.** The XML server reads the document's own schema location,
looks for the AUTOSAR schema next to it, and reports what it could not find. The AUTOSAR
schemas are licensed and SWCraft ships none. Project scaffolding writes a
`.vscode/settings.json` that switches that check off for `.arxml` and only for `.arxml`; an
older project has no such file, so add it, or bind the schemas yourself if you are licensed
to hold them. Either way an editor marker is not the gate, in either direction.

The same file keeps Code Spell Checker, if you have it installed, out of the model and the
generated files: its `"cSpell.ignorePaths"` names the model, `dist` and views folders your
`swcraft.yaml` declares, as `model/**`, `dist/**` and `views/**` in a new project. Without it the
spell checker marks every name in the model as an unknown word, since a name such as
`REQ_BTP_DECOMP_MAIN` is no word of any language. `swcraft init` writes the key only into a settings
file it creates: a project made with an earlier release, or one whose `.vscode/settings.json`
already existed, has no such key, so add it by hand with your own folders. A misspelling in a
model's `description` or a requirement's text is then no longer marked either. The paths are read
from the folder you open in VS Code, so a project opened from a folder above it is not covered, and
a folder you rename in `swcraft.yaml` later is not followed: edit the key with it. A folder whose
name holds a character a file pattern reads as a wildcard (`*`, `?`, `[`, `]`, `{`, `}`, `(`, `)`,
`!` or `\`) gets no entry, so its files stay spell-checked; a space needs nothing.

### The model check's findings in the Problems panel { #the-model-checks-findings-in-the-problems-panel }

<!-- purpose: Reading the model check's findings in the Problems panel and jumping to each one -->

**Check Model** and **Build** also put every finding of their model check into the **Problems**
panel: an error in red, a warning in yellow, with `swcraft` as the source and the check's name as
the code — `trace.link-needs-confirmation`, for one. Each entry sits at the line the finding
names, such as the `- REQ-12@3` entry under a port's `satisfies`; where that line cannot be found —
a list written on one line, `satisfies: [REQ-12@3]`, or a file edited since — it sits at the
element's `name:` line, and a finding about no one element sits at the first line of
`swcraft.yaml`. A finding on a field's value underlines the value — `IServer` in
`interface: IServer`, with its quotes when it is quoted — and the key when the value is not
written on the key's line: a list or a mapping, a `|` block, or nothing at all. A finding on a key
itself, such as `model.unknown-key`'s misspelt key, underlines the key.

A run refused because a model file is malformed — a required field missing, a value of the wrong
type, a line the YAML reader cannot parse — has one entry per field on that file, each an error
whose message is the refusal's line: `Invalid task YAML in …/Task_bad.yaml: period: Input should
be a finite number`. The entry marks the whole line of the field where it can be found, the line
the YAML reader names for a parse error, and the file's first line otherwise — a missing field, a
rule about the element as a whole, a key written inside a one-line `{…}` mapping. A quoted key that
holds a control character is written escaped in the message, `\n` for a newline and `\t` for a tab,
so the entry stays one line; such a key is never found on a line, so a top-level one sits at the
first line too. A field inside a list item sits at the list's key line, not on the item. A run
stops at the first malformed file, so a second one shows only once the first is fixed, and a file
that cannot be opened puts its entries at the first line of `swcraft.yaml`. That first line is
otherwise only for a run that could not check the model and names no model file — a manifest it
could not read, a command that could not start — as one error whose message is the reason.
**Build**'s model check places all of these the same way.

A **Check Model** run refused because the toolchain pin does not match is that one error too, and
it sits at the `toolchain_version` line of `swcraft.yaml`. The whole line is marked, so a comment
at its end is underlined with it. It falls back to the first line when that line cannot be found —
no such key at the left margin, as in a manifest written `{toolchain_version: …}` on one line or
with its top-level keys indented — and a `toolchain_version` that is no single release, a range
for one, is another refusal and stays at the first line as well. **Build** puts no entry in
Problems for the pin: its first step refuses before its model check runs, so it ends at the
notification with **Learn more**, and the entries of an earlier run stay as they were.

**The check's name is a link.** Click `trace.link-needs-confirmation` in an entry and this guide
opens inside VS Code on its [checks page](reference/rules.md), at that check's row, which says what
the check requires. The guide is started first when it is not running yet (see
[The guide, without leaving the editor](#the-guide-without-leaving-the-editor)). It is the guide
your installed SWCraft carries — the same release that found the problem — so its page lists that
check; a check the page does not list opens the page at its top. The one error of a run that could
not check the model names no check, so it has no link. A guide link at the end of an assistant's
answer opens the guide tab the same way, at the section the answer was drawn from.

**Check Model** and **Build** need a toolchain of the same release as the extension: they ask the
model check for its findings in a form an older toolchain does not know, and such a run ends with
that one entry, saying the toolchain and the extension are from different releases. Run **Update
Toolchain**.

These entries are the model check's own findings, placed in the editor — not the editor's opinion
of your model, and still not the gate. They describe the **saved** model at the last run: a save
runs nothing, so an edit since then is not in them, and the next **Check Model** or **Build**
replaces them all. A **Build** that fails before its model check leaves the previous run's entries
standing, and when a **Check Model** and a **Build** overlap, the run started last fills the
panel. Before a release, the answer is still `swcraft validate`.

**Two quick fixes** come with the trace-link warnings. On a link whose requirement has moved on
(`REQ-12@3` while the requirement says `version: 4`), the quick-fix menu offers *Confirm link to
REQ-12 (version 4)*; on a link with no version (`REQ-12`), *State the version of REQ-12 (version 4)*. Each
rewrites that one entry to `REQ-12@4` in the open file — only when you pick it — and you save. The
version comes from the requirement's saved file. Nothing is offered for an error, for a link in a
one-line list, or when the line has changed since the run — it no longer links to that
requirement, or no longer in that state; run **Check Model** again. You open the menu yourself: put
the cursor on the entry and press Ctrl+. (Cmd+. on macOS), or use *Quick Fix* on the entry in the
**Problems** panel or in its hover — no light bulb appears on its own. A save never applies a fix,
whatever `editor.codeActionsOnSave` says, and no fix is marked preferred, so the Auto Fix keystroke
does not apply one you did not pick.

## Diagrams, and the shortest way to see one { #diagrams-and-the-shortest-way-to-see-one }

<!-- purpose: Seeing the diagrams from the panel in one click, and what the Diagrams button runs -->

**Model → Diagrams** (`swcraft.diagrams`) runs `swcraft generate diagrams`, which writes the model's
diagrams as files into the directory your manifest declares as `views_root`, and then shows
`index.html` in **a tab of the editor** — one per window, which every press reuses and brings to the
front. Nothing is served and nothing listens on a port: the tab reads the files, so there is no
firewall prompt and nothing another computer could reach. In the tab a Ctrl+click (Cmd+click on
macOS) on a drawn element opens its YAML file beside the diagram, and the `diagram` link above an
element's name, or **SWCraft: Show in Diagram** (`swcraft.showInDiagram`) in the editor's context
menu, opens the diagram with that element selected — [In VS Code, to the model and
back](diagrams.md#in-vs-code-to-the-model-and-back) has the whole of it, and
[Diagrams](diagrams.md) what the views are and how to read them. The press no longer opens a
browser; to look at a page in one, open `index.html` from the file, and to share a picture, take
its SVG file.

**Saving a model file does not regenerate the diagrams.** The **Diagrams** item turns *out of
date* instead (see above), and so does a line at the top of the open tab, with a **Generate** button
that runs the press. After a press, or a **Build** that generated them, the tab shows the new
drawing by itself; the page's **Reload** button reads the page shown from the disk again. A window
reload closes the tab, and the next press opens it again.

A model the generator refuses — or a machine with no Node.js it can use — shows the refusal in the
**SWCraft** output channel, opens nothing and leaves `views_root` exactly as it was. A manifest
whose `views_root` is absolute, is the project directory itself or lies outside the folder is
refused before anything runs, with the sentence `swcraft clean-outputs` would give.

**Build** empties `views_root` with the other outputs before it checks the model, so a **Build**
that fails the check leaves the diagrams directory absent, and the next **Diagrams** press writes it
again. Within one window, **Build** and **Diagrams** take turns. A
second window open on the same folder, or a `swcraft` command run in a terminal at the same time,
does not wait for them, and a **Build** can then fail on the diagrams directory.

**Install Toolchain** and **Update Toolchain** can replace the installation these commands run
from, so within one window they do not overlap: while **Build** or **Diagrams** is running, the
toolchain commands are refused with a sentence asking you to run them again when it has finished;
and while a toolchain command is running, **Build** and **Diagrams** are refused the same way. The
other commands in the panel are not held back, and neither is another window or a terminal.

In a remote window — over SSH, in WSL or in a container — the pages are written on the remote
machine and the tab reads them there; that case has not been measured.

## Talking to the model { #talking-to-the-model }

<!-- purpose: Talking to the model through a chat assistant in VS Code: the server, the profile and the guard -->

Four pieces make this work, and three of them are files. SWCraft's assistant server is
registered with VS Code by the extension itself — no configuration, nothing in your project.
The assistant profile under `.github/agents/` and Copilot's model guard,
`.github/hooks/swcraft.json`, are what onboarding copies in when Copilot is among the clients you
choose ([Turning a folder into a project](#turning-a-folder-into-a-project)). This section is
about Copilot; Codex and Claude Code get files of their own, described in [Codex and Claude Code in
VS Code](#codex-and-claude-code-in-vs-code), and so does Kiro, described in [In Kiro](kiro.md#kiro).

A project that has no profile — one you imported, or one set up before the extension carried it —
gets a notice when the window opens: **Add the SWCraft Architect agent** writes the profile, and
writes `.github/copilot-instructions.md` and `.github/hooks/swcraft.json` only where the
project has none — Copilot's files alone; Codex's and Claude Code's come from **Onboard Project**. It never overwrites a file, never writes through a link and never runs the scaffolder. A project set up for Codex or Claude Code alone — their files present and none of Copilot's — gets no notice. **Not for this workspace** stops the notice for this
workspace; closing it without a choice asks again the next time.

The registration covers every open folder whose root holds a `swcraft.yaml`, and a manifest
further down inside the folder registers nothing: the workspace root is where SWCraft looks.

Open Copilot Chat, then use the **chat-mode dropdown** in the chat box — the picker that
chooses between the built-in modes and any custom agent it found — and pick **SWCraft
Architect**. *This is the step everybody misses.* The built-in modes are general coding
assistants, and they answer anyway, which is what makes the mistake hard to notice. Copilot's
Agent mode reaches the SWCraft tools too, but beside the terminal and file editing, so a question
whose answer is large can end with it offering to run a script over your model files — see
[the assistant page](assistant.md).

The profile grants that agent SWCraft's tools and one read-only file tool, `view`, and nothing
more — no file editing, no file creation, no terminal — so it cannot hand-edit a layer file even
if you ask it to. It reads your model through SWCraft while [the model guard](#the-model-guard)
runs, within the limits listed there; a `view` of another project file is not denied. Ask your model questions
there too: Copilot's general Agent mode has the terminal, and faced with an answer too large for
one reply it may offer to run a script that reads your model files itself. The project's Copilot
instructions tell it not to, in every mode — but instructions are guidance, and only the agent's
tool list takes the terminal away. If you do ask in the general Agent mode, set Copilot's **Auto**
model choice to optimize for intelligence: optimized for balance it can route a question to a
lighter model, and a lighter model was seen answering a partition question wrong by reading the
model's files instead of asking SWCraft. In the SWCraft Architect agent the same questions were
answered through SWCraft.

The first time it calls each tool, VS Code asks whether to allow it. Answer **Allow in this
Workspace** once per tool and the prompts stop for that project. **That permission is not
the review** — it authorises running a tool, and says nothing about the change the tool
computed. [The assistant](assistant.md) is about where a change is actually reviewed, and it
is the page to read before you let one commit anything.

The review itself is not in the chat: it is a tab VS Code opens by itself, the same for every
assistant, and [Review in VS Code](#review-in-vs-code) below is what it shows and what it cannot
guard against.

If the assistant reports changes that never appear in your files, the review setting is what
to look at. The extension always sets it on the server it registers, so what to check is a
second SWCraft server you configured by hand — that one carries whatever you gave it.

### Review in VS Code { #review-in-vs-code }

<!-- purpose: Reviewing an assistant's proposal in the review tab, and approving or rejecting it -->

When an assistant's proposal is ready, VS Code opens a **SWCraft review tab** by itself, beside
your editor and without taking the focus, so nothing you are typing in a chat can answer it. The
tab shows the change exactly as the server computed it, and under it **Approve** and **Reject**.
One click decides: there is no second dialog, and typing *approve* in the chat does nothing.
A proposal that would weaken a check opens a second tab, for a separate question, after you
approve the change — approving the change does not approve the weakening, and
[The assistant](assistant.md) lists when that second question comes.

A change normally touches only the lines it needs. When a file cannot be edited that way and will be
rewritten whole, a yellow **WARNING** block stands above the change in the tab, names every such file
and says what is lost; [The assistant](assistant.md#the-review-and-the-one-guarantee-worth-knowing)
explains when. The yellow belongs to the tab: in a terminal the same words appear with no colour.

Every assistant that uses this project's SWCraft server is reviewed in that same tab — Copilot,
another chat client, and `swcraft agent` in a terminal alike (a scripted rehearsal excepted: its
answers are in its script). So keep VS Code open on the project, with the SWCraft extension, while
any of them works. A proposal waits for as long as it takes: until you answer it, the assistant
cancels it, or the assistant's session ends — and in those last two cases it is abandoned with
your files untouched. With no such window open nobody can answer, so the assistant simply goes on
waiting. SWCraft's server does not end the wait when the machine sleeps; whether an assistant
does is its own matter, and [Codex and Claude Code in VS Code](#codex-and-claude-code-in-vs-code)
and [Another chat client](#another-chat-client) say what each one was tried with.
The tab says since when the proposal has been waiting.

Closed a tab without answering? The **Pending Change Sets** view in the SWCraft panel lists every
proposal still waiting for the projects open in this window, its badge counts them, and a click on one reopens its
tab. A proposal that was answered in another window, given up by the assistant, or whose
assistant's server stopped leaves the list, and its open tab says it is no longer waiting.

The tab is the only place SWCraft's tools let an answer come from, but the answer is a file the
extension writes into your own SWCraft folder, outside the project. Anything allowed to write
anywhere in your home could write that answer instead of you — a terminal command you let an
assistant run, Copilot's own terminal included, or an assistant you started with full access to
your disk. **So use the review tab only with an assistant that works confined to the project
folder**, and read a terminal command before you allow it. Keep the project in a folder of its own, never
your home folder itself: when the project folder contains your SWCraft folder, every proposal is
turned down with a note saying so. A VS Code window opened on a folder wider than the project, your
home folder for one, SWCraft cannot see, so open the window on the project folder.

### A large answer, and why the agent has `view` { #a-large-answer-and-why-the-agent-has-view }

<!-- purpose: When the assistant reads a large answer from a file: why it has the view tool -->

Copilot hands a tool's answer to the model whole only up to about 8 KB. A larger answer — a
project overview, what a change to a much-used interface affects, a long table — is saved to a
temporary file, and the model is told where it is and shown its first few hundred characters.
`view` is there so the agent reads that file and answers from all of it, rather than from the
preview. The profile tells it to view the saved answer and nothing else.

### The model guard { #the-model-guard }

<!-- purpose: What the model guard lets Copilot do before each tool call, and what it refuses -->

Every rule of the guard under Copilot,
and everything it does not reach, is on [The model guard's rules](model-guard.md#under-copilot).

`.github/hooks/swcraft.json` asks Copilot to run `swcraft guard` before every tool call, in the
SWCraft Architect agent and in every other Copilot mode. The guard looks at the call and:

- **denies** a file tool's read when any path it names is under your model directory, and a search
  whose folder is the model directory, inside it or above it — the project folder included, since
  a search there reaches the model. A search's file pattern, such as `model/**/*.yaml`, is judged
  by the folders it starts with, and a pattern that could climb back up with `..` after a wildcard
  is denied. A read or search whose arguments cannot be decoded is denied too. The agent is told to
  use the SWCraft tools instead;
- **denies**, whatever the client then does with it, a read path or a search's folder or file
  pattern that holds your model directory's path anywhere in it: for a model directory `model`,
  `zz/yy/model/swc/Engine.yaml` and `notes/MODEL/x.md` are denied, for `arch/model` both folders in
  a row (`zz/arch/model/x.yaml`), and also a path written relative to the project that starts with
  the last of them (`model/x.yaml`). Names are compared whatever their case, `..` is not applied
  first, and an absolute path inside the project is judged only from the project folder down. So a
  client that resolves a path in its own way, or a later release of one, still cannot reach the
  model through a path that names it;
- **denies**, likewise, a read path or a search's folder or file pattern whose last folder and name
  are those of a file or folder in your model directory: while `model/swc/Engine.yaml` exists,
  `elsewhere/swc/Engine.yaml` and the pattern `notes/**/swc/Engine.yaml` are denied, because a
  client that looks a file up by its name could find the model's. To know those names the guard
  lists the model directory once per call. When it cannot list it whole — more than 100,000 files
  and folders, more than two seconds, or a folder it may not read — it denies every path of two or
  more parts, an absolute path included, whatever it names, until you make the model directory
  readable again (its permissions, a broken mount) or keep less in it;
- **allows** a saved SWCraft answer, any other file, and every other tool — except where the
  project's `swcraft.yaml` is there but cannot be read or is not valid: the guard cannot tell where
  the model is, so it denies every file read and search but the saved answer, and allows every
  other tool.

What it does not reach:

- **The terminal.** What a command will read cannot be judged from its text, so a command in the
  general Agent mode is not guarded. Copilot asks you before it runs one.
- **Editing.** A file edit is not guarded; the SWCraft Architect agent has no file edit to make.
- **A guard that cannot run.** The hook runs the `swcraft` found on your path. Where none is
  found, or the one found is a release from before the guard existed, the hook cannot run, and
  Copilot then **denies every tool call** — SWCraft's own tools included — with *"hook errored"*
  or *"hook exited with code 2"*. Put this release's `swcraft` on your path (and restart VS Code
  so it sees the new path); **Check Editor Tooling** says when this is the case. Where hooks are
  switched off in Copilot (`chat.useHooks`), or the project has no hook file, nothing runs:
  Copilot is not blocked, and nothing guards the model.
- **Another chat client.** This hook is Copilot's mechanism; another client is not guarded by it.
  Codex and Claude Code have hook files of their own, [under Codex](#the-model-guard-under-codex)
  and [under Claude Code](#the-model-guard-under-claude-code) below.
- **A tool or a path field the guard does not know.** It knows the file and search tools Copilot
  names, and the fields their paths arrive in. In Copilot's Agent mode a read arrives as `Read`
  and a search as `Grep`, whose folders come as a list, each one judged; SWCraft's own tools
  arrive as `SWCraft-<tool>` and are allowed. Other names Copilot has used for the same tools are
  listed too, and so are `read` and `search`, the names a custom agent's profile may give the file
  and search tools; a name is matched whatever its case. Any other tool is allowed, and so is a
  read whose path arrives in a field the guard does not know; a search whose folder arrives in such
  a field is judged as a search of the project folder, and is denied.
- **The text a search looks for.** A `Grep` pattern is a regular expression and is not judged as
  a path; the guard judges where the search looks, not what it looks for. A `search` call's
  pattern is the exception: `search` may name either tool, so its pattern is judged as a glob, which
  can deny a regular expression that looks for text outside the model.
- **A file pattern past its first wildcard.** The folders after it are checked only for whether
  they could climb with `..`. That check is cautious, so it also denies some harmless patterns:
  `.*`, an excluding pattern such as `!**/x`, a folder written as a group such as `(draft)`, and
  any pattern holding a backslash.
- **Another folder.** The guard reads only the `swcraft.yaml` in the folder Copilot names. Open the
  project folder itself: a workspace whose root is a folder above the project is not guarded.
- **`~` and a `file:` URL.** A path written that way is taken literally, never expanded.
- **A path that does not spell the model directory.** The rule on the model directory's path judges
  the path as written: a path that names the model directory with a pattern (`mod?l`), splits it
  with `..` (`arch/x/../model`), uses other Unicode forms of the same letters, or a Windows short
  name (`INTERF~1`) is judged by the folder rules above alone. The rule on the model's own names judges a path's last folder and name as
  written too: a single name (`Engine.yaml`), a pattern whose last two parts hold a wildcard, a path
  that ends like a model file only once its `..` is applied, and a file added to the model while the
  guard judges the call are not reached by it.
- **Another folder named like the model directory** is refused too, the cost of judging the path as
  written: `docs/model/notes.md` for a model directory `model`, a path through `..` that passes the
  model directory's name, and, for a model directory of several folders such as `arch/model`, a path
  written relative to the project that starts with `model`. So is a file outside the model whose
  last folder and name are a model file's: `docs/interfaces/Foo.yaml` beside
  `model/interfaces/Foo.yaml`. Rename that folder or file, or let the agent use the SWCraft tools.
- **Network and drive-relative paths.** A path or file pattern written as a network path
  (`//server/share/...`, `\\server\share\...`) or relative to a drive (`C:notes`) is denied
  wherever it points, because it can reach the model under another name. A project kept on a
  network share and opened by its network path therefore has every file read and search denied.
  On macOS and Linux a path that starts with a drive letter, a colon and a slash, as on Windows, is denied too.
- **A change on Copilot's side.** If Copilot changes where or under what name it saves a large
  answer, the agent can no longer read it — the model stays guarded either way.

A project set up before the extension carried the guard has a profile already, so the notice above
does not appear for it. **Check Editor Tooling** reports the guard as missing, and **Onboard
Project** brings the guard, the current profile and the current Copilot instructions — it
overwrites all three, so keep a copy of anything you changed in them. Codex's hook file, when
Codex is among the clients you choose, it writes only where there is none or where it holds SWCraft's guard and nothing else, and never through a
link: a `.codex/hooks.json` that is a link, or a `.codex` folder that is one, is kept as it is, with
nothing written into it. The file is written so that the write itself follows no link and replaces
no file that appeared after the check; the one gap left is another program swapping the `.codex`
folder for a link in the instant between the last check and the write.

### The model guard under Codex { #the-model-guard-under-codex }

<!-- purpose: What the model guard decides under Codex, before each shell command and file edit -->

Every rule of the guard under Codex,
and everything it does not reach, is on [The model guard's rules](model-guard.md#under-codex).

`.codex/hooks.json` asks Codex to run `swcraft guard --host codex` before each shell command and
each file edit. Codex edits a file with a patch and reads and writes files from its shell, so this
guard judges what the Copilot one leaves alone:

- **denies** an edit when any file it adds, changes, deletes or moves is in your model directory,
  and a shell command when any word of it names the model directory or a path inside it —
  `cat model/swc/Probe.yaml`, `sed -i … model/swc/Probe.yaml`, `echo x >> model/notes`, `ls model`
  — or is a wildcard that could match one, such as `ls */swc` or `rm -rf ?*`. A wildcard is also
  checked against what it really matches in the project, so one that matches a link into the model
  directory (`cat short*/swc/Probe.yaml`) is denied; one over a folder of more than 1 024 entries is
  denied as too much to judge in time, even when the model is not among them. A command run from
  inside the model directory is denied whatever it names. A command the guard cannot read as a
  shell command — an unbalanced quote, a heredoc whose text holds an apostrophe — is denied
  whatever it names, and so is an edit it cannot read, a command or an edit too long, naming too
  many paths or crossing too many links to judge within three seconds, a path through more
  symbolic links than the system follows, and one nested in more than four `bash -c` levels. A
  cluster of short options is read for a path glued to its last option too, so
  `sort -uomodel/swc/Probe.yaml notes.md` is denied. An edit whose file's path holds the model
  directory's path anywhere, starts with its last folders or ends like a model file, is denied
  whatever Codex then does with it, as [under Copilot](#the-model-guard) (`zz/model/swc/New.yaml`
  included, and, while the model directory cannot be listed, every edit whose path has two or more
  parts); a shell word and the
  folder a command runs in are judged as the shell reads them instead. The agent is told to use the
  SWCraft tools instead — `find`, `read` and `change_set` — whose changes you approve in
  [the review tab](#review-in-vs-code), or to word the command another way;
- **denies** every edit of the guard's own files — `swcraft.yaml`, `.mcp.json` and the `.codex/`,
  `.claude/`, `.github/hooks/`, `.kiro/hooks/`, `.kiro/settings/` and `.kiro/agents/` folders, and
  the `.github` and `.kiro` folders that hold some of them, so `rm -rf .github` is denied too — and
  every shell command that names one of them other
  than to read it with a command that runs nothing and writes nothing the
  assistant names: `cat`, `head`, `tail`, `wc`, `less`, `more`, `file`, `stat`, `ls` or `grep`, which
  write nothing, or `swcraft` with a command that only reads or checks and writes nothing
  (`check-model`, `diff`, `impact`, `list`, `metrics`, `query`, `resource-report`) and no output
  option, which for `metrics` and `resource-report` includes `--report`: it writes a file, so it is no
  reading command. Such a
  command still reads when started through `command`, `env`, `exec`, `builtin`, `nice`, `nohup`,
  `stdbuf` or `timeout`, each named by its bare name: a command named by a path, such as
  `notes/cat` or `cat` by its absolute path, may be any program and is no reading one. `env` setting a variable
  first makes it no reading one, and a command holding `xargs` anywhere has no reading command at
  all — `ls swcraft.yaml | xargs rm` hands the name on — and neither does a command that first
  defines a function or an alias, or runs `source`, `.`, `eval`,
  `hash`, `enable` or `unalias` — after `cat() { rm "$@"; }` the word `cat` runs `rm`. A
  link counts by its name too: a `.codex/hooks.json` that is a link to a file elsewhere is still
  the hook, and so is the file a link directly inside `.codex`, `.claude` or `.github/hooks` points
  to, whatever name a command gives it.
  So `cat swcraft.yaml` and `swcraft check-model --project
  swcraft.yaml` are fine. `swcraft validate --project swcraft.yaml` and `swcraft generate arxml`,
  `diagrams` or `needs` with `--project swcraft.yaml` are fine too, as long as every output
  `swcraft.yaml` names — `outputs.arxml`, the previous ARXML kept beside it as
  `<name>_old.arxml`, `outputs.needs`, `dist_root` and `views_root` — lies outside the model
  directory and the guard's own files: `validate` is the gate, so the assistant must be able to
  run it, and it cannot change where the outputs go. When one of them points at one of the guard's
  files, the build is denied with a reason naming that output, and you correct the
  manifest yourself. Such a build may carry `--warnings-as-errors`, `--component` with a component
  name and `--arxml-version`; any other option, such as `--output` or `--all-components`, keeps it
  denied. `sed`, `git`, `rg`, `cp`, `tee` or `>` onto them are not fine, even to read,
  because each has an option that writes a file or runs a program. They are what the guards read:
  an assistant that could edit them could switch a guard off — its own, or another client's, since
  Codex could otherwise write Claude Code's settings and Claude Code Codex's hook file, so both
  guards protect the same set. The set is wider than the guards need: an assistant's edit of
  `.claude/commands/`, `.claude/agents/`, a skill or `.mcp.json` is denied too, and you make those
  edits by hand. The cost is that Codex cannot raise
  the project's pinned SWCraft release or edit the manifest for you — you make those edits
  yourself, outside Codex — nor run `git diff swcraft.yaml`; it reads the file with `cat`;
- **denies**, where your model directory sits in a folder of its own inside the project — `arch` for
  a model directory `arch/model` — an edit of that folder itself and a shell command naming it other
  than to read it, with the same reading commands: `rm -rf arch`, `mv arch x` and `rm -rf ar*` are
  denied, `ls arch` is fine, and so is a path beside the model inside it, such as `rm -rf
  arch/other`. Removing the folder would remove the model with it. Nothing changes when the model
  directory sits directly in the project folder;
- **allows** every other edit and command — `cat dist/model.arxml`, say — and every other tool, SWCraft's own included. Where the project's
  `swcraft.yaml` is there but cannot be read or is not valid, the guard cannot tell where the model
  is, so it denies every edit and shell command, an edit or a `cat` of `swcraft.yaml` included.
  Codex does not touch a broken `swcraft.yaml`: fix it by hand in the editor, and Codex works
  again from the next command.

The project is the nearest folder, from the one the Codex session runs in upwards, that holds both
a `swcraft.yaml` and `.codex/hooks.json`. **The guard runs only when the project is a git
repository**: Codex finds a project's `.codex/` folder from the repository's root, so in a folder
that is no git repository it loads no project hook at all, even when started in the project folder
itself — `/hooks` lists no *SWCraft model guard* there. `git init` in the project folder makes it
one; **Onboard Project** says so when it onboards a folder that is none. In a repository, a session
started in a subfolder of the project is guarded too. The guard errs towards
denying: a word that merely equals the model directory's name (`echo model`) is denied, and so is
`ls *` run in the folder that directly holds the model directory, a `#` comment naming it, or a `${`
with no closing brace, even inside quotes (`grep '${' src`). So is a `${…}` holding a blank
and a quote, such as `cat ${x:-a "b c"}`, whose words the guard cannot rebuild, and a reading
command naming `swcraft.yaml` in a command that holds a word followed by `()`, even quoted
(`grep '()' swcraft.yaml`) or `xargs` anywhere, even as text (`grep xargs swcraft.yaml`). A
wildcard or brace word is also judged as the name it spells, so a
model directory whose name holds `[`, `*` or `{` is guarded by that name too. In the
project folder itself, a command other than the reading ones above with a wildcard that could match
`swcraft.yaml` or the model directory's name is denied too — `git add *`, `du -sh *`,
`find . -name '*.yaml'` — and on Windows so is a word that opens with one letter and a colon not
followed by a slash, such as `sed 's:a:b:'`, which reads there as a place on a drive. A wildcard
matches a name starting with a dot only when it starts with a dot itself, as bash matches: `*` never
names `.codex`, `.c*` does. A pattern group such as `!(x)` or `@(a|b)`, which bash's `extglob`
option makes a wildcard, is read as one that matches any name, even where the option is off or the
group is quoted. A command other than the reading ones above that names `.github`, `.kiro` or a
folder holding the model directory is denied even when it writes nothing — `git add .github`,
`git diff .kiro`, `find .github -name x`, `cd .github && ls`, `chmod -R 755 .kiro`, `git add arch`.
Codex then words the command another way.

**Trust it once.** Codex runs a project's hook only after you trust the project's `.codex/` folder
and the hook itself: in a project that is a git repository, start Codex in the project, type
`/hooks`, find *SWCraft model guard* and trust it. A hook file that changes is skipped until you
trust it again, so do that after
**Onboard Project** has replaced it. The extension cannot see whether Codex trusted the hook, so
**Check Editor Tooling** reports nothing about it; `/hooks` is where to look.

Codex's own words for its hooks are *"a useful guardrail, not a complete enforcement boundary"*,
and this one is no more. What it does not reach:

- **A command that reaches the model without naming it.** A script or program that opens the files
  (`python tool.py`), a variable or a substitution (`$DIR/x`, `$(…)`, a backtick's output joined
  to a word, `${x#…}`), a tool run over the whole project (`rg pattern`,
  `find . -name Engine.yaml -delete`, `git add -A`), a path put together while the command runs. Of a `${…}` only the word after its
  `-`, `=`, `+` or `?` is read, and of a backtick only the words inside it.
- **The project folder itself, and every folder above it.** A command naming one — `rm -rf .`, the
  project's full path, `rm -rf ..` — is not judged as naming a folder that holds the model or the
  guard's files: denying every command that names `.` would deny `git add .`.
- **A command that rewrites the guard's own files without naming them.** `swcraft init` run
  over the project, renaming a folder above it, or a `swcraft` command run on another project's
  `swcraft.yaml` whose build outputs point into `.codex/`.
- **What a `sed -f` script writes.** The guard reads the files a `sed` script written on the
  command line writes, never those of a script kept in a file.
- **A folder the command moves into first.** In `cd notes && cat ../model/x` the words are judged
  from the folder the command starts in.
- **A working folder Codex does not show the guard.** A command Codex runs in another folder is
  judged from that folder only when Codex hands the guard the folder with the command; whether it
  always does is not yet confirmed, and when it does not, a command run inside the model directory
  that names only paths below it is not reached.
- **A hook declared anywhere else.** The guard finds the project by `.codex/hooks.json`; the same
  hook declared in `.codex/config.toml` or in your own `$HOME/.codex/hooks.json` runs a guard that
  finds no project and allows everything.
- **Windows.** Codex runs PowerShell there; the guard reads a backslash as a separator too, but no
  other PowerShell syntax.
- **A link reached from outside the project.** A wildcard's real matches are read only inside the
  project, so a wildcard under another folder that matches a link into the model directory is
  judged by its names alone; and of the `.codex`, `.claude` and `.github/hooks` folders only the
  entries directly inside them are read for links.
- **A function or alias the shell already has.** One defined by a profile (`bash -lc` reads the
  login one), by `BASH_ENV` or exported earlier can make a reading command's name run another
  command; the guard sees only the command's own text.
- **A command other than `xargs` that passes on what a reading command prints.** `parallel`, a
  `while read` loop over a variable, or a variable holding `xargs` can turn a name `ls` printed
  into an argument of another command; the guard sees only `xargs` written out.
- **A shell set to match hidden names** (`shopt -s dotglob`, in the command or a profile). The
  guard reads a wildcard as bash does by default, so there `*` also names `.codex`, which the guard
  does not see.
- **`~` and `$HOME`.** A path written that way is taken literally, never expanded.
- **An edit wrapped in a heredoc.** Codex accepts one; the guard cannot read it and denies it, even
  outside the model — a needless denial, not a miss.
- **Another server's file tools.** A tool of another assistant server that writes files is not
  judged: the guard does not know its arguments.
- **Input typed into a command already running**, which Codex does not show the hook again.
- **A folder or a hook that is not trusted.** Codex then runs no hook at all, and the guard cannot
  say it was skipped.
- **A project that is no git repository.** Codex loads no project hook there, so nothing is
  guarded and nothing says so; `git init` in the project folder is the remedy.
- **A guard that cannot run.** The hook runs the `swcraft` your login shell finds: Codex starts the
  hook through your login shell, so its path is the one your login shell's profile sets — a `fish`
  universal `fish_user_paths`, say — and not the one the terminal you started Codex from was given.
  A release from
  before the guard knew Codex rejects `--host codex`, and Codex then **blocks every shell command
  and edit**, with that release's usage message as the reason. Where no `swcraft` is found, the
  hook cannot start, and Codex **runs the tool anyway**. Put this release's `swcraft` on your login
  shell's path.

The reviewed path is the SWCraft tools and the review tab; the guard keeps an assistant from
stepping round them by accident, not on purpose. Codex's hook file changes nothing for Copilot:
Copilot's hook runs the same command and gets the same answers as before.

The guard is half of what keeps Codex on the reviewed path; the other half is what Codex is told.
It reads the project's `AGENTS.md`, which tells it to use the SWCraft tools and leave the model's
files alone, and by default it reads only the first 32 KiB of that file — SWCraft's is larger, and
the part past the cut holds the review's rules. The `.codex/config.toml` that **Onboard Project**
writes raises the limit with `project_doc_max_bytes = 131072`, so Codex reads the whole file with
room to spare — seen with Codex 0.167 on the command line; that the Codex extension's own build
honours the line too has not been checked yet; a `.codex/config.toml` of your own needs that line too ([Codex and Claude Code in VS
Code](#codex-and-claude-code-in-vs-code)). What it does not reach: an `AGENTS.md` of your own in a
folder above the project may draw on the same allowance, and a Codex that does not trust the
project reads none of its `.codex/config.toml`, this line included.

### The model guard under Claude Code { #the-model-guard-under-claude-code }

<!-- purpose: What the model guard decides under Claude Code, before each file, search and shell tool -->

Every rule of the guard under Claude Code,
and everything it does not reach, is on [The model guard's rules](model-guard.md#under-claude-code).

The project's `.claude/settings.json` asks Claude Code to run `swcraft guard --host claude` before
each of its file, search and shell tools — `Read`, `Grep`, `Glob`, `Edit`, `Write`, `MultiEdit`,
`NotebookEdit`, `Bash`, `Monitor` and `PowerShell`. It joins the two guards above:

- **denies** a read whose file is in your model directory and a search whose folder is the model
  directory, inside it or above it, by Copilot's rules ([The model guard](#the-model-guard));
- **denies** an edit, a write or a notebook edit of a file in the model directory, naming the file,
  and one whose file it cannot judge or that names none;
- **denies** a read, a search, an edit or a write whose path or pattern holds the model directory's
  path anywhere, starts with its last folders or ends like a model file, whatever Claude Code then
  does with it, as [under Copilot](#the-model-guard) — `zz/model/swc/New.yaml` and
  `elsewhere/swc/Engine.yaml` included — with the same refusals of another folder named like the
  model directory and of a file ending like a model file, and the same lockout while the model
  directory cannot be listed; a shell word is judged as the shell reads it instead;
- **denies** a shell command by Codex's rules, word for word the same judgment
  ([The model guard under Codex](#the-model-guard-under-codex)), judged from the folder Claude Code
  says the command runs in — a `Bash` command, and a `Monitor`, which runs its command in the same
  shell (a `Monitor` that watches a web socket instead has no command to judge, and is denied). On
  Windows, where Claude Code's `Bash` is Git Bash, a word that opens with one letter between
  slashes, such as `/c/…` — also once braces are expanded, as in `{/c/…,}` — is also read as a path
  on that drive, and denied when either reading reaches the model;
- **denies** every edit of the guard's own files — the same set Codex's guard protects:
  `swcraft.yaml`, `.mcp.json`, the `.codex/`, `.claude/`, `.github/hooks/`, `.kiro/hooks/`,
  `.kiro/settings/` and `.kiro/agents/` folders and the `.github` and `.kiro` folders holding some of
  them — and a shell command naming one other than to read it, with the same reading commands as
  under Codex. So the assistant cannot edit `.claude/commands/`, a skill, `.mcp.json`, Kiro's server
  settings or Kiro's agents either: make those edits by hand. A folder holding your model directory
  inside the project, such as `arch` for `arch/model`, is protected the same way, as under Codex;
- **denies** `PowerShell` whatever it runs: the guard reads bash, not PowerShell, and a command it
  cannot read is denied. On Windows, let Claude Code use its `Bash` tool in a SWCraft project;
- **denies** a path into the review's own folder — the `reviews` folder in your SWCraft folder,
  where the review tab writes its answers: a read, an edit or a write there, a search whose folder
  is that folder, inside it or above it, and a shell word naming it or a folder above it, such as
  your home folder written out in full (the disk's top folder, `/`, excepted). A search above it
  includes one over your whole home folder or the whole disk (`/`), so a `Grep` or `Glob` started
  there is denied too, even when it looks for something else;
- **denies** a change to your SWCraft folder itself — `$HOME/.swcraft` (on Windows, `SWCraft` in
  your local application data folder), where the `swcraft` the guard runs is installed: an edit or
  a write of a file there, and a shell command that writes, removes or moves anything there or a
  folder holding it, such as `rm -rf` of your home folder written out in full (the disk's top
  folder, `/`, excepted). Reading it is allowed — `cat` of a file under `current` — except the
  `reviews` folder above. So the assistant cannot update or reinstall SWCraft: you do that;
- **allows** everything else — every other file, every other tool, SWCraft's own included. Where
  the project's `swcraft.yaml` is there but cannot be read or is not valid, it denies every judged
  tool except a saved answer — a `Read`, an edit or a `cat` of `swcraft.yaml` included. Claude Code
  does not touch a broken `swcraft.yaml`: fix it by hand in the editor, and Claude Code works again
  from the next call.

A path that opens with `~` in a read, search, edit or write tool is judged both ways — as one in
your home folder, `$HOME`, and as one in a folder named `~`, a search's folder and its pattern in
every combination of the two — and denied when any reading reaches
the model, the guard's files, the review's folder or your SWCraft folder: the review's folder or the
project's model written from `~` is denied, a notes file in your home folder written that way is
allowed. A path naming another user's home, such as `~someone`, is denied: write it in full. When your
home folder's path holds a search pattern's character — `{`, `}`, `(`, `)`, `[`, `]`, `*`, `?`, `@`,
`+`, `!` or `|`, such as a user folder named `John (Admin)` — the guard cannot tell that path from a
pattern, so every path and pattern opening with `~` is denied there: write it in full. In a `Bash` command
the shell expands `~` itself, which the guard does not follow (below).

The project is the nearest folder, from the one the command runs in upwards, that holds a
`swcraft.yaml` and `.claude/settings.json` — and also the folder the Claude Code session was
started in, when it holds them: a `cd` out of the project, into another folder Claude Code was
given, or into a project nested inside it still leaves the session's own project guarded, and a
call is judged against both. When **Onboard Project** kept a `settings.json` of your own, put the
guard's entry into it. A `.claude/settings.local.json` holding the entry also marks the project,
but Claude Code reads that file at the root it uses for the session, which may be the git
repository's rather than the project's, so that has been checked only for a project folder Claude
Code was started in. The guard needs no git repository. A denial looks like this in the chat (captured from Claude
Code on 2026-11-08, when it was asked to edit a model file directly):

```text
PreToolUse:Edit hook error: SWCraft: in this project the model directory (`model`) is changed through the SWCraft tools, whose changes the panel reviews; this edit writes `model/hardware/PARTITION0.yaml` there. Use the SWCraft MCP tool `change_set` instead.
```

The assistant reported the denial, did not try again, and the model file was unchanged.

What it does not reach — the Codex list first, as it applies here: a command that reaches the model
without naming it (a script, a variable, a substitution, a tool run over the whole project), a
command naming the project folder itself or a folder above it (`rm -rf .`), `~` or `$HOME`
written in a shell command, a function or alias a shell profile defines, what a `sed -f` script
writes, a link reached from outside the project, a folder an earlier `cd` in the same command moves
into, and on Windows a Git Bash folder other than a drive letter. Then:

- **The review's folder reached any other way.** A shell command writing it with `~`, `$HOME` or
  any variable, a script that opens the files, a path put together while a command runs: a `cat` of
  the pending proposals or a `printf` of an answer into that folder, written that way, is allowed —
  and an answer written there approves the change. The guard stops an accidental reach, not a
  deliberate one. What the review rests on is Claude Code's own confinement: it asks you before it writes
  outside the project folder. Started with `--dangerously-skip-permissions`, or with a permission
  rule that allows such writes, it no longer asks — so do neither in a SWCraft project.
- **Your SWCraft folder reached any other way**, the same ways — `rm -rf $HOME/.swcraft/current`
  written with `$HOME` is allowed — and a `swcraft` installed somewhere other than your SWCraft
  folder (a Python environment of your own, a system folder), which the guard does not protect.
- **A session started in a subfolder of the project.** Claude Code then reads none of the project's
  `.claude/settings.json`: no guard, none of its settings, and the server is not approved by it.
  It still finds `.mcp.json`, and the server then answers that it cannot find the project. Start
  Claude Code in the project folder.
- **A session started outside the project**, and a command run outside the project when Claude
  Code does not name the project as the folder the session started in; a folder you have not
  trusted (Claude Code then runs no project hook), hooks switched off with `/hooks`, or `disableAllHooks` set in a settings file
  outside the project.
- **A hook declared only in your own `$HOME/.claude/settings.json`.** It runs a guard that finds no
  project, and allows everything.
- **Another server's file tools**, a tool a later Claude Code adds, and Claude Code's tools that
  run other tools or code — its JavaScript tool, its workflow and scheduling tools — which the
  guard judges only through the calls Claude Code hands it. Whether an edit made through the
  JavaScript tool reaches the guard has not been seen: in release 2.1.319 that tool is a setting
  you turn on yourself, offered only in an interactive terminal session on its larger models — not
  in the VS Code chat and not to a scripted run — so it could not be tried. `Grep` and `Glob` were not
  among the tools Claude Code 2.1.319 offered — a search went through `Bash` — and `MultiEdit` was
  not either; the guard judges all three, should a release bring them back.
- **A subagent's calls** only as far as Claude Code hands them to the hook; in the release tried, a
  subagent's `Read` reached it.
- **A guard that cannot run.** Where no `swcraft` is found, the hook cannot start, and Claude Code
  **runs the tool anyway**: the guard does not block when it is missing. A release of `swcraft`
  from before the guard knew Claude Code rejects `--host claude` with a usage message and exit code
  2, and Claude Code then **blocks every file, search and shell tool** with that message as the
  reason. Which `swcraft` the hook finds — the one on the path VS Code was started with, or the
  one your login shell's profile sets — has not been told apart yet; keep this release's `swcraft`
  first on both, and see [which `swcraft` the chats
  run](#codex-and-claude-code-in-vs-code).

### Codex and Claude Code in VS Code { #codex-and-claude-code-in-vs-code }

<!-- purpose: Using Codex or Claude Code in VS Code on a SWCraft project: files, one-time steps, limits -->

Codex (OpenAI's Codex extension) and Claude Code (Anthropic's Claude Code extension) work with a
SWCraft project the way Copilot does: they ask SWCraft's tools, and a change they propose opens the
same [SWCraft review tab](#review-in-vs-code). Install the client's own extension from VS Code's
Extensions view and sign in to it as its maker asks; SWCraft's extension is needed too, for the
review tab. Then run **Onboard Project** (or `swcraft init --integration moon --client …`): it
writes the chosen client's server line with `SWCRAFT_REVIEW` set to `panel`, so the client's
changes wait for you in the SWCraft review tab.

#### The files Onboard Project writes

**Onboard Project** ([Turning a folder into a project](#turning-a-folder-into-a-project)) and
`swcraft init --integration moon` write these files for the clients you choose — each client's own,
beside its guard's hook file (Codex's `.codex/hooks.json`). Choose Codex, Claude Code or both in
Onboard Project's list, or give `swcraft init` `--client codex`, `--client claude` or both; with no
choice only Copilot's files are written, and none of these.

| File | For | What it does |
|---|---|---|
| `.mcp.json` | Claude Code | starts SWCraft's server as `swcraft mcp-serve`, the project's `swcraft.yaml` next to it, with `SWCRAFT_REVIEW` set to `panel` |
| `.claude/settings.json` | Claude Code | the model guard's hook ([under Claude Code](#the-model-guard-under-claude-code)); `enabledMcpjsonServers` approving the `swcraft` server of `.mcp.json`; and `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` set to `0`, so a call waiting for your review stays in the chat's foreground |
| `.codex/config.toml` | Codex | the same server line, with `tool_timeout_sec = 86400` (Codex waits a day for the review), `default_tools_approval_mode = "approve"`, and `project_doc_max_bytes = 131072`, so Codex reads all of `AGENTS.md` |
| `AGENTS.md` | Codex, and Claude Code through `CLAUDE.md` | the assistant profile's instructions, opened by a few lines for these two clients: their own file and shell tools stay off the model, the guard, and the guard's files of every client |
| `CLAUDE.md` | Claude Code | one line that has Claude Code read `AGENTS.md`, so both clients read one text |

`swcraft` is found on your path, as in every server line SWCraft gives, and `swcraft.yaml` is named
relative to the folder the client starts the server in — which is why these clients are started
in the project folder (below). `default_tools_approval_mode = "approve"` lets Codex call SWCraft's
tools without asking you first: every change is staged and waits for you in the review tab, and a
read changes nothing, so a question in the chat before each call would add a click and no
decision. To be asked anyway, set it to `"prompt"` in `.codex/config.toml`, and delete the file's
first line (below) so that **Onboard Project** keeps your change.

**Your own file is kept.** Each of them is written where the project has none, and replaced
where it is SWCraft's own — `.mcp.json` and `.claude/settings.json` when they hold nothing but
what SWCraft writes, an earlier release's values included, and the other three while their first line still says they were written by
**Onboard Project**. Delete that line and the file is yours. A file that is yours — one you wrote,
one with anything added, one that cannot be read — is left as it is, and the onboarding output names
it; add SWCraft's part to it by hand, from the table above (the server line is the one under
[Another chat client](#another-chat-client), with `swcraft.yaml` relative instead of absolute).
Nothing is written through a link, and nothing outside the project: no file under `$HOME`, and no
trust decision, which stays yours in each client.

#### Once per client

**Codex.** Open the project folder in VS Code (a git repository, as [the guard
under Codex](#the-model-guard-under-codex) needs) and open the Codex chat. Trust the project when
Codex asks. Codex then lists the project's hook as new under **Review hooks**: choose **SWCraft
model guard** and **Allow selected**. After **Onboard Project** replaces the hook file, Codex skips
it until you trust it again the same way.

**Claude Code.** Open the project folder and open the Claude Code chat. Trust the folder when Claude
Code asks: until you do, it reads none of the project's settings — no guard, no server approval and
no foreground setting. If it still asks whether to use the project's `swcraft` server, approve it.

**Which model.** The Claude Code extension's model picker writes your choice into
`$HOME/.claude/settings.json`, so a model picked in one chat becomes the default for every Claude
Code session on the machine, the command line included. Pick it back, or edit that file, when you
are done.

#### Asking and changing

A question is answered through SWCraft's tools, and a change is proposed through `change_set`: the
SWCraft review tab opens and you answer it there — never in the chat. While the tab waits, Codex
says in the chat that SWCraft is awaiting review and keeps waiting for up to a day; Claude Code
keeps the call running in the chat's foreground, up to its own limit of about 28 hours. Both read
sensibly as busy, not failed.

**The same files reach a terminal.** `codex` or `claude` started in a terminal in the project
folder read the same `.codex/config.toml` or `.mcp.json`, so their proposals also wait for the
review tab of a VS Code window open on the project — and, with none open, wait until the client
gives up, then end abandoned with your files untouched. To review elsewhere for yourself alone:

- **Claude Code:** add a server of the same name at Claude Code's local scope, which wins over the
  project's file (a user-scope one does not): `claude mcp add --scope local …` with the same
  command and `SWCRAFT_REVIEW` set to `elicit`, which asks in Claude Code's own chat — only an
  interactive terminal session can answer it.
- **Codex:** start it with `-c mcp_servers.swcraft.env.SWCRAFT_REVIEW=stage-only`, which wins over
  the project's file for that run and keeps its other settings. Codex does not answer `elicit`, so
  `stage-only` is the choice: the work is done and nothing is written.

**A long tool description.** Claude Code shortens a tool's description to its first 4 096
characters. Every SWCraft tool's description fits, and SWCraft's own checks keep it so; the import
rules the import tool's description once carried are in [What the plan
decides](how-to/import-a-component.md#what-the-plan-decides).

#### Which `swcraft` the chats run

Both clients start the server as a bare `swcraft`, looked up on the path VS Code was started with,
not on the path of a terminal you open later — seen for Codex; for Claude Code it is inferred, as
its extension runs in the same VS Code, and was not logged. A VS Code started from a shell with another path, or
with an older `swcraft` first on it, gives the chats the wrong one: an older release refuses the
project's pinned release, and the chat says only that SWCraft's tools are unavailable. SWCraft's
extension warns when the `swcraft` the chats would find is not the one it uses. The reliable check:
quit VS Code and start it again from the Dock or from Finder (on macOS this reads your login shell's
path), and see that the warning does not come back. On Windows and Linux the check is the same —
start VS Code so that its path has this release's `swcraft` first, and see that the warning does
not come back — but it was tried on macOS only. `swcraft --version` in VS Code's own terminal
is only a hint: that terminal's shell reads your profile again, so it can show the right release
while the path the chats inherited still holds the old one.

The guard hooks look `swcraft` up too, and an older `swcraft` there blocks every file and shell
tool (under [Codex](#the-model-guard-under-codex), and under [Claude
Code](#the-model-guard-under-claude-code)); SWCraft's warning does not see the path your login
shell's profile sets, so put this release's `swcraft` first there as well. The warning appears
only when a client file names a bare `swcraft` — SWCraft's own `.mcp.json` or `.codex/config.toml`,
or one of yours with a `swcraft` server.

Codex reads servers from your own `$HOME/.codex/config.toml` too. A `[mcp_servers.swcraft]` table
left there from an earlier hand configuration was seen to keep the project's server from starting
when it pointed at a project that no longer existed: look for one and remove it.

A Codex `app-server` left running keeps the `swcraft` it found when it started. After you change
which `swcraft` comes first on your path, quit that process (or restart the computer): starting VS
Code again does not reach a Codex process that outlived the editor, and its chats go on running the
old `swcraft` until it ends.

**Copilot and Claude Code in one project — not yet checked.** VS Code always reads a `.mcp.json`
at the root of the project folder, so when Claude Code is chosen, Copilot lists the project's
`swcraft` server beside the one SWCraft's extension registers. The two count as servers of one
name, and with VS Code's default for that (`chat.mcp.collisionBehavior`, `"disable"`) one of them is
turned off. By VS Code's own code (release 1.145.2) it is likely the extension's that is turned
off, so Copilot may run the project's server instead: the bare `swcraft` from the path VS Code was
started with, not the one SWCraft's extension uses — and whether that entry works for Copilot at
all, VS Code perhaps not reading its `${CLAUDE_PROJECT_DIR:-.}`, has not been checked either. None
of this has been observed yet; it is why choosing Copilot with Claude Code shows the warning under
[Turning a folder into a project](#turning-a-folder-into-a-project). Choosing Codex instead of
Claude Code writes no `.mcp.json`, and in a project where Copilot is not used the collision does
not arise.

#### Start in the project folder

The server line names `swcraft.yaml` relative to where the server starts, and both clients start it
in the folder the session starts in. Open the project folder itself in VS Code. Started anywhere
else, every SWCraft tool answers in the chat:

```text
No SWCraft project here: swcraft.yaml was not found in the folder the server was started in; start the client in the project folder.
```

From Claude Code the manifest is named `./swcraft.yaml`. A subfolder of the project is such a
place: Codex then answers this; Claude Code answers it too, and runs no guard and none of the
project's settings ([under Claude Code](#the-model-guard-under-claude-code)). A subfolder that holds
a `swcraft.yaml` of its own is a project of its own, and the server opens it without a word. A
`swcraft.yaml` that is there and cannot be used is named the same way, with what is wrong and the
remedy — *The SWCraft project cannot be opened: swcraft.yaml is not valid YAML (line 2, column 1);
correct the manifest, then call the tool again.*, or the field that is invalid — never with the
values it holds.

Claude Code's server log shows an `[ERROR]` "Server stderr" line, *Failed to validate request*,
when its server starts: Claude Code asks a question SWCraft's server does not know before it
connects, and then connects as usual. That line is harmless.

#### What was tried

On 2026-11-08, once, on macOS, in VS Code 1.145.2 with SWCraft's extension: **Codex** — OpenAI's
extension 26.1021.22027, which runs its own Codex 0.164, on gpt-6.1-sol — answered a question from
the model,
proposed a change that opened the review tab and was committed when approved there, and was
denied by the guard when asked to edit a model file directly, the file unchanged; **Claude Code** —
Anthropic's extension 2.1.319 — answered a question on Opus and proposed a change on Haiku, which
was committed when approved in the review tab. Claude Code's guard was not called in VS Code that
day; the same day, outside VS Code, Claude Code 2.1.319 on the command line answered a question,
committed a change approved through the review, and was denied an `Edit` of a model file by the
guard, as shown above. Also outside VS Code, Codex CLI 0.167.0 on gpt-6-luna, on a project set up
by this release, answered a question, committed a change approved through the review, and was
denied by the guard a shell command that wrote a model file, the file unchanged — a check of the
command-line Codex, not of the 0.164 the extension runs. Where the server starts was measured on the command line, Codex 0.167 and
Claude Code 2.1.319, from the project folder and from a subfolder. What was not tried: Windows and
Linux, a **Reject** through either client, a wait longer than a few minutes inside VS Code, other
models and releases, and on Windows how Claude Code finds the `swcraft` that `.mcp.json` names.

### Another chat client { #another-chat-client }

<!-- purpose: Connecting a chat client that is not VS Code to SWCraft's assistant server, and its review modes -->

Codex and Claude Code opened in VS Code on an onboarded project need none of this section: the
project's own files configure them ([Codex and Claude Code in VS
Code](#codex-and-claude-code-in-vs-code)), and so do Kiro's editor and `kiro-cli` ([In
Kiro](kiro.md#kiro)). Any other client, and Codex or Claude Code on a project
whose files you keep yourself, is configured by hand, wherever that client keeps its servers.
There is one line to give it — the command, its arguments and the one environment variable
that tells the server a human is there to review what it proposes, `SWCRAFT_REVIEW`:

```json
{
  "command": "swcraft",
  "args": ["mcp-serve", "--project", "<absolute path of the project>/swcraft.yaml"],
  "env": { "SWCRAFT_REVIEW": "panel" }
}
```

`swcraft` is found on your path, so this line is the same on every machine; the manifest path
is absolute because the client decides what directory the server starts in. With
`SWCRAFT_REVIEW` set to `panel` that
client's proposals are reviewed in [the same SWCraft review tab](#review-in-vs-code) as Copilot's,
so VS Code has to be open on the project with the SWCraft extension while it works — and the
client's assistant has to work confined to the project folder, for the reason that section ends
with. `panel` is the value to give. The server also takes `elicit`, which puts the question in the
client's own chat instead, but only a client that can ask a server's questions there answers it;
with any other client every proposal is abandoned unanswered. Leave the variable out and the
server starts fail-closed — it will do the work and write nothing, which is the symptom
[The assistant](assistant.md) explains.

The server waits for your answer as long as it takes, and a client may give up on a tool call
after a limit of its own. For Codex the stanza goes into its `config.toml` with a limit of one
day; Codex documents no maximum and no unlimited value, so a wait longer than a day ends there on
Codex's side:

```toml
[mcp_servers.swcraft]
command = "swcraft"
args = ["mcp-serve", "--project", "<absolute path of the project>/swcraft.yaml"]
env = { SWCRAFT_REVIEW = "panel" }
tool_timeout_sec = 86400
```

The first time Codex asks whether to let the SWCraft server run one of its tools, choose **Always
allow** or **Allow for this session**. A one-time **Allow** makes the call after your answer stop
at a second prompt.

Run Codex in its default sandbox, which lets it write only inside the project folder, and never
with full access: an assistant that may write anywhere in your home can answer the review itself.
Inside the project folder, the model directory included, it is [the model guard under
Codex](#the-model-guard-under-codex) that keeps Codex's own edits and commands off the model —
trust it once with `/hooks`.

Claude Code takes the same server line. By default it moves a tool call that is still running
after two minutes into the background; the answer then reaches the assistant later, as a task
notification, and is lost if you leave Claude Code's session before you answer. To keep the call
in the foreground instead, where Claude Code shows SWCraft's line saying the proposal is waiting
for your answer, add this to the `env` of a Claude Code settings file — `$HOME/.claude/settings.json`
for you in every project, or the project's `.claude/settings.local.json` for you in this project
only:

```json
{
  "env": { "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0" }
}
```

The setting is Claude Code's own, and it covers every server that Claude Code uses, not only
SWCraft's: none of their long calls moves to the background any more. Claude Code offers no way to
set it for one server alone. Nothing else needs setting: Claude Code's own limit on one tool call
is about 28 hours unless you lowered it, and the line SWCraft sends every minute while it waits
keeps Claude Code from ending the call as idle.

Copilot in VS Code needs nothing of this: it waits for your answer as long as it takes, a machine
going to sleep included. `swcraft agent` in a terminal has no limit of its own either.

Each of these waits was tried once, on 2026-11-02, with the answer given after 30 minutes: Copilot
in VS Code (with the machine asleep for part of the wait), the Codex command line with the stanza
above, and the Claude Code command line both ways. Codex and Claude Code inside VS Code were tried
on 2026-11-08 with the project's own files and a wait of a few minutes at most ([what was
tried](#codex-and-claude-code-in-vs-code)). What was not tried: a long wait inside VS Code with
either, a wait longer than 30 minutes with the machine awake, a **Reject** through each client, a
client that reconnects during the wait, and how reliably Codex keeps waiting and what that costs —
its assistant looks at the call about once a minute while it waits.

## When something is missing { #when-something-is-missing }

<!-- purpose: When something in the editor is missing or wrong: Check Editor Tooling and what it reports -->

**Check Editor Tooling** (`swcraft.checkEditorTooling`), in the `…` at the top of the SWCraft
view, is the command to reach for
when something is already wrong. It reports which of the packaged extensions this window
actually has, whether the bundled schemas shipped, and six things about SWCraft itself — and
it states inside the report what it could not check, rather than implying a clean report means
everything is fine.

If the extension warns when it starts that its navigation is off — Go to Definition, the hover,
Find All References, the Outline, Go to Symbol, the line above an element's `name:` line and the
links in descriptions and reports — its reference table is missing or damaged: reinstall the
extension. **Check Editor Tooling** does
not look at that file, so that warning is the only sign.

The six are worth knowing, because each has a different repair:

| What it says | What to do |
|---|---|
| the installation is not there | **Install Toolchain**, with the archive |
| it is there and `swcraft --version` will not start | **Update Toolchain** — something the installation runs on is damaged or gone. An installation from a current archive carries its own Python, so reinstalling puts it back; one from an older archive was built over the machine's own Python, and breaks when that Python moves or goes away |
| it runs, and printed no version this could read | **Update Toolchain** — the first line it did print is quoted back to you; a CLI that answers `--version` with a usage block or with nothing is an installation to rebuild, not a version to compare |
| it runs, and its version is not this extension's | install the archive of the version you want; both numbers are named |
| a bare `swcraft` in this window resolves **nowhere** | on macOS and Linux this is what a perfectly healthy installation says until you act: the installer prints one `export PATH="…/current/bin:$PATH"` line and edits no shell file, so add it to your shell profile — and then quit VS Code completely and start it again, because a new window is not enough. On Windows it means the application was launched before the installer edited the user `PATH` |
| a bare `swcraft` in this window resolves **somewhere else** | a warning, not a fault: the extension runs the installation by its full path, while a terminal here runs whatever it finds first |

The last two are the ones nobody guesses, and both are about `PATH` rather than about SWCraft.
What the check reads is the `PATH` **VS Code itself was started with**, which is fixed when the
application launches: neither a new window nor *Reload Window* changes it. So an environment
activated in the integrated terminal is precisely what this check *cannot* see — that shell's
environment changes and the extension host's does not — and what it *can* see is another
`swcraft`, or another environment's `bin`, sitting ahead of the installation in the `PATH` the
application was launched with. Either way the buttons are unaffected: they run the installation
by its full path.

Beside the six, in a window whose project holds a `swcraft.yaml`, it reports two Copilot rows. A
project set up for Codex or Claude Code alone — their files present and none of Copilot's — gets
both as *not checked*, never as missing. Otherwise it reports the **Copilot agent
profile**: *missing* — counted as a problem, even after you answered the notice above with *Not
for this workspace*; run **Onboard Project**, or accept the notice; *differs from this release's*
— a warning, not counted, because you may have edited the profile on purpose, and **Onboard
Project** replaces it; or this release's.

And it reports the **Copilot model guard**, `.github/hooks/swcraft.json`: *missing* — a warning,
not counted, saying the model is not protected from Copilot's file tools, with **Onboard Project**
as the repair; *differs from this release's* — a warning, **Onboard Project** replaces it; or this
release's. Whatever the file's state, the row is a warning too when a bare `swcraft` in this window
resolves nowhere or is not this extension's version, because the guard runs that `swcraft`. When
it resolves nowhere, or is a release from before the guard existed, the warning says that Copilot
can call no tool in this project — SWCraft's included — until this release's `swcraft` is on the
path; it says so conditionally when the version cannot be read or your hook file differs from
this release's, and not at all when there is no hook file, since then nothing runs. It stays a
warning, not counted, because the panel, the CLI and SWCraft's server outside Copilot keep
working. The row cannot see whether Copilot actually runs the hook.

It ends with the sentence this whole page keeps coming back to: a healthy pack is not
evidence about your model. No editor diagnostic from any of these extensions says anything
about the architecture or the generated ARXML. `swcraft validate` is the gate.

## The guide, without leaving the editor { #the-guide-without-leaving-the-editor }

<!-- purpose: Opening this guide inside VS Code, beside your model, from your own installation -->

**Open Guide** (`swcraft.openGuide`), in the `…` at the top of the SWCraft view, serves this
guide from your own install and shows it inside VS Code, in an editor tab beside your model. The
tab's toolbar can move the page to your own browser when you want the full window. It is
`swcraft docs` with the browser-opening left to VS Code, which is what makes it work in a Remote
SSH, WSL or Codespaces window: the pages are served on the machine SWCraft is installed on, and
VS Code forwards the address so the tab in front of you shows it. When VS Code cannot show the
guide in a tab, it opens in your default browser instead.

Three things follow from serving it rather than opening the files directly, and all three
are the reason the command exists: search and navigation work, the pages describe the release
you actually have installed, and nothing needs a network. The first Open Guide starts the server
and every one after it reuses the same one; it stops by itself when the window closes.

**Open Guide** needs no project — a folder does not have to be open. Neither do three items of
the tree and the other entry of the `…` menu. The three items are **Install Toolchain**
(`swcraft.installToolchain`) and **Update Toolchain** (`swcraft.updateToolchain`), which write to
the one installation this machine has, and **Onboard Project** (`swcraft.onboardProject`), which
asks you for the folder to scaffold; the menu entry is **Check Editor Tooling**
(`swcraft.checkEditorTooling`), which reports on the host rather than on a model. Nothing greys
the other items out — clicking one with no folder open says so, with **Learn more** (below), and
does nothing else.

If your install carries no guide, Open Guide says so, names the folder it looked in and the
release archive to unpack, and puts the whole message in the **SWCraft Guide** output channel.
A SWCraft checkout that `swcraft.executable` points at carries no built guide of its own unless
its release artifacts were built in it; `SWCRAFT_GUIDE_SITE`, set to a rendered guide in the
environment or in a `.env` file in the open folder, makes it serve that one (which `.env` files
are read: [Getting started](getting-started.md#validate-the-gate)).

**Learn more**, on two refusals, opens the section of this guide that says what to do:

- a window with no folder open, or a folder with no `swcraft.yaml` at its root — it opens
  [Turning a folder into a project](#turning-a-folder-into-a-project);
- a command refused because the project's `swcraft.yaml` pins another SWCraft release, on that
  command's failure notification — it opens [Upgrading](upgrading.md#the-procedure), *The
  procedure*. A read-only command that only warns about the pin and answers anyway shows no
  button.

No other notification carries one. When SWCraft itself cannot be started the guide cannot be
served either, so no **Learn more** is offered: the run's failure points to the **SWCraft** output
channel, and the messages of the toolchain pickers and of **Onboard Project** name **Check Editor
Tooling** and **Install Toolchain**. Any other failed run points to the **SWCraft** output
channel too, where the command's own message is.

**One guide tab per window.** A check's name in **Problems**, **Learn more** and **Open Guide**
open the guide the same way: the guide is started when it is not running, and the page opens in
the window's guide tab — a guide tab already open in the window is moved to the new page and
brought to the front, and a tab opens only when there is none. Three cases still give another
tab: a guide tab you moved to another window, a second guide tab you opened yourself (the first
one found is reused, so the two stay two), and the fallback to your default browser, which opens
a browser tab each time. When the guide cannot be served, nothing opens and the error Open Guide
would show says why.

The guide stops with the window. A guide tab still open when the window reloads — VS Code puts
it back — shows a page that cannot be reached and does not recover by itself. The next guide
link starts the guide again on an address the system picks anew, so it opens a new tab rather
than reusing the old one; close the old one.

These links have been tried in one desktop window on macOS. A Remote SSH, WSL or Codespaces
window, VS Code running in a web browser (vscode.dev, Codespaces in a browser, `code serve-web`),
and a link clicked while another VS Code window is in front have not been measured. In VS Code
running in a web browser, a check's link is expected to open the guide once per run: a second click
on that check's name — in the same entry or in another entry of that check — may do nothing until
the next **Check Model** or **Build** makes the links afresh. Where VS Code is slow to make a link,
the check's name is plain text in that run; a link it has not made after ten seconds is given up
with a line in the **SWCraft** output channel, and the next run asks for it again.

## The Resources view { #the-resources-view }

<!-- purpose: The Resources view: memory and CPU budgets in the editor, with the report beside them -->

**Model → Resource Report** (`swcraft.resourceReport`) runs `swcraft resource-report` on the open
project. One press does three things: it fills the **Resources** view, below the panel and the
Trace Links view, it opens that view, and it opens the report in a tab
([below](#the-report-tab)). It answers whenever you press it, with or without a Build. The `↻` in
the view's title bar updates the view only, and a green **Build** fills the view as well, without
opening it and without a report tab ([below](#out-of-date-a-failed-run-and-the-log)).

The tab is the report to read; the view is for finding your way, a click on a row taking you to the
file behind it.
The view stays collapsed until you use it; before the first run it holds one sentence naming both
ways and a **Resource Report** button. After **Resource Report** it opens by itself:

```text
RESOURCES
▾ Cores
  ▸ ✔ CORE0      12.00% of 70.00% · headroom 58.00%
▾ Memories
  ▾ ✔ RAM        14336 of 24576 bytes · headroom 10240
      ExampleSWC              8192 bytes
      BrakeLogger             4096 bytes
      SpeedFilter             2048 bytes
```

**Cores**, **Memories** and **Topology** are its sections, in that order. A section with nothing
in it is not shown, and **Topology** appears only when the report names a reference between the
software and the hardware that does not resolve — the report's `TOPOLOGY` section
([The command line](cli.md#the-reports-topology-section)). A model that declares no core and no
memory reads one row, *The model declares no core and no memory.*

Each core and memory row reads its use, its limit and the headroom to that limit: a core's in
percent, a memory's in bytes, its limit and headroom in whole bytes
([below](#the-report-tab)). The limit is the budget — a
core's threshold, a memory's size times its threshold. Its icon is its status: a green tick within
budget, a red cross over budget, and a yellow warning for *unknown*, a `strict` core or memory one of
whose consumers declares no budget, so its figure leaves that load out. The tooltip adds the
enforcement, the headroom to the whole core or the memory's whole size, and how many consumers it
has and how many of them declare nothing.

Expand a row to see who uses it:

- its consumers — a core's tasks, a memory's components — largest first, each with its amount, and
  a task with the figures its share is computed from, `12.00% (wcet 1200us / period 0.01s)`. One
  that declares nothing reads `not declared`, with a yellow warning. The consumers that declare `0`
  are folded into one last row, `3 consumers declare 0 bytes`, so a memory every component lists
  stays readable;
- its findings, one row each, red for an error and blue for an `info` finding of an `advisory`
  resource. The missing-budget findings are folded into one row, `2 consumers declare no budget`,
  because the consumers above already name them.

A click opens the file behind the row, as one preview tab with the focus left in the view. A core
or a memory opens its hardware file at its `threshold` line, else its `size`; a task opens at its
`wcet`, a component at the entry of its `memory:` budget that names the memory. When the key is not
there — a consumer that declares nothing, or a budget written in the `{…}` form on one line — the
file opens at its first line. A finding opens the element it names. The folded `0` row and the
**Topology** rows open nothing. A row whose element is no longer in the saved model is a warning
that says to refresh.

### The report tab { #the-report-tab }

<!-- purpose: The resource report tab: where its file is written and what it shows -->

A press of **Resource Report** writes the report as `resource-report/resource-report.md` in the
folder your manifest names as `dist_root` (`dist/` in a scaffolded project) and opens it in the
Markdown preview, as **Build** does with its build report, with the focus on the tab. It writes
nothing into the model. It says what the command line says: how many resources and findings there
are, any unresolved reference between the software and the hardware, then each core and memory with
its status, its use against its limit, its physical headroom, a table of who uses it, and its
findings.

**A core's tasks by partition.** A core's table names each task's partition, and a second table
under it gives each partition's share of the core, largest first:

```text
| Partition  | Share of the core |
|------------|-------------------|
| PARTITION0 | 9.00%             |
| PARTITION1 | 3.00%             |
```

The shares add up to the core's use. They are sums and nothing more: a partition has no budget of
its own, so a subtotal carries no status, no threshold, no headroom and no verdict on the
partition. A partition no task names is in neither table, and a task that declares no budget counts
as zero, as it does in the core's total. A memory's table has no partition column, and a core with
no task reads *No consumer.*, which is what a model without partitions shows, because a task reaches
a core only through a partition. The view has no partition column; this is the tab's.

**A memory's budget and headroom read in whole bytes, rounded down**, in three tabs — this one, the
build report and the [Change Report](change-report.md) — and in the view. A memory of 262144 bytes
with a threshold of 0.8 reads a budget of `209715` bytes, where the exact figure is `209715.2`. Only
the reading changes: whether a memory is within its budget is still decided on the exact figure.
The exact figures stay in the terminal's text output, in the assistant's answers and in every
finding or verdict sentence, so a finding beside the whole figure can name the fraction.

**What the tab does not follow.** Only a press of **Resource Report** rewrites the file and redraws
an open tab. **Refresh** and the run after a green **Build** update the view and leave the tab as it
is, and a **Build** removes the file along with the rest of `dist/`, so a tab left open then shows a
report whose file is gone. The view is the current one, with its *out of date* mark; press **Resource
Report** for a new tab. A press is not held back while a **Build** runs, so the Build's first step
can remove the file that press wrote, as it can a Change Report's.

**When no tab opens.** A project pinned to another SWCraft release fills the view and gets no tab:
the report is not written, and the **SWCraft** output channel says why. A report that cannot be
written does the same. A `swcraft` too old to write the report fails the press, the view saying
`the last run failed`, while **Refresh**, which asks for no file, still fills it.

### Out of date, a failed run, and the log { #out-of-date-a-failed-run-and-the-log }

<!-- purpose: When the Resources view is out of date or a run failed, and where its log is -->

**Resource Report** and **Refresh** (`↻` in the view's title bar) run the report whenever you press
them. **Resource Report** fills the view, opens it and opens the report tab; **Refresh** updates the
view only and writes no file. A press during a report's own run is refused with *a run is in
progress; try again when it ends*. A press during the refresh after a green **Build** waits for it:
**Refresh** then opens the view, and **Resource Report** runs the report once more, because that
refresh wrote no report for a tab to open. One run starts by itself: a green **Build**, one whose six steps all passed, warnings or not, runs the
report once after it has finished and its build report has opened. The view fills or updates
whether it is open or collapsed and loses its *out of date* mark, and no report tab opens: the one
tab a green **Build** opens is its build report. That run moves no focus: a view you left collapsed stays collapsed. The status bar
shows the run while it goes, and the **Resource Report** item then reads `reported` with the time,
like any other run, with no yellow dot.

Nothing else runs the report. A **Build** that fails, at any step, leaves the view as it was: the
last report stays, with its mark if a change set one, an empty view stays empty, and nothing reads
`failed`. **Check Model** runs none, whether it passes or not. A Build run in a terminal or by CI
refreshes nothing. When **Build** ends while a report is in progress,
or while **Install Toolchain** or **Update Toolchain** is running, or after the folder it ran in
stopped being the window's first, the run is skipped without a word and the view keeps what it shows
and its mark. While that run goes, **Install Toolchain** and **Update Toolchain** refuse, as they do
during the Build itself.

After a run, any change under the model folder or to
`swcraft.yaml` — a save, or a file changed by a checkout or a terminal while the window is open —
marks it out of date: the view's title reads `out of date — Refresh`, the panel's **Resource
Report** item gets the yellow dot and the text `out of date`, and the SWCraft icon's count includes
it ([above](#out-of-date-never-regenerated-behind-your-back)). The rows stay as they were and still
answer the figures of the last run. Press **Refresh** or **Resource Report** for new ones; a green
**Build** brings them too. The
item has no `not generated` state: what it follows is the view's report, not the file a press
writes, and before a run the view's button says what to press.

A change saved while a run is still going marks the report that run shows, as it marks the ARXML
and the diagrams during a Build. What the mark does not reach: the view's report is kept by this window
only, so a reload shows the button again rather than a report, even after a green **Build**; a change VS Code's file watcher
does not deliver marks nothing; and a change dated by a modification time older than the run's
start — a file copied in with its old time kept (`cp -p`, `rsync -t`, an unpacked archive), or one
on a file system that keeps times only to a second or two — leaves the report marked current, as it
does the ARXML and the diagrams ([above](#out-of-date-never-regenerated-behind-your-back)).

A run that fails — the project could not be read, or the extension could not read the report the
command printed, one written by another SWCraft release for instance — keeps the last report in the view and says
`the last run failed — see the SWCraft output channel` beside the title; with no report yet, one
row says the same. The **Resource Report** item reads `failed` with the time, as a failed **Build**
does. The run after a green **Build** can fail in the same way, with a warning; the **Build** stays
green. The **SWCraft** output channel holds the log only: the command line the run used, anything
the command printed on its error stream and one closing line — never the report. The text report is
`swcraft resource-report` in a terminal ([The command line](cli.md#the-report-file)).

The view and the tab are the command line's report, and like it, they are no verdict: `resource-report` exits 0 over
budget, and a red row is a reason to read the model, not a failed check. `swcraft validate` is the
gate.

## The Impact view { #the-impact-view }

<!-- purpose: The Impact view: what depends on an element, before you rename or delete it -->

**SWCraft: Impact of This Element** (`swcraft.impact`) answers the question you have before a
rename or a deletion — *what depends on this element?* — by running `swcraft impact` on the open
project and drawing its answer in the **Impact** view, below the Resources view. The view stays
collapsed until you use it; before the first question it holds one sentence and no button, because
the question is asked from an element's file. The panel's **Model** section has no *Impact* item.

### Asking { #asking }

<!-- purpose: Asking for the impact of an element from its file, the palette or the Impact view -->

Open the element's file — a file in one of the model's layer folders — and right-click →
**SWCraft: Impact of This Element**, or run it from the Command Palette, or press its button in the
Impact view's title bar. In `swcraft.yaml`, in a file under `dist/` or in any other file outside the
model's layers, neither the context menu nor the palette offers it; the title-bar button then says
to open an element file, and runs nothing. Whether a file is an element file is decided when you
switch to it: after you change `model_root` in `swcraft.yaml`, the menu and the palette follow the
new folder from the next editor you switch to. The command itself reads `swcraft.yaml` as it is.

What it asks about is read from the line the cursor is on (a right-click moves the cursor to the
line you click, except inside a selection, where the cursor stays where the selection put it):

- a line holding a reference — `interface: IExample` in a component, a `satisfies:` entry, a key
  under `memory:` — asks about the element it names, the one Go to Definition opens from that line.
  In a runnable's `reads`, `writes` or `calls` entry it is the element that holds the member: the
  interface, for a data element or an operation; the component itself, for its own `port:`;
- any other line — the `name:` line, a description, an empty line — asks about the file's own
  element.

The **impact** lens above an element's `name:` line asks about that element, wherever the cursor is.
A reference the model does not resolve is a message, and nothing runs.

What the reading does not reach: a name written where the model declares no reference — a component
named in a `description`, a connector end's `port:` (only its `swc:` is a reference), a task
entry's `runnable:` — asks about the file's own element. A port, a data element or an operation is
never asked about itself, only the element that holds it: the walk is over elements. A line in a
form the navigation does not read (a flow list, [above](#following-a-reference)) is read as Go to
Definition reads it. And the question is a name: when two layers declare it — a component and a
composition of one name — the view shows two roots, each with its own dependents.

The command line reads the saved files. If you have changed the element's `name:` line and not
saved, the command asks you to save first; a reference's target is asked about as it is saved, and
what an unsaved edit would add or remove is not counted.

### Reading the tree { #reading-the-tree }

<!-- purpose: Reading the Impact view's tree: who depends on the element, and how -->

After asking about `IExample` in the example project:

```text
IMPACT
▾ IExample                       interfaces · 6 dependents
  ▾ swc                                            2
      ExampleConsumerSWC                  via port rIn
      ExampleSWC                          via port pOut
  ▾ connectors                                     1
      Example_to_Consumer   via ports ExampleSWC.pOut, ExampleConsumerSWC.rIn
  ▾ compositions                                   1
      Root                    members ExampleSWC, ExampleConsumerSWC
  ▾ tasks                                          1
      Task_10ms                     runnable ExampleConsumerRun
  ▾ sequences                                      1
      task10ms_consumer_cycle           step: ExampleConsumerRun
```

The root is the element you asked about, with its layer and how many elements depend on it. Under
it is one node per layer that holds a dependent, in the model's order of layers, with its count;
under each, the dependents, each with how the walk reached it (`via port rIn`, `members ExampleSWC,
ExampleConsumerSWC`). The walk is the whole one, as `swcraft impact` prints it without `--depth`:
what depends on the element directly and what depends on those, so a dependent has no children of
its own — the tree already lists them. A layer of more than twenty dependents is collapsed, its
count still beside it. A name no layer declares reads *No layer declares 'Name'.*, and an element
nothing depends on *Nothing depends on it.* under its root. A layer this extension does not know —
a newer toolchain's — is listed last under the name the command line gives it, and its rows open
nothing.

A click opens the file at the element's `name:` line (`id:` in a requirement), as one preview tab
with the focus left in the view; the root opens the element you asked about. When the root is a
requirement, a dependent that names it opens at the line that names it; one reached through another
element opens at its `name:` line. A click never opens the line of the port the walk went through.
A dependent whose file cannot be found is a warning naming it.

To ask about a dependent, press the button at the end of its row, or choose **SWCraft: Impact of
This Element** in its right-click menu. A new question replaces the tree: there is no history and
no way back but asking again. While a question runs, another is refused with *a run is in
progress; try again when it ends.*

### An answer out of date, a failed run, the log { #an-answer-out-of-date-a-failed-run-the-log }

<!-- purpose: When the Impact view's answer is out of date or a run failed, and where its log is -->

The view never asks again by itself. After an answer, any change under the model folder or to
`swcraft.yaml` that VS Code's file watcher delivers — a save, a checkout or a terminal edit while
the window is open — marks it: the view's title reads `out of date — Refresh`, and the rows stay as
they were. **Refresh** (`↻` in the title bar) asks the same question again. No panel item gets a dot
for it and the SWCraft icon's count does not include it: the answer is not a file. A change saved
while a question runs marks the answer that run shows. What the mark does not reach is the
Resources view's limit ([above](#out-of-date-a-failed-run-and-the-log)): the answer is kept by this
window only, so a reload shows the welcome again; a change the file watcher does not deliver marks
nothing; and a file whose modification time is older than the run's start leaves the answer marked
current. When the window's first folder changes, the answer about the previous one is dropped and
the welcome is back.

A run that fails — the project could not be read, or the extension could not read the answer, one
written by another SWCraft release for instance — keeps the last tree and says `the last run failed
— see the SWCraft output channel` beside the title; with no tree yet, one row says the same. The
**SWCraft** output channel holds the log only: the command line the run used, anything the command
printed on its error stream and one closing line — never the answer. The text answer is
`swcraft impact` in a terminal ([The command line](cli.md#the-six-commands-that-only-look)).

Like the command line's answer, the view is no verdict: `swcraft impact` is read-only and exits 0
whatever it finds, and a long list is a reason to read the model, not a failed check. `swcraft
validate` is the gate.

## Importing requirements { #importing-requirements }

<!-- purpose: Importing requirements from the editor: the file, the mapping, the dry run and one click -->

**Model → Import Requirements** (`swcraft.importRequirements`) runs
`swcraft import requirements` on the open project. [Importing requirements](cli.md#importing-requirements)
is about the file, the mapping, what the import changes and its report; this section is about the
button. It never applies anything before you have read the dry run's report and clicked **Write**:

1. **Pick the file.** A file dialog opens at the workspace folder, showing `.json` files; you choose
   the team's `needs.json`. Cancelling it ends the command, and nothing runs.
2. **Pick the mapping.** A list asks how the team's needs map to SWCraft's: the mapping you used last
   (marked *last used*), **Choose another mapping file…**, which opens a dialog showing YAML files, and
   **SWCraft's own names (no mapping)**. The first time, only the last two are offered. Your choice is
   remembered at once in the setting `swcraft.requirementsImport.mapping`, in the workspace folder's
   settings (`.vscode/settings.json`, which your version control shows): a path relative to the project
   directory, or an absolute one for a file outside it. *SWCraft's own names* clears it. Only that
   file's value is read: one set in your user settings is not offered. A team can
   commit that file so that every architect imports with the same mapping. Escape ends the command,
   and nothing runs.
3. **The delivery's version.** A box asks for the version this delivery records in each
   requirement's `baseline`. It comes **filled with the version the file names** (its
   `current_version`): press Enter to record that one, or type another — the team's commit, say. When
   the file names no version, the box is empty and you must enter one; an empty answer is not
   accepted. Escape ends the command, and nothing runs.
4. **The dry run.** The item runs the import with `--dry-run`, which writes nothing:

   ```text
   $ swcraft import requirements --source=<path>/needs.json --mapping=<path>/team.needs-mapping.yaml --project swcraft.yaml --dry-run
   ```

   The version you kept as the file names it is not passed — the command takes the file's own, and
   the report's first line says `(from the file)`; a version you typed is passed as
   `--baseline=<your text>`.

   Its report opens first, every time, in the tab *Requirements Import Report*, which looks like the
   tab where you review an assistant's change set: the report on top, the buttons under it. The
   report is Markdown, like SWCraft's other reports: the command writes it as a Markdown file
   (`--report` with a `.md` path in a temporary folder of its own, outside your project, deleted as
   soon as the tab has read it), and the extension's lines stand as text under it. A run that
   writes no report, a refusal, shows the command's own output in a code block instead, and so does
   an older toolchain, which writes its plain-text report into that file. VS Code's built-in Markdown support renders it; if that is
   turned off, the tab says so in one line and shows the same report as plain text. The
   report is not saved anywhere, so closing the tab asks nothing. A line the command prints that
   starts with `Warning:` also stands in a yellow block above the report. A run the command refuses
   — a file that breaks the format, a mapping it refuses, an id of one of your architecture
   requirements — shows its `Error:` lines there, ending with *Nothing was written.*, with no buttons:
   the item reads `failed`, and a warning names it. Read it before you write: a delivery is the
   complete set, so a requirement the file no longer holds is in its *Removed* list.
5. **One click decides.** Under the report, whose count is its summary line, stand **Write** and
   **Cancel**. They stay there, in the tab, until you click one. You are asked every time, a dry run
   whose report says `No change.` included; applying that changes nothing. Only **Write** imports.
   **Cancel** closes the tab, as **Reject** does in a change set's review tab, and applies and
   records nothing; closing the tab is the same. Run the import again to apply it later.
6. **After writing.** The buttons are disabled while the import runs. The item runs the same
   command again without `--dry-run` — a second run over the model as it is by then, not a replay
   of the first — and its report replaces the dry run's in the same tab, with **Write** and
   **Cancel** gone. The item reads `imported 12:41`, and the tab offers **Check Model** beside the
   import's count, which runs only if you click it; the import does not check the model itself. If
   the import itself is refused or fails — the model or the file changed since the dry run, say —
   its `Error:` lines are in the report, ending with *The import did not complete. Check what it
   changed with Change Report or git status.*, the tab has no button, the item reads `failed`, and a
   warning names it. When the command line says the import was applied and only its report failed,
   the last line is *The import was applied, but its report could not be shown. Check what it
   changed with Change Report or git status.* instead.

**What the button does not do.** It reads no model file and has no import logic of its own: what
changes and what is refused is the command line's. Of the needs file it reads one key, the version
the box is filled with; whether the file is a valid delivery is the command line's to say, so a file
the box read a version from can still be refused by the dry run, and a file it could not read leaves
the box empty. It does not check the remembered mapping before the run: a file that is gone, or one
inside your model directory, is the command line's refusal in the dry run's report, and the next
import offers it again beside **Choose another mapping file…**. It does not compare the import with its dry run,
so a model or a file changed in between gives the import's own result, which its report shows. It
does not save your open editors: a requirement file you have open with unsaved edits shows VS Code's
own "changed on disk" question when you save it. It remembers the mapping between runs, never the
needs file, and it does not
wait for a **Build** or a **Check Model** started while its buttons wait: each runs at once, so a
**Build** running beside the import can read the requirements while the import is changing them.
Another **Import Requirements** started while the buttons wait takes the same tab, and the first
one's buttons then apply nothing. One started while an import's run is busy — its dry run, or the
import itself after **Write** — is refused with a notification, *SWCraft: Import Requirements is
already running in this window — wait for it to finish.*, before its file dialog opens, or when you
pick its file if that run started while the dialog was open: two runs of this import never write
at once. An **Import Component**, or a `swcraft import` run in a terminal, can still run beside it.

## Importing a component { #importing-a-component }

<!-- purpose: Importing a component from an ARXML file in the editor: the file, the dry run, then Write -->

**Model → Import Component** (`swcraft.importComponent`), directly under **Export Component**, runs
`swcraft import component` on the open project. [Exchanging with other tools](cli.md#exchanging-with-other-tools)
is about the file, what the import writes and refuses, and its report; this section is about the
button. It works as **Import Requirements** does, above: nothing is written before you have seen
the dry run's report and clicked **Write**.

1. **Pick the file.** A file dialog opens at the workspace folder, showing `.arxml` files; you
   choose one. Cancelling it ends the command, and nothing runs.
2. **The dry run.** The item runs the import with `--dry-run`, which writes nothing:

   ```text
   $ swcraft import component --source=<path>/SpeedSensor.arxml --project swcraft.yaml --dry-run
   ```

   Its report opens first, every time, in the tab *Component Import Report*, in the same form as
   the requirements import's tab: the line under the title names the run and the file, the report
   is Markdown headed by the file's name, and the buttons stand under it. The report is the one the
   command writes, never saved, and a `Warning:` line the command prints also stands in a yellow
   block above it.
3. **One click decides.** Under the report stand **Write** and **Cancel**, every time, a report
   that says the component is `unchanged` included. Only **Write** imports. **Cancel** closes the
   tab, as **Reject** does in a change set's review tab, and writes nothing; closing the tab is the
   same.
4. **After writing.** The buttons are disabled while the import runs. The item runs the same command
   again without `--dry-run` — a second run over the model as it is by then — and its report
   replaces the dry run's in the same tab, the two buttons gone. The item reads `imported 12:41`,
   and the tab offers **Check Model** beside *SpeedSensor.arxml was imported.*; it runs only if you
   click it. If the import itself is refused or fails, its `Error:` lines are in the report, ending
   with *The import did not complete. Check what it changed with Change Report or git status.*, the
   tab has no button, the item reads `failed`, and a warning names it. When the command line says
   the import was applied and only its report failed, the last line is *The import was applied, but
   its report could not be shown. Check what it changed with Change Report or git status.* instead.

**A component your project already has, which the file changes.** The dry run is then refused —
the command line's refusal says the component *already exists in the target project and diverges*
— and the command exits with status 3, the refusal an update resolves. The tab shows that refusal
and then *The project already has this component, and the file changes it. Show Update shows what
an update would replace and what it keeps.*, with **Show Update** and **Cancel** under it and
nothing beside them.

- **Show Update** runs the dry run again with `--update`, its buttons disabled meanwhile. Its report
  replaces the page: what the update would replace, as a preview of the differences, what it keeps
  from your project, and anything that would stop it. Under it stand **Update** and **Cancel**.
- **Update** runs the same command with `--update` and without `--dry-run`, and step 4 follows.
  **Cancel**, at either point, closes the tab and writes nothing.

`--update` is passed only on that path, after **Show Update** and **Update**: the item never adds it
to a first dry run, and it decides by the command's exit status, never by reading its text.

**Any other refused dry run** — a shared interface or data type that differs from yours, a
component your project holds as an external stub under another name (`SpeedSensor_ext0`), a line of the file the import cannot carry, a
`toolchain_version` that is not this release — shows the command's `Error:` lines as it groups
them, ending with *Nothing was written.*, and no button: the item reads `failed`, a warning names
it, and no update is offered, since an update would not resolve it. An update dry run that is
refused ends the same way.

**What the button does not do.** It reads no model file and has no import logic of its own: what is
written, kept and refused is the command line's. It never passes `--replace-access-lists`, so an
update keeps your runnables' read, write and call lists as the command does by default, and the
report says so; to take the file's lists, run the command yourself. A toolchain older than this
extension exits with status 1 for a changed component too, so no update is offered with it. It does
not compare the import with its dry run, nor the update's preview with the first dry run, so a
model or a file changed in between gives the import's own result, which its report shows. It does
not save your open editors: a component file you have open with unsaved edits shows VS Code's own
"changed on disk" question when you save it. It remembers no file between runs, it does not check
the model itself, and it does not wait for a **Build** or a **Check Model** started while its
buttons wait: each runs at once. Another **Import Component** started while the buttons wait takes
the same tab, and the first one's buttons then apply nothing. One started while an import's run is
busy — a dry run, the update's preview, or the import itself — is refused with a notification,
*SWCraft: Import Component is already running in this window — wait for it to finish.*, before its
file dialog opens, or when you pick its file if that run started while the dialog was open: two
runs of this import never write at once. An **Import Requirements**, or a `swcraft import` run in a
terminal, can still run beside it.

## Experiments { #experiments }

<!-- purpose: Running an allocation experiment from the panel, and opening its report -->

**Experiments → Allocation (CARMA)** (`swcraft.experimentAllocation`) runs
`swcraft experiment allocation` on the open project — the question of where a component could move
to another partition, and what the move would buy. [Experiments](experiments.md) is about the
question and the report; this section is about the button.

Without a setting it runs with the experiment's defaults. To ask your own question, write a
configuration file and name it in the setting `swcraft.experiments.allocation.config` — a path
relative to the project directory, usually in the workspace's settings so it travels with the
project; empty runs the defaults again, and a value that is not a path at all (a number, say) is
refused in the output channel without running anything. The item passes it as `--config`, and the
output channel shows the command it ran:

```text
$ swcraft experiment allocation --project swcraft.yaml --config=experiments/rebalance.yaml
```

The **SWCraft** output channel shows the run as the command prints it: the summary line and the
paths of the two files it wrote. On success the item reads `ran 12:40` and `report.md` opens in VS
Code's Markdown preview, where its tables render and the names linked to your model open their
files in one click. A refusal — a model with an error finding (a warning does not refuse it), a
key the configuration file does not know,
a name your model does not declare, an `output` outside the project — is the command's own `Error:`
line in the channel; the item reads `failed`, a warning names it, and nothing opens. Nothing is
previewed either when the run printed no report path, or when the report is gone by the time the
preview would open — a **Build** started meanwhile removes `dist/`, and the item does not wait for
one, nor a **Build** for it; the channel says so in one line.

The item does not watch your model: a report is not marked out of date when the model changes, and
saving a file runs nothing. Press it again for a new answer.

## Trace links { #trace-links }

<!-- purpose: Linking requirements to model elements in the Trace Links view -->

**Experiments → Trace Links** in the panel is where you link your requirements to the elements of
your model. It holds two nodes, **Labelling** and **Review**: you choose each one's documents there,
and the work happens in the **Trace Links** view below the panel, which shows the documents of the
mode you clicked last — its title line says which, *Labelling · SRS Thermal Protection*. [Experiments](experiments.md#trace-links-which-elements-could-a-requirement-be-linked-to)
is about the search behind it and its report; this section is about the panel.

### Labelling and Review { #labelling-and-review }

<!-- purpose: Choosing Labelling or Review for a document in the Trace Links view -->

- **Labelling** is for a document whose requirements are not linked yet: you link them by hand,
  and the search orders them so that the ones that teach it the most come first. Nothing is ticked
  for you, and an element shows without its band: `runnable:BrakeThermalProtection.EvaluateThermalProtection`.
- **Review** is for a document the search can already propose for, because enough of its
  requirements are linked: each element shows its band and a short reason, and **Mark strong** can
  tick the strong ones for you. A coloured dot before an element repeats its band: green *strong*,
  yellow *medium*, grey *weak*.

**Click Labelling or Review** to switch the Trace Links view to that mode and choose its documents:
the view opens, and a list opens with every document, the ones
with no link yet first, each with its count (*12 requirements · none linked*). Tick the ones you
want for that mode and press Enter; Esc changes nothing. Choosing again replaces that mode's
documents. A document is in one mode at a time: one ticked here leaves the other mode, and the list
marks it (*in Review*) before you choose. Requirements with no document can be labelled only
together with every document; ticked for Labelling without them, they are left out and a message
says so. The palette's *SWCraft: Trace Link Search…* asks for the mode and opens the same list.

The panel searches only when it must: when no proposals file is there yet, or when a chosen document
is not in the file in the mode you chose. Otherwise a choice only changes what is shown. **New
Search…** (the Trace Links view's `…` menu, or Trace Links's context menu in the panel) searches again for the documents both modes hold. The search
reads the configuration file the setting `swcraft.experiments.trace.config` names, as the allocation
experiment does (empty: the defaults), and the Labelling documents are passed for that run only — the
file is never rewritten, and the report records the choice as *from the command line*.

If the last search's proposals file still holds decisions you have not applied, a question comes
before a search:

- **Apply…** applies them (below) and then searches, as long as nothing is left unapplied;
- **Discard and Search** searches anyway and overwrites them;
- **Cancel** runs nothing.

If the command line refuses to overwrite the previous proposals file all the same — it reads that
file more strictly than the panel does, so a file edited by hand can be one it cannot read — the panel
asks once whether to discard it and search. And a choice that takes a document out of both modes
while it holds decisions not applied asks first, since they would be out of sight: **Apply…**,
**Discard Them** (that document's decisions only) or Cancel.

### Reading the tree { #reading-the-tree_1 }

<!-- purpose: Reading the Trace Links tree: documents, requirements, proposed elements and what is left -->

Each mode node in the panel counts what is left: *2 documents · 9 of 24 to go*. A requirement counts
as done when it is linked, when you ticked an element for it, or when you rejected every element
proposed. In the view, each document holds its requirements as `ID  Title`, marked *ticked*,
*linked*, *rejected* or *nothing found*, followed by the first words of the requirement's text. Hover
a requirement for its whole text, or **click it** to open its file in a preview tab; the next one you
click replaces it in the same tab, and the focus stays in the tree. The view never splits the editor:
everything it opens goes into one preview tab of the editor you are in.

Under a requirement:

- the **three** strongest proposals. They stay where they are: ticking or rejecting one brings no
  other in. In Review an element reads like `strong·port:BrakeController.pTemp  names pTemp`: its band and
  kind, highlighted, then its name and, greyed, the first of its reasons (*like SWR-BRK-001* — the linked requirements of the same document that
  link there, *similar to …*, *terms brake, temperature*); hover it for all of them;
- the elements you chose yourself and the links awaiting confirmation;
- **More candidates (N)**: the requirement's other proposals, unfolding in place as rows like the
  three above, with checkboxes; it opens by itself when one of them is decided. At its end, or under
  the requirement when there is nothing more, **Any element of the model…** opens a list of the
  model's elements, the ones the search did not propose — for a component the component itself or one
  of its ports and runnables.

A requirement that is **linked** already shows its links and nothing to decide. A link reads like
`linked·port:DiagnosticManager.pBrakeDiagnostics`, with a check.

**Click an element** to open its file at its line, in the same preview tab. The click
never changes a decision.

### Deciding { #deciding }

<!-- purpose: Ticking, rejecting and undoing proposed links in the Trace Links view -->

- **Tick** an element to accept it; untick to take it back.
- **Reject** one with the ✕ on its line or from its context menu; it shows *rejected*, and ↺ undoes it.
  Apply records a rejection, and the element is not proposed again while the requirement stays as it is.
- **Mark strong** (the view's title bar, while it shows Review) ticks every *strong* element of the
  Review documents nobody
  has decided yet. It is a preselection, nothing more: read them and untick any you do not accept.
  The decisions file records them as accepted from a preselection.
- **Clear** (the view's `…` menu) undoes every decision not yet applied, after asking.
- A link **awaiting confirmation** has a checkbox too: tick it once you have checked that the link
  still holds for the requirement's new version.

Every decision is saved into the proposals file within a moment, and nowhere else — not into your
model and not into the editor's own storage; the two lists of documents are a view preference of the
workspace. An element already applied or found stale shows that and cannot be changed. If the
proposals file changes on disk while the panel is open (a search or an Apply from the terminal), the
tree reloads; a change you made at that same moment is not saved, and a message says so. While the
proposals file has unsaved changes in an editor, the panel refuses to save over it. Trace Links itself
says what is waiting: *2 ticked, 1 rejected, not yet written*, and the Trace Links view starts with a
row **Apply 2 ticked links…** while anything waits.

### Applying { #applying }

<!-- purpose: Applying the decided links: the dry run's report, Write and Cancel, and what Write writes -->

**Apply…** (the row at the top of the Trace Links view, or the ✓ in its title bar) runs the command
line's dry run and opens its report in the tab *Trace Apply Report* — the same tab, with the same
buttons, as the requirements import's report: the report on top, **Write** and **Cancel** under it,
and no question in a dialog. The report says what Write would do: how many links it writes into
how many files, the rejections it records and the links you must add by hand.
**Write** writes exactly what the dry run showed: if anything changed in between, the command refuses
and writes nothing, and you run Apply again. Its report then replaces the dry run's in the same tab,
with the buttons gone, and the SWCraft output channel gets one line saying what was written.
**Cancel** closes the tab and writes nothing; closing the tab is the same. A dry run with nothing to
write says `No change.` and offers no **Write**; so does a dry run the command refuses, which shows
its `Error:` lines. The tab is not saved anywhere, and the next Apply report replaces it. While the
report waits for your click, the Trace Links view takes no new decision.

Apply adds only the `satisfies` lines to each file: every other line — your comments, your key order,
your quoting — stays as it was. A `satisfies` list written in a form it does not edit
(a list in brackets over several lines, a list written through a YAML alias, or any list of a file
starting with a byte-order mark, for example) is left alone, while the other lists of the same file
are still written: the dry run's report says how many links must be added by hand
and names each one. And if an edited file would not read
back as exactly what it held plus the new links, Apply writes nothing at all and says which file.

The view's `…` menu also has **Open Report** (the search's `report.md` in the Markdown
preview) and **Open Proposals File** (the JSON, in an ordinary editor).

## The full command list { #the-full-command-list }

<!-- purpose: Every SWCraft command in VS Code, with its Command Palette entry -->

| Command | Palette entry |
|---|---|
| `swcraft.build` | SWCraft: Build |
| `swcraft.checkModel` | SWCraft: Check Model |
| `swcraft.diagrams` | SWCraft: Diagrams |
| `swcraft.showInDiagram` | SWCraft: Show in Diagram (with an element file in the editor) |
| `swcraft.testTables` | SWCraft: Test Tables ([its page](test-tables.md)) |
| `swcraft.changeReport` | SWCraft: Change Report |
| `swcraft.impact` | SWCraft: Impact of This Element (with an element file in the editor) |
| `swcraft.resourceReport` | SWCraft: Resource Report |
| `swcraft.metrics` | SWCraft: Architecture Metrics |
| `swcraft.exportComponent` | SWCraft: Export Component |
| `swcraft.importComponent` | SWCraft: Import Component |
| `swcraft.importRequirements` | SWCraft: Import Requirements |
| `swcraft.experimentAllocation` | SWCraft: Allocation Experiment (CARMA) |
| `swcraft.experimentTrace` | SWCraft: Trace Link Search… |
| `swcraft.onboardProject` | SWCraft: Onboard Project |
| `swcraft.installToolchain` | SWCraft: Install Toolchain |
| `swcraft.updateToolchain` | SWCraft: Update Toolchain |
| `swcraft.checkEditorTooling` | SWCraft: Check Editor Tooling |
| `swcraft.openGuide` | SWCraft: Open Guide |

What each one does underneath is on [The command line](reference/cli.md); the panel items
are that surface with the arguments filled in. **Open Guide** and **Check Editor Tooling** are
entries in the `…` at the top of the SWCraft view rather than tree items; **Impact of This Element** is in the editor's context
menu and the Impact view's title bar ([above](#the-impact-view)), and **Show in Diagram** in the editor's
context menu and on the `diagram` link above an element's name, never in the panel, because it
needs an element and a cursor the panel does not have; every other command of the list is an item of the
tree. The Trace Links tree's own commands — *New Search…*,
*Choose Documents…*, *Other Element…*, *Mark Strong*, *Clear*, *Apply…*, *Open Report*, *Open
Proposals File*, *Accept (Tick)*, *Reject*, *Undo Reject*, *Open Element*, *Choose Another Element…*
and *Remove Choice* — are in the Trace Links view's title bar and menus or on the panel's Trace Links nodes, and not in the
palette, because each acts on the view or a node. So are the Resources view's *Refresh* — the same
run as **Resource Report**, which is in the palette, with no file and no tab — and *Open Element*,
a row's click, and the Impact view's *Refresh* and *Open Element*, a row's click.

## A note on what has been measured { #a-note-on-what-has-been-measured }

<!-- purpose: Which platforms the extension has been measured on -->

The extension's behaviour has been exercised on macOS. Windows has not been measured, so
treat the keyboard shortcuts and the paths above as the shapes rather than as tested facts
on that platform.
