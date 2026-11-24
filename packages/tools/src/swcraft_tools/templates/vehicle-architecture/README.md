# {{PROJECT_NAME}}

A standalone SWCraft architecture project. The YAML model under `model/` is the
source of truth; every generated artifact is derived from it by one command.

**This project installs nothing.** It carries no environment, no bootstrap
script and no wheels — only the model, the manifest, the build graph and the
agent instructions. The SWCraft toolchain is installed **once per machine** from
the workstation archive and is reached as a single `swcraft` command on
`PATH` — a `PATH` the installer edits for you on Windows and, on macOS and Linux,
prints one line for you to add to your shell profile; this project records in
`swcraft.yaml` *which release* it is built with, and every `swcraft` command checks that
pin before it does anything else.

## Prerequisites

Two things, both installed once on the machine and neither of them per-project:

- **The SWCraft toolchain.** Unpack `swcraft-<version>-<platform>.zip` and run
  the one installer file inside it (`swcraft_install.cmd` on Windows,
  `swcraft_install.sh` elsewhere). Then get `swcraft` on `PATH`, which is **not
  the same act on both platforms**: on Windows the installer has already edited
  the user `PATH`, so all that is left is to open a **new** terminal; on macOS and
  Linux it edits no shell file at all — it prints one
  `export PATH="<home>/current/bin:$PATH"` line — so add that line to your shell
  profile and then open a new terminal. Until you do, `swcraft` is `command not
  found` and the executable is there under the installation home all the same.
  Then check it:

  ```sh
  swcraft --version
  ```

  The same archive carries the SWCraft VS Code extension, which the installer
  installs for you. Inside VS Code the extension can do the whole thing —
  *SWCraft: Install Toolchain* and *SWCraft: Update Toolchain* point it at an
  archive.
- **[proto](https://moonrepo.dev/proto)** — `proto use` installs the `moon`
  version pinned in `.prototools`. moon runs the build graph below; it is not
  part of the SWCraft toolchain.

The version this project expects is `toolchain_version` in `swcraft.yaml`. If
the installed toolchain is a different release, every `swcraft` command that
writes or gates refuses and names both versions: install that release's archive,
or raise the pin and rebuild.

## Build the model

```sh
moon run :build
```

That single command is the whole local contract:

```
clean-outputs -> check-model -> generate arxml/diagrams/needs -> validate-arxml
```

| Outcome | Meaning |
|---|---|
| exit 0 | the model is valid; `dist/model.arxml`, the diagrams under `views/` and the Sphinx-Needs `dist/needs.json` are freshly generated, and `dist/build-report.md` says what the build gave — unless a `Warning: the build report was not written:` line says why there is none |
| non-zero exit | the build failed; the failure message names why. A failed model check left no output of its task; a failed `validate-arxml` check leaves `dist/model.arxml` on disk and, unless a `Warning: the build report was not written:` line says why not, `dist/build-report.md` beside it, whose verdict names the failed check |

A `WARNING:` line in `check-model`'s or `validate`'s output is a listed gap, not a failure: the
build goes on and exits 0 (the warnings are a requirement nothing satisfies, a link whose
version is not stated or is not the requirement's current one, and a requirement that is a stub,
named as a parent and never imported; a component that satisfies no requirement is no finding).
A pipeline that must not let one through adds `--warnings-as-errors` to the `check-model` and
`validate-arxml` tasks' `args`; this project does not.

`validate-arxml`, the gate, runs last: it waits for all three generators and runs
`swcraft validate --report`, which also writes `dist/build-report.md` once the ARXML
is generated, whether its check then passes or fails — the verdict and which check
gave it, what was built, the warnings, the cores and memories, and the ARXML
elements changed against `dist/model_old.arxml`. The report is for a person to
read: it changes no exit code and is no qualification evidence. Two consequences
of its shape: a generator that fails stops the gate from running, so a build
that fails before the gate has no report (the first task removed the last one);
and the gate is never served from moon's cache (it depends
on the licensed schema and on `dist/model_old.arxml`, which no input can
declare), so it runs on every `moon run :build`. `model_old.arxml` is the ARXML
the previous build that wrote one left; `swcraft validate` run on its own moves
the previous ARXML aside only when the new one differs, so there it is the last
ARXML that differed.

