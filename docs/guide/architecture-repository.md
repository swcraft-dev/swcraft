<!-- draft: not yet reviewed by the product's author -->

# An architecture repository

<!-- purpose: Setting up a repository with a build graph: moon, the scaffold, the build, a build machine -->

This page is for whoever sets up the repository a team keeps its architecture in: a project that
builds with one command, on a workstation and on a build machine alike. [Getting
started](getting-started.md) is the same ground for one person trying SWCraft out; this page adds
the build graph, an existing repository and the build machine.

The repository owns **no toolchain at all**. SWCraft is one installation per machine, reached
through `PATH` ([Install](getting-started.md#install)), and the project records only *which
release it is built with* — the `toolchain_version` pin in its `swcraft.yaml`. Every build task
runs the bare `swcraft` that installation provides.

| You get | You do not get |
|---|---|
| A YAML architecture model with a checked, reproducible local build | A CI/CD pipeline — see [On a build machine](#on-a-build-machine) |
| One command, `moon run :build`, that fails loudly on an invalid model | Any hosted service, package feed or credentials to set up |
| A VS Code workspace whose chat reaches the model, with nothing to configure | An automated rollout to other workstations |

## What the machine needs { #what-the-machine-needs }

<!-- purpose: What a machine needs before it builds an architecture repository: SWCraft and moon -->

- **SWCraft, installed from its archive** — [Install](getting-started.md#install). Nothing else
  of SWCraft's is needed: no Python, no Node.js and no checkout of SWCraft's sources.
- **moon**, the build orchestrator the repository's build graph runs on. The scaffolded project
  pins its own moon version in `.prototools`, so [proto](https://moonrepo.dev/proto) installs it
  (`proto use` in the project). moon **must** be found on the machine that builds the model:
  without it there is no build. SWCraft's own commands work without it.
- **VS Code**, optionally, for the panel and the assistant — the installer hands the extension to
  it when it finds it ([In VS Code](vscode.md)).

## Create the repository { #create-the-repository }

<!-- purpose: Scaffolding a repository with a build graph with init --integration moon, and its files -->

```sh
swcraft init vehicle-arch --integration moon
cd vehicle-arch
```

`--integration moon` is what makes this a buildable repository. Without it, `swcraft init` writes
only the minimal scaffold [Getting started](getting-started.md#create-a-project) describes, and
there is no build graph. With it, `init` writes the template's files instead: the shared ones
below, and the files of the chat clients chosen with `--client` — given once per client, GitHub
Copilot when none is named. Which files each client gets is on [In
VS Code](vscode.md#turning-a-folder-into-a-project), and Kiro's on [In
Kiro](kiro.md#onboard-project-in-kiro).

| File | What it is for |
|---|---|
| `moon.yml`, `.moon/workspace.yml`, `.moon/toolchain.yml` | the build graph and its workspace |
| `.prototools` | the project's moon version — and no Python version, because the project installs nothing |
| `swcraft.yaml` | the manifest, with the project's name and `toolchain_version` set to the release that scaffolded it |
| `.vscode/settings.json` | stops the editor checking generated `.arxml` against a schema it cannot have, and keeps the spell checker out of the model and the outputs ([In VS Code](vscode.md#the-red-squiggles-are-not-the-gate)) |
| `.gitattributes`, `.gitignore`, `README.md` | line endings, ignored files (`.env` among them), and the project's own build instructions |
| `model/` | the model tree, with the four scaffolded elements: `hardware/CPU.yaml`, `hardware/CORE0.yaml`, `hardware/PARTITION0.yaml` and `compositions/Root.yaml` |

What it does **not** write is as deliberate: no environment, no install script, no requirements
file and no chat server configuration. The toolchain is installed once per machine, the clean step
is `swcraft clean-outputs`, and the SWCraft extension registers the assistant's server with the
editor itself — so there is nothing per machine to generate, to ignore or to repair.

The project name defaults to the directory name; `--name` sets another.

**One line ties the project to a release.** `swcraft.yaml`'s `toolchain_version` is compared with
the installed SWCraft by every `swcraft` command before it does anything else. The commands that
write or check refuse a mismatch in one sentence naming both releases; the read-only ones print the
same sentence and answer anyway ([Getting started](getting-started.md#create-a-project) lists
which are which). It names one release, never a range, and a manifest without it pins nothing. The
check works forward only: a SWCraft release older than the one that introduced the field ignores
it.

## Build the model { #build-the-model }

<!-- purpose: Building with moon run :build: the tasks in order, what the exit code means, what stays on disk -->

```sh
moon run :build
```

That one command is the whole local build:

```text
clean-outputs -> check-model -> generate arxml/diagrams/needs -> validate-arxml
```

| Outcome | Meaning |
|---|---|
| exit 0 | the model is valid; the ARXML, the diagrams and the `needs.json` declared in `swcraft.yaml` are freshly generated, and `dist/build-report.md` says what the build gave — unless a `Warning: the build report was not written:` line says why there is none |
| non-zero exit | the build failed, and the message names why. A failed model check left no output of its own task; a failed `validate-arxml` leaves the ARXML on disk and, unless a `Warning:` line says otherwise, `dist/build-report.md` beside it, whose verdict names the failed check |

A `WARNING:` line in the output is a listed gap, not a failure: the build goes on and exits 0. A
pipeline that must not let a warning through adds `--warnings-as-errors` to the `check-model` and
`validate-arxml` tasks' `args` in `moon.yml`; the scaffold does not.

`validate-arxml` is the last task and the one that decides. It waits for all three generators and
runs `swcraft validate --report`, so the build report never reads an output while it is being
written, and a failing generator stops it. It is never served from moon's cache — its verdict
depends on the licensed schema on the machine and on the kept previous ARXML, which no declared
input covers — so it runs on every build. [The build report](cli.md#the-build-report) says what
the report holds; it changes no exit code and proves nothing on its own.

**A failing build can leave files behind — read the exit code, never the directory.**
`clean-outputs` runs first: it moves the previous `dist/model.arxml` aside as
`dist/model_old.arxml` and removes the rest of the generated outputs, and every generator waits for
`check-model`, so a failing build never leaves the *previous* run's outputs under their own names.
What it can leave is part of *this* run: the diagrams and `needs.json` wait for `check-model` alone,
so a build that passes the model check and fails at `validate-arxml` keeps them; and `validate`
writes the ARXML before it checks it, so a failing schema check leaves the refused file at
`dist/model.arxml`. A document is written whole or not at all, but no file's presence says a build
passed. The kept `model_old.arxml` is the previous *written* ARXML, not necessarily one that passed.

`clean-outputs` removes only the two output folders `swcraft.yaml` declares, `dist_root` and
`views_root`, and keeps `dist/audit.jsonl` — the assistant's record of model changes — beside the
kept ARXML. The kept copy is one level deep: an ARXML configured in a subfolder of `dist_root`, or
directly under `views_root`, is not moved, and a `dist_root` inside `views_root` loses it. An
output folder that is the project folder itself, is absolute, or reaches outside the project
through `..` is refused by name before anything is removed.

A new repository has an empty model, which builds and gives empty but valid outputs. Write the
model by hand under `model/` ([Working with the model](model.md)) or through the assistant
([The assistant](assistant.md)). In VS Code, the panel's **Build** runs the same steps without
moon ([In VS Code](vscode.md#the-panel)).

## Adding SWCraft to an existing repository { #an-existing-repository }

<!-- purpose: Adding SWCraft to a repository that already has files: what init keeps and what to merge -->

The same command works in a folder that already has content:

```sh
swcraft init existing-arch --integration moon
```

`init` **never overwrites a file**, and never writes through a link. Every file it would write is
written only when absent; anything already there is reported as `Skipped <path> (already exists)`
and the command still exits 0. So an existing repository keeps its own `.gitignore`, `README.md`
and `swcraft.yaml`. Two exceptions to "exits 0": `init` reads the `swcraft.yaml` it finds, to
place the spell checker's exclusions, so one whose `toolchain_version` names another release
refuses `init` with the pin's sentence before anything after its *Skipped* line is written; and a
`.gitignore` without a `.env` line gets that one line added, nothing else in it changed.

The four model elements `init` scaffolds are also held back when writing them would break a model
that passed before:

- **The hardware.** `hardware/CPU.yaml` is not written into a project that already declares a
  processor under any file name — a project has exactly one — and none of the three hardware files
  is written when the name it declares is already taken. The line reads `Skipped <path>
  (hardware/ already declares the processor '<name>')`. `PARTITION0.yaml` is also held back
  whenever the same run does not write `CORE0`, because `init` never puts a partition on a core it
  did not write: `Skipped <path> (its core 'CORE0' is not one this run writes)`.
- **The root composition.** `compositions/Root.yaml` is not written when the layer already
  declares a root, or already declares an element named `Root` — a repository whose root is
  `compositions/VehicleTop.yaml` would otherwise fail the model check with two roots.

A layer that cannot be read holds back its own candidates and nothing else. Nothing already on
disk is changed either way, and nothing else in either layer is checked: every other rule is the
model check's.

After running it in a repository that already had content:

1. **Read the `Skipped` lines** — those are the files to merge by hand. A kept `.gitignore`
   usually needs `dist/`, `views/` and `.moon/cache/` added.
2. **If the repository already had a `swcraft.yaml`**, it is kept as it was, so add the pin by
   hand — `toolchain_version: "<the release you installed>"` — and check that its `model_root`,
   `dist_root`, `views_root` and `outputs:` agree with what `moon.yml` declares as task outputs.
3. **If the repository was set up with SWCraft 0.6 or earlier**, delete the files that release
   wrote and nothing reads any more: [Upgrading](upgrading.md#upgrading-0-7-0) lists them.
4. **Build**: `moon run :build`, as [above](#build-the-model).

## On a build machine { #on-a-build-machine }

<!-- purpose: Running the build on a CI/CD build agent: what to install, what to run, what the exit code means -->

SWCraft ships a local build and nothing else: no pipeline definition comes with the scaffold, and
choosing and running a CI/CD system, a package feed, its credentials and its agents is the work of
whoever runs that system. What such a pipeline wraps is two things, in a checkout of the
architecture repository:

1. **Install the archive once on the build agent**, with `--no-vsix`: there is no editor on it,
   and everything else — the layout, the `PATH` entry, the version — is the same as an architect's.
   Add `--no-path` when the agent's job names the executable by its full path. On Windows a flag
   is also what keeps the installer from waiting for a key; `SWCRAFT_NO_PAUSE=1` covers a call
   with no flags. Where the organisation runs its own package index, the same installer takes
   `--index-url URL` in place of the archive's own packages — a change on the agent's side that
   touches nothing in the repository. The install is the agent's provisioning step, not part of
   every build ([The installer's options](getting-started.md#the-installers-options)).
2. **Run `moon run :build`.**

On a Linux or macOS agent, the first once, in the unpacked archive, and the second on every build in
the checkout (`swcraft_install.cmd --no-vsix` on Windows):

```sh
./swcraft_install.sh --no-vsix
moon run :build
```

A non-zero exit means the model is invalid **and the failing task produced no output of its own**
— earlier tasks may have written theirs, and `validate` writes the ARXML it then checks — so read
the exit code, never the presence of a file. A zero exit means the outputs declared in
`swcraft.yaml` are present and current, a `WARNING:` line notwithstanding, unless the tasks run
with `--warnings-as-errors`. To run the steps without moon, [In a build
pipeline](cli.md#in-a-build-pipeline) gives the same commands in the order that works.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next after setting up an architecture repository -->

| If you want to | Read |
|---|---|
| install SWCraft on a workstation | [Getting started](getting-started.md#install) |
| move the repository to a newer SWCraft | [Upgrading](upgrading.md) |
| find out why something did not work | [Troubleshooting](troubleshooting.md) |
| run the build's steps without moon | [Command line](cli.md#in-a-build-pipeline) |
