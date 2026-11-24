<!-- draft: not yet reviewed by the product's author -->

# The big picture

<!-- purpose: When you want the whole picture: where SWCraft stands and how its layers fit together -->

[Start here](index.md) says what SWCraft produces and what it refuses to be. [Working with the
model](model.md) says what goes in which file. This page is the one in between: where SWCraft
stands in an AUTOSAR project, how the model's layers hold together as a whole, what is
mandatory and what is a choice, and how people actually work with it day to day. Read it once
before the model page; after that you will not need it.

## Where SWCraft stands { #where-swcraft-stands }

<!-- purpose: Where SWCraft sits in an AUTOSAR project: the VFB height and the height below it -->

AUTOSAR software is described twice, at two heights.

The upper one is the **Virtual Functional Bus**, the VFB. On it, software components talk to
each other only through ports and interfaces, and nothing says where a component runs — on
which core, on which ECU, or whether a signal crosses a network on its way. A composition
groups components, connectors join their ports, and the whole thing is an architecture that
would be true on any hardware. That is the height a domain architect works at.

The lower one is the **Runtime Environment**, the RTE: the generated C code that makes the
bus real on one ECU. It turns "this port sends a value" into a function call, a buffer write
or a network frame, and it calls each runnable when its event fires. An RTE generator reads
the AUTOSAR files and writes that code — and to do so it needs things the VFB never mentions:
what each value is stored in, which runnable runs in which task, and what the processor is.