The guarantee is about **fresh** outputs, and it is worth reading twice because
the obvious shortening of it is false. `clean-outputs` runs before anything else:
it moves the previous `dist/model.arxml` aside as `dist/model_old.arxml` and removes
the rest of the generated artifacts, and every generator depends on `check-model`,
so a failed build can never leave the *previous* run's artifacts behind under their
own names to be mistaken for fresh ones. `model_old.arxml` is the one copy kept on
purpose: the previous ARXML that was written, not necessarily one that passed.
`swcraft generate arxml` and `swcraft validate` make the same move before they write,
and only when the content differs. What it can leave behind is a *this* run's partial set:
`generate diagrams` and `generate needs` depend on `check-model` alone, so a build that
gets past the model gate and fails at `validate-arxml` leaves both of their
outputs in place — and `swcraft validate` itself generates `dist/model.arxml`,
announces it and *then* checks it, so a failing XSD or structural check exits
non-zero with that file on disk, and `dist/build-report.md` written beside it
(unless a `Warning: the build report was not written:` line says why not).
Nothing there is a leak: a writer either writes a whole valid document or writes
nothing, which is the actual guarantee. Never treat the mere presence of an output
file — `dist/build-report.md` included — as evidence that the build succeeded:
read the exit code.

Individual steps can be run on their own, for example
`moon run architecture:check-model`.

One file inside `dist/` is deliberately **not** treated as a generated artifact:
`dist/audit.jsonl`, the MCP server's append-only record of every model change.
The clean step preserves it (and `.gitignore` keeps it out of the repository),
so the build never destroys that history.

### After upgrading the toolchain

Install the newer archive, raise `toolchain_version` in `swcraft.yaml` to match,
and run `moon run :build` again. The manifest is a declared build input of every
cacheable task, so the bump invalidates them all and the next build regenerates
with the new toolchain rather than serving the previous version's artifacts from
moon's cache.

### If you change `dist_root` or `views_root`

