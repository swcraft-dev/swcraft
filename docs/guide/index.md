<!-- draft: not yet reviewed by the product's author -->

# SWCraft

<!-- purpose: When you meet SWCraft for the first time: what it turns your text files into, and how -->

SWCraft turns an architecture you write as plain text files into the artefacts the rest of
the toolchain needs — AUTOSAR ARXML, diagrams and the trace for the requirements team — and it grades the
whole thing every time you change it.

## What it actually is { #what-it-actually-is }

<!-- purpose: What SWCraft is: an architecture written as YAML, one element per file, checked every time -->

You write the architecture as YAML: one file per element, named after the element it
holds. A software component is a file. So is an interface, a data type, a
connector, a task, a requirement. There is no project file listing them and no index to keep
in step — the directory *is* the list, which is what makes the model reviewable in a pull
request and mergeable by the same tools as source code.

From those files SWCraft generates three things, and checks all of them:

- **AUTOSAR ARXML** — the component types, their ports, the interfaces and the data types,
  ready for the AUTOSAR toolchain. Generated against AUTOSAR 4.2.2 or 4.3.0.
- **Diagrams** — UML/SysML views as SVG drawings with a page each, shown in a VS Code tab that
  leads from a drawn element to its file and back, or opened from the file in a browser: an internal block
  diagram and an overview per composition, the nesting of the compositions, the deployment of the
  tasks into partitions on the cores (when the model declares a task), and one sequence diagram per scenario you
  declare.
- **Requirements** — your architecture requirements and the elements that satisfy requirements, as
  one Sphinx-Needs file the requirements team loads into its own documentation.

And one command decides whether any of it is allowed out: `swcraft validate` reads the whole
model, generates the ARXML and checks it. It exits 0 or it does not, and a failing run is one of
two kinds. A model problem stops the run before anything is generated, so there is nothing on disk
to mistake for a good file. A problem the schema check finds comes after generation: by then the
AUTOSAR file has been written, the run names it and exits 1, and the refused file stays on disk —
so read the exit code, never the directory listing.
[Working at the command line](cli.md#the-two-commands-that-grade) says which failures leave what.

## What it is for { #what-it-is-for }

<!-- purpose: What SWCraft is for: one source of truth, checks over the whole architecture, a safe assistant -->

Three things, in the order they tend to matter.

**One source of truth.** The component list, the diagram and the ARXML stop being three
documents that agree by discipline. They are one description with three renderings, and the
renderings are regenerated rather than maintained.

**Checks that read the whole architecture.** A schema can tell you a key is misspelled. It
cannot tell you that a port names an interface nobody declared, that two components claim
one name, that a connector joins two provide ports, that a task names a partition the hardware
does not declare, that a requirement was written which nothing in the architecture realizes, or that a
component was checked against a version of a requirement that has changed since. SWCraft can,
and it says so in the vocabulary you wrote rather than as a line number in a generated file.
[The checks](reference/rules.md) is the whole list.

**An assistant that cannot go behind your back.** SWCraft exposes the model to a chat
assistant in your editor, and the assistant reaches the model through SWCraft or not at all
— it has no file editing and no terminal. Every change it proposes arrives as one reviewable
set that is applied whole or not at all, and it is refused outright when it would break a
check that is an error; a check that is a warning holds nothing back, and `swcraft validate`
lists the warning afterwards.
[The assistant](assistant.md) is about what that means in practice.

## Which page you want { #which-page-you-want }

<!-- purpose: Which page of this guide to start at, by who you are and what you want to do -->

| If you are | Start at |
|---|---|
| trying it for the first time | [Getting started](getting-started.md) — empty directory to checked ARXML, one sitting |
| setting up the repository a team builds its architecture in, or a build machine for it | [An architecture repository](architecture-repository.md) |
| wondering how the pieces fit, and where SWCraft stands in an AUTOSAR project | [The big picture](big-picture.md) |
| wondering what goes in which file | [Working with the model](model.md) |
| looking for the panel, the diagrams or the hovers | [In VS Code](vscode.md) |
| about to let an assistant change your model | [The assistant](assistant.md) |
| doing a common task step by step, by hand or through the assistant | [How-to](how-to/index.md) |
| working over SSH, or wiring up a build | [Command line](cli.md) |
| trying to read a generated diagram | [Diagrams](diagrams.md) |
| handing component placement and runnable scheduling to the test team, and reading what they accepted | [Test tables](test-tables.md) |
| asking where a component could move to another partition, and what it would buy | [Experiments](experiments.md) |
| about to commit, and asking what the change does to the architecture | [The Change Report](change-report.md) |
| asking what state the architecture is in and where it is heading | [Architecture metrics](metrics.md) |
| moving a project to a newer SWCraft | [Upgrading](upgrading.md) |
| stuck on an install, a build or a check that will not pass | [Troubleshooting](troubleshooting.md) |
| looking up a key, a check or an option you half-remember | [Reference](reference/index.md) |

The reference section is generated from SWCraft's own definitions and describes the version
you have installed, so it is exhaustive and it is never out of date with the product. The
pages outside it are written by hand: they explain how the pieces fit together, which no
generator knows.

If SWCraft is installed, `swcraft docs` is how you get back here: it serves the copy of the
guide that came with your install — on your own machine, with no network involved — and opens
it in a browser. The address is printed either way; `swcraft docs --no-browser` prints it and
leaves the browser alone.

## What SWCraft is not { #what-swcraft-is-not }

<!-- purpose: What SWCraft is not: no canvas, no code generator, no AUTOSAR schema vendor -->

- **Not a modelling tool with a canvas.** The model is text. Diagrams are generated from it,
  never drawn into it.
- **Not a code generator.** It generates the architecture description, not the C behind it.
- **Not an AUTOSAR schema vendor.** The AUTOSAR XSD schemas are licensed and SWCraft ships
  none. Without one on the machine, `swcraft validate` checks the generated ARXML
  structurally and says in its output that it did so — it never claims a check it did not
  run.
- **Not a service.** There is nothing to stand up, nothing to log into and no data leaving
  your machine except what you send an assistant yourself.

## A note on what has been measured { #a-note-on-what-has-been-measured }

<!-- purpose: Which platforms SWCraft has been measured on, and where to find the guide's version -->

SWCraft is exercised on macOS and Linux. Windows has not been measured, so treat the paths
and the keyboard shortcuts in this guide as the shapes rather than as tested facts on that
platform. Which version of SWCraft this copy of the guide was built from is written on its
own `version` page, at `/version/` in the built site.