SWCraft describes the VFB height completely — components, ports, interfaces, compositions,
connectors — and the part of the lower height an architect decides: the data types a value is
stored in, the tasks and the partitions and cores they run on, the hardware. It writes the result as AUTOSAR
ARXML for the toolchain that generates the RTE, and it stops there: SWCraft generates no RTE,
no C code and no basic-software configuration. [Start here](index.md#what-swcraft-is-not) is the
list of what it is not.

## One model, fifteen layers, three groups { #one-model-fifteen-layers-three-groups }

<!-- purpose: The fifteen layers in three groups, and the order in which a model gets filled in -->

The model is a directory of YAML files, one element per file, and the files are organised in
layers — one directory per layer, fifteen in all. They are easiest to hold in your head as
three groups, which is also the order a model gets filled in. The reference page [The model
layers](reference/model/index.md) is the exhaustive list; this is the map.

**What the software is** — the part that reaches the AUTOSAR file:

- **Data types** are the vocabulary, seven layers under `data_types/`. A **base type** says
  how the platform stores a value (`uint8`, 8 bits, a C declaration). An **implementation
  type** is what generated code holds a value in — a value type on a base type, a reference to
  another type, a structure, a fixed array. An **application type** is what the value *means*:
  the unit it is measured in, the conversion from the stored number to the physical one, the
  range it is declared within, or an enumeration's named values. Units, conversions and
  constraints can be elements of their own, shared. A **mapping set** pairs the two views: it
  says which implementation type stores each application type.
- **Interfaces** are what ports speak: a sender-receiver interface lists typed data elements,
  a client-server interface lists operations with typed arguments. Each element names its type
  as either an application type or an implementation type.
- **Software components** own ports, each port names an interface and a direction, and a
  component's internal behaviour — its runnables, the events that start them, the data they
  read, write and call — is written beside the ports.
- **Compositions** nest components and other compositions, and expose boundary ports;
  **connectors** join a provide port to a require port over one interface.

**What it runs on** — the part the RTE generator needs beyond the bus:

- **Hardware**: exactly one processor, its cores, the partitions on them, its memories. Every
  project has this layer.
- **Tasks**: the OS tasks, each in a partition, and the runnables each one executes.

**What it is for, and what it does** — the part that reaches documents and diagrams, never the
ARXML:

- **Requirements**, the architecture ones published with the elements that satisfy requirements as a Sphinx-Needs file. A requirement names no element that realizes it;
  the components, and the other elements that realize it, name the requirements they satisfy (an
  element's `satisfies`). A derived requirement names what it derives from, in `derived_from`.
- **Sequences**, scenarios across components that become sequence diagrams.

Everything references everything else by name — a port names an interface, an element names a
type, a connector names two ports, a task names a partition — and SWCraft follows every one of
those names every time it reads the model. That is what the checks are: not "is this file
well-formed" but "does the whole still hold together".

## What is mandatory, and what is a choice { #what-is-mandatory-and-what-is-a-choice }

<!-- purpose: Which layers a valid AUTOSAR file needs, and which ones you add only when you need them -->

A model that generates a valid AUTOSAR file needs surprisingly little: base types,
implementation types, interfaces typed by them, components with ports, and the hardware layer
every project carries. [Getting started](getting-started.md) builds exactly that in five files.

Everything above it is a choice you make per project, and each choice brings its own rule:

- **Application types** are optional. A model whose interfaces are typed by implementation
  types alone has no physical view and needs no mapping set. The moment an interface element
  names an application type, that type needs an implementation through a mapping set the
  component names — the RTE cannot store "a speed in km/h" without being told it is a
  `uint16` — and `swcraft check-model` reports the one that has none. A mapping is many to one,
  not a twin per type: ten physical quantities may all be stored in the same `uint16`.
- **Compositions and connectors** are optional until you want a structure and a wiring;
  **tasks** until you want to say what runs where; **requirements** and **sequences** until you
  want the documents and the diagrams they produce.

A model imported from another tool's export usually arrives with every layer filled, in that
tool's habits — often an implementation type of the same name beside every application type.
That is the exporting tool's convention, not a rule of AUTOSAR or of SWCraft; the same
architecture written by hand would have far fewer implementation types.

## How you work with it { #how-you-work-with-it }

<!-- purpose: The three ways to start a model (from nothing, from an export, from requirements) and the work loop -->

There are three ways in, and one way of working once you are in.

**From nothing.** `swcraft init` scaffolds a project — a manifest, the layer directories, a
placeholder processor — and you write element files. The first five take an afternoon; the
[Getting started](getting-started.md) page walks them.

**From an export.** An ARXML file another tool wrote is imported whole with
`swcraft import model`, and a single component with `swcraft import component`. Both produce a
**report** that says, construct by construct, what was carried and what was not and why: a
mode-switch com-spec SWCraft has no representation for, a limit it could not read as a number,
an element of a kind it does not know. Nothing is silently dropped — a line in the report is
the promise — and what was carried comes back out of `swcraft generate arxml` as the same
AUTOSAR, so the export's meaning survives the round trip even where its bytes do not. An export
carries no requirement, so a freshly imported model has none, and its components satisfy none
until you link each one to the requirements it realizes; neither is a finding, and a requirement
you then write is listed as a warning until something satisfies it.
[Command line](cli.md#exchanging-with-other-tools) is the detail. Requirements written in a
requirements tool come in through `swcraft import requirements`
([Command line](cli.md#importing-requirements)).

**From an assistant.** The model is also reachable by a chat assistant in your editor, through
SWCraft: it has no file editing and no shell, and its one read-only file tool is kept off the model
by the project's model guard while the guard runs — the guard's limits are listed on
[In VS Code](vscode.md#the-model-guard). Every change it proposes is one reviewable set that is applied whole
or refused whole, under the same checks you run yourself. [The assistant](assistant.md) is about what that means in practice.

Whichever way in, the loop is the same. You change files — in the editor, by hand, by an
assistant's proposal. `swcraft check-model` reads the whole model and says what no longer holds
together, in your own names: *port `pSpeed` of `BrakeController` names interface `IVehicleSpeed`,
which no file declares*. `swcraft generate diagrams` draws the views you want to look at.
And `swcraft validate` is the gate: it generates the ARXML and checks it against the AUTOSAR
schema, and either it exits 0, or the run names the file it refused — read the exit code, not
the directory. In VS Code the same loop is the panel's three buttons; in a pipeline it is three lines.

## Two architects, one model { #two-architects-one-model }

<!-- purpose: How a domain architect and an integration architect share one model, each in their own layers -->

The layers are also a division of labour. A domain architect works at the VFB height —
interfaces, components, compositions, the physical types a signal is measured in — and need
never open the tasks directory. An integration architect works below it — the hardware, the
tasks, what each value is stored in, the resources — and takes the upper layers as given.
Both edit one model, in one repository, and the checks hold the seam between them: a task
naming a runnable that was renamed, a mapping set missing the type a new interface introduced,
a task in a partition that is not there, each shows up on the next `check-model` run, naming both
sides.

## Four principles, which explain most of the design { #four-principles-which-explain-most-of-the-design }

<!-- purpose: The four principles behind the design: text is the truth, one gate, nothing lost, the standard first -->

**The text is the truth.** There is no database and no canvas. Diagrams, ARXML and documents
are renderings of the files, regenerated and never edited; the files are reviewed, diffed and
merged like source code, by the same tools.

**One gate.** `swcraft validate` is the only command that says yes. Editor squiggles and the
assistant's refusals are early warnings in the same vocabulary, never a substitute: a model is
good when `validate` exits 0, and not before.

**Nothing is lost silently.** What SWCraft reads it writes back the same, and what it cannot
carry it reports by name. A value an AUTOSAR file can hold is never changed or dropped on the
way through; when a tool export and its re-export differ, each difference is one of the
maintainer's closed list of allowed differences, every entry justified against the standard's own text.

**The standard first.** Where AUTOSAR says how something is written, SWCraft writes it that
way; where it leaves a choice and nothing is lost, SWCraft takes the smaller change. Matching
the output of any particular other tool byte for byte is not a goal, and never was.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next after the big picture: the first project, the layers and the checks -->

- [Getting started](getting-started.md) — the five files and the one command.
- [Working with the model](model.md) — every layer, what each key means, how the files find
  each other.
- [The checks](reference/rules.md) — the whole list of what `check-model` holds you to.