They are declared in `swcraft.yaml` **and** named literally in `moon.yml` (every
task's `outputs:`). Change both. The clean step needs no edit — `swcraft
clean-outputs` reads the two roots from the manifest — but a moon `outputs:`
entry left behind would point at a directory nothing generates into.

## Platform support

Every task in `moon.yml` declares `options.shell: false`. moon's shell defaults
to **on** for Windows and off everywhere else, and the composed command line it
hands to PowerShell is a parser this build does not need in its path: with the
older, path-spelled command it rejected the line outright and the build failed
on its first task, before SWCraft ran at all. Turning the shell off removes the
parser; on macOS and Linux it pins the behaviour those platforms already had, so
one committed spelling serves every platform and no per-OS fork of the file is
needed. Do not "tidy" it away.

This file is committed as it stands and works on Windows, macOS and Linux: the
scaffolded build has been run on a real Windows machine, where moon resolved the
bare `swcraft` from `PATH` with the shell off and every task of `moon run :build`
passed. If `moon run :build` cannot resolve `swcraft` on your machine, the
SWCraft installation is not on `PATH` — report that rather than editing
`moon.yml` per platform.

## Edit the model

Model changes belong in `model/` as YAML, one element per file. AI-assisted
changes go through the SWCraft MCP server rather than through direct file
edits, and `.github/agents/` carries the agent instructions that enforce that
boundary. Each chat client you set the project up for (below) brings a hook that
runs `swcraft guard` before an agent's tool call:
`.github/hooks/swcraft.json` for Copilot, which denies its file tools a read or
search of `model/`; `.codex/hooks.json` for Codex, which denies a patch or a
shell command that names `model/` and runs only once you trust the project's
`.codex/` folder and the hook in Codex (`/hooks`); and the hook in
`.claude/settings.json` for Claude Code, which denies a read, a search, an edit,
a write or a shell command (`Bash` or `Monitor`) that names `model/`, and every
`PowerShell` call, and runs only in a folder you trusted in Claude Code, with the
session started in this folder; and `.kiro/hooks/swcraft-guard.json` for Kiro,
which denies a read, a search, a write or a shell command that names `model/`,
a search over the whole project, and every tool it does not know, and runs only
in a folder you trusted in Kiro and then reloaded the window once.
They are a guardrail, not an
enforcement boundary; the user guide lists what they do not reach.

### What the model already holds

Every layer directory under `model/` is created for you, and **four element
files** are written into them. They are the minimum the model gate accepts, not
examples to imitate:

| File | What it is |
|---|---|
| `model/hardware/CPU.yaml` | the one processor. Exactly one must be declared — none and two are both model errors |
| `model/hardware/CORE0.yaml` | one core of that processor. A partition sits on a core by name |
| `model/hardware/PARTITION0.yaml` | one partition on that core, with no safety level. Every task names the partition it runs in, and takes its core from it, so the first task you write needs one to exist |
| `model/compositions/Root.yaml` | the **root composition** — the top of the exported hierarchy |

The root composition is the one of the four whose absence stops the build
immediately: exactly one composition must declare `root: true`, and the model
gate refuses a project where none does. It arrives with no members, because a
member places a component *type* and a fresh project declares none. As you add
components, place each one in it — or in a sub-composition it contains — as a
`{name, type}` pair:

```yaml
name: Root
title: Root composition
root: true
members:
  - name: SpeedSensor
    type: SpeedSensor
```

A connector that names no composition is drawn inside the root, which is why a
flat project needs nothing else here.

### The MCP server and the assistants' files

For Copilot the SWCraft VS Code extension registers the server with the editor:
one server per open folder whose root holds a `swcraft.yaml`, started with the
`mcp-serve` subcommand against this manifest, from the one SWCraft installation
on the machine. Codex, Claude Code and Kiro read the server from files this project
carries, each naming the bare `swcraft` and this project's manifest, so a fresh
clone needs nothing done to them.

The project carries the files of the chat clients chosen when it was made:
`swcraft init --integration moon --client <name>`, repeated for several, or the
choice Onboard Project offers. Copilot alone is the default.

| Client | Its files |
|---|---|
| `copilot` (GitHub Copilot) | `.github/agents/swcraft-architect.agent.md`, `.github/copilot-instructions.md`, `.github/hooks/swcraft.json` |
| `codex` (Codex) | `AGENTS.md`, `.codex/config.toml`, `.codex/hooks.json` |
| `claude` (Claude Code) | `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.claude/settings.json` |
| `kiro` (Kiro) | `.kiro/settings/mcp.json`, `.kiro/steering/swcraft.md`, `.kiro/hooks/swcraft-guard.json` |

What each file is for:

| File | What it is for |
|---|---|
| `.github/agents/swcraft-architect.agent.md`, `.github/copilot-instructions.md` | the assistant's instructions for Copilot |
| `.github/hooks/swcraft.json` | Copilot's model guard hook |
| `.mcp.json` | Claude Code's server line, with the review in the SWCraft review tab |
| `.claude/settings.json` | Claude Code's model guard hook, the server's approval and its foreground calls |
| `.codex/config.toml` | Codex's server line, with the review in the SWCraft review tab, and the size up to which Codex reads `AGENTS.md` |
| `.codex/hooks.json` | Codex's model guard hook |
| `AGENTS.md` | the assistant's instructions for Codex and Claude Code: the agent profile's text after a note on file tools |
| `CLAUDE.md` | the assistant's instructions for Claude Code: one line importing `AGENTS.md` |
| `.kiro/settings/mcp.json` | Kiro's server line, with the review in the SWCraft review tab |
| `.kiro/steering/swcraft.md` | the assistant's instructions for Kiro: the agent profile's text after a note on file tools |
| `.kiro/hooks/swcraft-guard.json` | Kiro's model guard hook |

With Copilot and another client together, `.vscode/settings.json` also turns off
Copilot's reading of `AGENTS.md` and `CLAUDE.md`
(`"chat.useAgentsMdFile": false`, `"chat.useClaudeMdFile": false`), so Copilot
keeps to its own instructions; a settings file that already existed is left as
it was. With Copilot and Claude Code together, Copilot also reads `.mcp.json`
and may list the SWCraft server twice; the user guide says what to do. With Kiro
and Codex or Claude Code together, Kiro also reads `AGENTS.md`, so it reads the
instructions twice, the second time cut short; the user guide says more.

Start Codex, Claude Code and Kiro in this folder: the server lines name the manifest
relative to it. SWCraft's **Onboard Project** writes the same files and keeps
one that is not recognisably its own: one you wrote, or one whose first line no
longer says SWCraft wrote it. Another MCP host is configured by hand; the SWCraft user guide has the
one-line invocation for it.

## Downstream integration

**This project delivers a local build contract only.** It ships
no pipeline definition of any kind, and wiring one up is downstream integration
work owned by whoever operates the CI/CD system — not by this project.

The contract an integrator wraps is exactly the two steps above:

1. install the SWCraft toolchain on the agent from the workstation archive, at
   the release `toolchain_version` names;
2. run `moon run :build`.

A non-zero exit means the model is invalid and no artifact the failing task
declares was produced — the tasks *before* it in the chain may well have written
theirs, and `validate` writes the ARXML it then checks; a zero exit means the
three generated artifacts are present and current, a `WARNING:` line in the
output notwithstanding (a warning is listed and fails nothing unless the task
runs with `--warnings-as-errors`). Nothing
else about the build needs to be understood to automate it, and nothing about
where the toolchain comes from is fixed here: an internal package index replaces
the archive's bundled wheels by changing one flag on the installer.
