<!-- draft: not yet reviewed by the product's author -->

# Getting started

<!-- purpose: Your first project: from an empty directory to a generated and checked ARXML file -->

This page goes from an empty directory to a generated, checked ARXML file. It takes five
element files and one command, and the whole thing fits in one sitting.

If you would rather do it inside VS Code, the extension ships a seven-step walkthrough that
covers the same ground with the buttons in place of the commands — **Help → Welcome**, then
*The first hour with SWCraft*. The two are interchangeable; this page is the version you can
follow over SSH, and [In VS Code](vscode.md) is where the editor's own surface is described.

## What you need { #what-you-need }

<!-- purpose: What a workstation needs before you install SWCraft, and what it does not need -->

- **No Python and no Node.js.** SWCraft brings its own of both. Its packages are chosen for one
  Python version, and the archive carries exactly that Python: the installer runs on it and builds
  the installation on it, so a Python you already have — of any version, or none at all — is
  neither used nor changed. The Node.js it carries is what the diagrams are laid out on. The same
  goes for the two VS Code extensions SWCraft works with: the archive brings them, so the
  editor needs no access to the VS Code Marketplace.
- **The archive.** One file — `swcraft-<version>-<platform>.zip` — holding everything: the
  three SWCraft packages, every third-party package they need, `swcraft-specimens` (see below), the
  VS Code extension and the installer that puts them in place. Your integrator hands it to you, or
  you download it from the [Releases page](https://github.com/swcraft-dev/swcraft/releases) of
  SWCraft's repository, where a released version's Linux, Windows and macOS archives sit with a
  `SHA256SUMS-<platform>` beside each. There is nothing else to download and nothing to fetch
  while it installs.

Nothing else. There is no server to stand up, no database, and no licence key. The AUTOSAR
XSD schemas are licensed and SWCraft ships none, which is the one thing that changes what a
check can tell you; [Validate](#validate-the-gate) says exactly what.

## Install { #install }

<!-- purpose: Installing SWCraft from its archive, on macOS, Linux or Windows, and checking it worked -->

Unpack the archive anywhere — a downloads folder is fine, nothing installs *into* it. On
**macOS**, clear the quarantine flag, `com.apple.quarantine`, on the unpacked folder first, once:
the macOS archive is not signed, so macOS marks what it unpacks as downloaded and refuses to start
the programs inside until the flag is gone.

```sh
xattr -dr com.apple.quarantine <unpacked folder>
```

Then run the one file inside it, `swcraft_install.cmd` on Windows and `./swcraft_install.sh`
everywhere else:

```text
swcraft_install.cmd     on Windows (double-clicking it from Explorer works too)
./swcraft_install.sh    everywhere else
```

Started with no arguments — a double-click from Explorer, or `.\swcraft_install.cmd` typed at a
PowerShell prompt, which runs it the same way — the window waits for a key press before it
closes, so you can read what the installer said, including a refusal, which is one sentence and
the only thing you need from it. Pass any flag and it does not wait; neither does a run inside an
already-open `cmd.exe` window. Set `SWCRAFT_NO_PAUSE=1` to switch the wait off for good, which is
what an unattended caller wants.

While it runs, the installer prints one numbered line per step and closes each with how long the
step took; a step's own sentence, such as the shell line below, is printed under its line. On
macOS it reads like this, with the step count depending on what the archive carries:

```text
preparing the installer ... done
Installing SWCraft <version> into <your home>/.swcraft
1/12 checking the wheels against this machine ... done, 0 s
2/12 unpacking Python ... done, 3 s
3/12 creating the Python environment ... done, 4 s
4/12 installing swcraft-agent and its dependencies ... done, 38 s
...
12/12 removing the installer's temporary Python ... done, 1 s
SWCraft <version> is installed in <your home>/.swcraft
```

The programs the steps start — Python's environment builder, `pip`, VS Code's `code` command — do
not write to the screen. Their output goes into `install.log` in the installation directory:
`%LOCALAPPDATA%\SWCraft\install.log` on Windows, `$HOME/.swcraft/install.log` elsewhere, or the
directory `--home` names. It holds the **last run only**: every install replaces it. It is also a
file that stays, so know what it can carry: a user name and password written into an `--index-url`
are masked in the installer's own lines, but whatever a program prints about what it was given is
written as that program printed it, and a password passed as part of a URL's query is not masked
at all. On macOS and Linux the installer creates the file readable by you alone; on Windows it is
protected like the rest of the directory it is in, which under `%LOCALAPPDATA%` is your own.

When a step fails, its line ends in `failed`, and the installer prints the log's last lines (up to
twenty) and the log's path before the one-sentence refusal, which is still the last line. A run
that succeeds prints the log's path only when a program said no to something that does not stop
the installation — an editor extension that `code` would not install — because what that program
said is in the log alone.

`--verbose` puts the programs' own output back on the screen as it scrolls by; the log is still
written, without that output. A package index that asks for a user name and password while
installing needs `--verbose` and a terminal: without it nobody could see the question, so the
installer gives the program no keyboard and the step fails at once, with the question in the log
rather than a window waiting on it. That is also what happens under VS Code.

`--uninstall` writes no log: it removes the directory a log would be kept in, and the one program
it starts, `code`, writes to the screen.

A first install that fails leaves something behind. One that fails before anything is unpacked —
at the check of the wheels — leaves an installation directory holding only `install.log`; `--uninstall` refuses
that directory as no installation, so delete it yourself. One that fails later may leave part of
an installation beside the log — an unpacked runtime, a `versions/<version>` directory — and
`--uninstall` removes it.

Then make SWCraft reachable, and this is where the two platforms differ.

On **Windows** the installer edits your user `PATH` for you. Open a **new** terminal — this is the
step people skip; a terminal that was already open still has the environment it started with.

If an anti-virus scanner watches your machine, ask for the installation directory,
`%LOCALAPPDATA%\SWCraft`, to be excluded from on-access scanning before you install: the
installation writes thousands of small files — Python, its packages and Node.js come to about
12 900 in the macOS archive — and a scanner that inspects each one as it is written has been
measured stretching a Windows install to 27 minutes. Without the exclusion, a step that unpacks a
runtime can also stop with `… could not be replaced (… Access is denied …) after 8 attempts over
7.85 s`: the installer has already waited for the scanner to let go of the files it just wrote, and
running the installer again is the answer.

On **macOS and Linux** it edits no shell file, deliberately: the shell zoo makes a silent edit
worse than a printed one. It prints one line instead, ending in the installation's `bin`
directory —

```sh
export PATH="$HOME/.swcraft/current/bin:$PATH"
```

— and you add that to your shell profile (`$HOME/.zshrc`, `$HOME/.bashrc`, `$HOME/.config/fish/config.fish`)
and open a new terminal. Until you do, `swcraft` is `command not found` and the full path
`$HOME/.swcraft/current/bin/swcraft` works. From the new terminal, `swcraft --version` names the
release you installed:

```sh
swcraft --version
```

That is the whole installation, and it is one per machine rather than one per project. It
lands under your user profile — `%LOCALAPPDATA%\SWCraft` on Windows, `$HOME/.swcraft` elsewhere —
and holds one directory per release with a `current` link pointing at the one on your path.
Nothing is written into an architecture project, and no project has an environment of its own
to create or to keep in step.

The installation puts a `node` beside `swcraft` in the folder on your path, so a Node.js you
already have and SWCraft's meet there. Which one a terminal finds differs: on Windows the installer
adds its folder at the *end* of your user `PATH`, so a Node.js already on it comes first; on macOS
and Linux the printed line puts it at the *front*, so once that line is in your profile SWCraft's
`node` comes first.

The installation also brings `swcraft-specimens`, a generator of synthetic architecture projects, as
a command beside `swcraft`. Its main use is a model of a given shape: `swcraft-specimens specimen
passport.json --out <dir>` reads a [model passport](reference/model-passport.md), the names-free
description `swcraft passport` writes, and generates a project of made-up components with that
shape, so a problem seen on a model that cannot be shared can be reproduced on one that can. Its
`swcraft.yaml` names the SWCraft release it was installed with. `swcraft-specimens generate small
--out <dir>` writes a project from a packaged preset instead, so you can try SWCraft's commands on
a model before your own exists; `swcraft-specimens --help` lists the presets and commands. It was checked
against this SWCraft release before publication, it lives in the same installation, and installing
a newer SWCraft replaces it with the one that release brings. There is nothing to download for it
separately.

The extension comes with the archive too: the installer hands the `.vsix` to VS Code when it
finds it, and says so when it does not. [In VS Code](vscode.md) is the rest of that story.

Which version *this* copy of the guide was built from is written on its own `version` page,
at `/version/` in the built site.

```sh
swcraft --help
```

### The installer's options { #the-installers-options }

<!-- purpose: The installer's options: another folder, no PATH edit, no editor, a package index, removal -->

The installer needs no option on a workstation. Each one below is for a case where a default is
wrong, and the wrapper hands every option to the installer unchanged:

| Option | What it is for |
|---|---|
| `--home DIR` | install into another folder. It must be an absolute path: the `current` link points into it |
| `--no-path` | leave `PATH` alone — for a caller that names the executable by its full path |
| `--no-vsix` | leave VS Code alone — for a build machine, which has no editor |
| `--source DIR` | install from another folder of packages instead of the archive's own |
| `--index-url URL` | install from a package index instead of the archive's own packages; not together with `--source`, and the archive's platform check is skipped, since the index answers for the machine it runs on |
| `--python PATH` | build the installation on this Python instead of the one the archive carries; one older than 3.11 is refused |
| `--version VERSION` | install under this version instead of the one the archive's files carry — for an archive put together by hand |
| `--uninstall` | remove the installation and the extension; on Windows the `PATH` entry too, and elsewhere it prints the line to remove from your shell profile. `--no-path` and `--no-vsix` apply to it |
| `--verbose` | show the programs' own output on the screen as well as writing `install.log` |

Running the same archive's installer again is a **repair**: it installs that release again in
place and deletes nothing. A newer archive's installer is an upgrade, and an older one's is the way
back — [Upgrading](upgrading.md) has both.

A build machine installs the same archive, with `--no-vsix`; [On a build
machine](architecture-repository.md#on-a-build-machine) has the rest.

## Create a project { #create-a-project }

<!-- purpose: Creating a project with the scaffold, and what each file and directory of it is for -->

```sh
swcraft init my-arch
cd my-arch
```

That writes one manifest, a `.gitignore`, the model's directories and four model files — fifteen
layer directories, seven of them under `data_types/`:

```text
my-arch/
  swcraft.yaml
  .gitignore          # keeps .env, where your local settings and keys go, out of the repository
  model/
    swc/  interfaces/  connectors/
    data_types/
      application/  implementation/  base/  mapping_sets/
      units/  compu_methods/  constraints/
    requirements/  tasks/  sequences/
    compositions/
      Root.yaml       # the top of the hierarchy, empty until you place something in it
    hardware/
      CPU.yaml        # the one processor, as a placeholder
      CORE0.yaml      # one core, advisory and uncapped until you give it a budget
      PARTITION0.yaml # the one partition on that core, without a safety level; a task names it
  .vscode/
    settings.json     # turns the editor's XML schema check off for .arxml, and only for .arxml,
                      # and keeps the spell checker out of model/, dist/ and views/
```

The spell checker is Code Spell Checker, where you have it installed: it would mark every name in the
model as an unknown word. The settings name the folders your `swcraft.yaml` declares, so with a
`swcraft.yaml` of your own already in the directory they follow its `model_root`, `dist_root` and
`views_root`. A `.vscode/settings.json` that is already there is kept as it is, so a project made
with an earlier release has no such key: add `"cSpell.ignorePaths": ["model/**", "dist/**",
"views/**"]` to it by hand, with your own folders. A `swcraft.yaml` already in the directory that
pins another SWCraft release stops `init` with the pin's sentence before it writes anything more.

There is no configuration file for the editor's chat, and that is not an omission: the SWCraft
extension tells VS Code how to start the assistant itself. [In VS Code](vscode.md) says what
another chat client needs instead.

`compositions/` and `hardware/` are the two layers `init` does not leave empty, and it is the
**pair** that makes a fresh project pass the model check. Every project declares exactly one
processor and exactly one composition marked as the top of the hierarchy, so a scaffold that
wrote neither would produce a project that does not load. All four files are placeholders. The
processor and the core are meant to be replaced by the part you are really building for — until
they are, the core budget is `advisory` and set to the whole core, so it reports rather than fails
and no task needs an execution budget yet. A task names the partition it runs in and takes its core
from it, so `PARTITION0.yaml` is what your first task names; it claims no safety level until you
give it one. `Root.yaml` starts out holding nothing: a composition
places components, and you have not declared any. You add each component to it as you go, and
[The model](model.md) says why. Where a component is placed also matters to a task: a task names a
component placed once by its type, and one occurrence of a type placed more than once by its path
below `Root`.

Three of the four are load-bearing from the first minute: delete `PARTITION0.yaml` and the model
check still passes until a task names it, delete `CORE0.yaml` and it does not — the partition sits
on it — delete `CPU.yaml` and it does not either — every project declares exactly one processor —
and delete `Root.yaml` and it does not, because there would be no top to the hierarchy.

The manifest is short, and every path in it is relative to the directory holding it:

```yaml
name: my-arch
schema_version: "1.0"
model_root: model            # where the model files live
dist_root: dist              # where the generated build outputs go
views_root: views            # where the generated diagram views go
outputs:
  arxml: dist/model.arxml
  needs: dist/needs.json
```

`schema_version` is the version of *this file's format*. The AUTOSAR release the generated
ARXML targets is a different field, `arxml_version`, and it defaults to `4.2.2` when you
leave it out.

Spell every key exactly. A key the manifest does not declare — `arxml_verison:` for
`arxml_version:` — is an error of the model check (`manifest.unknown-key`), on that key's line in
VS Code, naming the nearest declared key; a misspelt key would otherwise be dropped and its setting
never applied. The project still loads, so listing and querying keep working, but the model check,
`swcraft validate` and Build stop until you rename or delete the key. `swcraft generate` on its own
does not check the manifest.

There is a third version field you will meet, `toolchain_version`, and `swcraft init` on its own
does not write it. It is the SWCraft release the project is *built with*. Add it when you want
the project to say so:

```yaml
toolchain_version: "<the release you installed>"     # what `swcraft --version` printed
```

Every `swcraft` command then compares it with the toolchain it is running as. Ten of them
refuse a mismatch: the eight that write or gate — `generate`, `validate`, `check-model`,
`clean-outputs`, `import`, `experiment`, `change-report`, and `init` over a `swcraft.yaml` that is
already there — refuse before they write anything, and `mcp-serve` and
`agent` refuse before they hand over, because an assistant editing a project against the wrong
toolchain is the case the pin exists for. All ten name both versions. The read-only six — `diff`, `impact`,
`list`, `metrics`, `query`, `resource-report` — print the same sentence and answer anyway, and `metrics --report` and `resource-report --report` then write no file. A manifest without the field pins nothing, which is why it is optional and
why an older project keeps working. Raising it is the whole of an upgrade on the project's side —
[Upgrading](upgrading.md) is the rest. `swcraft init --integration moon` **does** write it, set
to the release that scaffolded the project, because a moon build caches on it.

## Write the first five files { #write-the-first-five-files }

<!-- purpose: Writing the first five files: a base type, a type, an interface, a component and a requirement -->

The model is plain YAML with one rule that governs everything else: **one element per file,
named after the element.** There is no index to keep in step and no file that lists the
others — the directory *is* the list. (Two names that differ only in case get two files; how,
is on [Working with the model](model.md#one-element-per-file).)

A component that does anything at all needs five files. Build them from the bottom up.

**A base type** — `model/data_types/base/float32_base.yaml`. It says how the platform stores a
value: its size in bits, its encoding, and the C type the generated code declares for it.

```yaml
name: float32_base
kind: base_type
size: 32
encoding: IEEE754
native_declaration: float
```

**A data type built on it** — `model/data_types/implementation/float32.yaml`. This is the type an
interface names. A base type never is: a data element, an argument, a record field and an array
element all name a value type (or an enumeration, a record or an array), and the value type names its
base. Keeping the two apart is what lets one storage format carry several value types, each
with a unit and a range of its own.

```yaml
name: float32
kind: value_type
base: float32_base
```

**An interface that carries it** — `model/interfaces/IVehicleSpeed.yaml`:

```yaml
name: IVehicleSpeed
kind: sender_receiver
data_elements:
  - name: speed
    implementation_type: float32
```

The key says which kind of type the element names: `implementation_type` for a type under
`data_types/implementation/`, as here, and `application_type` for one under
`data_types/application/` — exactly one of the two, because one name may be declared on both
sides and only you know which side you mean.

**A component that provides it** — `model/swc/sensing/SpeedSensor.yaml`. Note the extra
directory level: components are grouped, and the group is the directory name rather than a
key inside the file.

```yaml
name: SpeedSensor
type: application
description: Reads the wheel speed sensor and publishes the vehicle speed

ports:
  - name: pSpeed
    direction: provide
    interface: IVehicleSpeed
    description: The current vehicle speed

runnables:
  - name: SpeedSensor_Run
    events:
      - type: timing
        period: 0.01

handle_termination_and_restart: no_support
```

The last line says whether the component's behaviour can be stopped and restarted. AUTOSAR
requires the answer on every component, so SWCraft asks you to write it — `no_support` here,
because nothing in this sensor stops or restarts it; the [component reference](reference/model/swc.md)
lists the other two values.

**A requirement that asks for it** — `model/requirements/REQ_SPEED_001.yaml`. Requirements
are named by their `id`, not by a `name`:

```yaml
id: REQ_SPEED_001
title: The vehicle speed shall be published every 10 ms
description: The system shall publish the current vehicle speed at a 10 ms cycle.
req_type: req
version: 1
source: architecture
```

`version` is SWCraft's own change counter for the requirement, never a requirements tool's
revision, and `source` says who owns the file: `architecture`
for one you write here, `external` for one a requirements tool delivers. The requirement names
no component. The component names the requirement instead, with the version it was written
against — add these two lines at the end of `SpeedSensor.yaml`:

```yaml
satisfies:
  - REQ_SPEED_001@1
```

The last file and those two lines are not decoration. A requirement nothing satisfies is
reported as a warning: SWCraft holds that every requirement you write is realized by something
in the architecture, and it will tell you when one is not, without holding the file back. A
component that satisfies no requirement is no finding — not every element of an architecture
answers a requirement of its own — but the link is how the requirement is allocated. When the
requirement's `version` moves on, a link still naming `@1` is reported as needing confirmation,
until you have checked the component against the new text and written the new number. Every check
the product runs, what each one wants and which of them are warnings, is listed on
[The checks](reference/rules.md).

## Validate — the gate { #validate-the-gate }

<!-- purpose: Validating the first model, and reading the answer when a check fails -->

```sh
swcraft validate --project swcraft.yaml
```

This is the one command whose answer counts. It reads the whole model, generates the ARXML,
and checks that ARXML against the AUTOSAR XSD — or, when no licensed schema is configured on
the machine, structurally, and it says so rather than pretending:

```text
ARXML generated: <project>/dist/model.arxml (AUTOSAR 4.2.2)
Note: XSD validation is skipped (requires the licensed AUTOSAR_4-2-2.xsd schema).
Validation passed.
```

It exits 0 when the model is good and non-zero when it is not, which is what a build graph
and a merge check read.

To have the ARXML checked against the real schema, get the licensed AUTOSAR XSD for the version
your project generates — `AUTOSAR_4-2-2.xsd` or `AUTOSAR_4-3-0.xsd` — and point
`SWCRAFT_AUTOSAR_XSD_PATH` at it. The file name has to be that version's: a schema under any
other name, or a path that does not exist, is not used, and the run checks structurally and warns
you why. Set the variable in your environment, or put it in a `.env` file in the folder you run
`swcraft` from, or in any folder between that one and your project's folder, the one holding
`swcraft.yaml`. A `.env` above the project's folder is not read. Every `swcraft init`, with or
without `--integration moon`, leaves the project's `.gitignore` ignoring `.env`, so a path on your
machine stays out of the repository: it writes the file, or adds the one `.env` line to a
`.gitignore` you already have and changes nothing else in it.

Leave out the two `satisfies` lines and the run still passes, but it tells you what is missing
before it does:

```text
  WARNING: Requirement 'REQ_SPEED_001' is satisfied by no element: add `satisfies: [REQ_SPEED_001@1]` on the component, port, runnable, composition, interface, task or hardware element that realizes it.
0 error(s), 1 warning(s).
ARXML generated: <project>/dist/model.arxml (AUTOSAR 4.2.2)
Note: XSD validation is skipped (requires the licensed AUTOSAR_4-2-2.xsd schema).
Validation passed.
```

A warning is a gap to close, not a refusal: the file is generated and the exit code is 0. A
pipeline that must not let a known gap through runs `validate --warnings-as-errors`, and the
same run then stops before generating anything, exactly as an error does;
[Command line](cli.md#the-two-commands-that-grade) has the details.

Misspell the interface the port names instead — `IVehicleSped` — and you get the other outcome:

```text
  ERROR: SWC 'SpeedSensor' port 'pSpeed' references interface 'IVehicleSped' which is not declared in model/interfaces/.
1 error(s), 0 warning(s).
```

Two things about a failure are worth knowing on day one:

- **An error like this one stops the run before anything is generated**, so there is
  nothing new on disk, and the `dist/model.arxml` the last run wrote is still there, untouched —
  removing the previous outputs is a build's clean step rather than this command's.
- **A failure the *schema* check finds comes after generation**: by then the file has been
  written, so the refused one now sits at `dist/model.arxml` — the line above the failure names
  it — and the one it replaced at `dist/model_old.arxml`. Read the exit code and the message,
  never the directory listing.
- **The message names the element, not a line number in a generated file.** Findings are
  stated in the vocabulary you wrote, because that is where the fix goes.

## Look around { #look-around }

<!-- purpose: Listing the model and running the model check, as soon as the first validation passes -->

Two commands are worth running the moment the first check passes. Neither writes anything.

The first is a report rather than a check. `swcraft list` — like `diff`, `impact`, `query` and
`resource-report` — exits successfully whenever it managed to *read* the project, however
uninteresting or alarming its answer is, so a clean exit from it is never an answer about
whether the model is valid. Only a project it could not read at all makes it fail. `query`
has one exception, given in full on [Command line](cli.md): a question the model's
vocabulary cannot answer is not an answer either, and it fails.

```sh
swcraft list --project swcraft.yaml
```

```text
requirements: REQ_SPEED_001
compositions: Root
swc: SpeedSensor
interfaces: IVehicleSpeed
implementation_types: float32
base_types: float32_base
hardware: CORE0
hardware: CPU
```

`Root`, `CORE0` and `CPU` are the three elements `init` wrote for you. They are elements like
any other, and they appear here from the very first run — before you have added anything at
all, `list` prints exactly those three.

The second *is* a check: `swcraft check-model` answers the model half of the same question `validate`
answers, and writes nothing at all. It exits non-zero on a bad model exactly as `validate`
does. This is the one a build graph runs first, because `validate` would rewrite the very
ARXML the next build step is about to check:

```sh
swcraft check-model --project swcraft.yaml
```

## Generate the rest { #generate-the-rest }

<!-- purpose: Generating the diagrams and the Sphinx-Needs file after the ARXML -->

The ARXML is one of three outputs, and `validate` has already produced it. The other two are
the diagrams and your architecture requirements with their trace, as a Sphinx-Needs file:

```sh
swcraft generate diagrams --project swcraft.yaml
swcraft generate needs --project swcraft.yaml
```

The diagrams go into `views_root`, as drawings and pages that VS Code's **Diagrams** shows in a tab
of the editor and a browser opens from the file, and the
`needs.json` to the path the manifest declares under `outputs` — `dist/needs.json` in the manifest
above. [Diagrams](diagrams.md) is about
reading the first of them, and [Publishing the architecture to the requirements
team](cli.md#publishing-the-architecture-to-the-requirements-team) about what the second carries and
how the requirements team loads it.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next after the first project, by what you want to do -->

| If you want to | Read |
|---|---|
| see where all of this stands in an AUTOSAR project | [The big picture](big-picture.md) |
| understand the layers and how they refer to each other | [Working with the model](model.md) |
| find the panel, the diagrams and the hovers | [In VS Code](vscode.md) |
| ask an assistant to make the change for you | [The assistant](assistant.md) |
| know what every command and option does | [The command line](reference/cli.md) |
| look up a key you half-remember | [The model layers](reference/model/index.md) |
