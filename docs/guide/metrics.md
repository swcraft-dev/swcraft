<!-- draft: not yet reviewed by the product's author -->

# Architecture metrics: what state the model is in, and where it is heading

<!-- purpose: When you want to know what state the architecture is in, and where it is heading -->

`swcraft check-model` tells you whether the model is valid. **Architecture Metrics** answers the
question an architect keeps asking about a model that is valid, or not yet: *what state is the
architecture in, and where is it heading?*

The report opens with an **Overview**: the two questions and a short answer to each, a few
sentences in plain words, for a reader who reads nothing else. Then comes **Definitions and method**:
each term its figures use — for example a node, an external stub, a dependency and its direction,
Ca, Ce and Ct, the mean and deviation, a hub, isolated, a composition table, cohesion, coverage,
`n/a` and movement — with how it is counted, in a few plain lines. That is the short form of this page.
Then come the figures, in three blocks:

- **State** — how big the model is, how its components depend on one another, how each composition
  is wired inside, how well each component's runnables hold together, how much of the model carries
  a safety level, a runnable, a task and a requirement link, and what `check-model` finds.
- **Movement** — the main figures at an earlier revision and now, and what changed between them.
- **What to look at** — the facts above that name a place, in a fixed order.

Every section names the terms it uses and points to them in *Definitions and method* instead of
defining them again, then gives its figures and what they mean for this model, in sentences computed
from the figures. **There is no overall score, the report is not a gate, and it
is no qualification evidence**: `swcraft validate` is the gate. A figure is a place to look, never a
verdict on the model.

Everything is computed from the model as it is; nothing is added to it and nothing is stored. Where
the model holds none of the data a figure needs, or the method does not apply (too few components to
have outliers, a threshold below zero, a component whose runnables give no pair, save one whose only
runnable uses a port or a variable, which gives 1), the report says
`n/a` and why, never `0`. So a fresh import with no tasks, no safety levels and no requirements still
gets an honest report.

## In VS Code: one click { #in-vs-code-one-click }

<!-- purpose: Making the Architecture Metrics report in VS Code with one click -->

The SWCraft panel's *Model* section has **Architecture Metrics**, under Resource Report (and the
palette *SWCraft: Architecture Metrics*). Click it: it runs the command for the open project, writes
the report and opens it in VS Code's Markdown preview. It asks nothing, and the command's own output
goes to the **SWCraft** output channel. It writes nothing into the model.

By default it compares your working tree with the last commit, `HEAD`. To compare with something else
— a release tag, the branch you started from — set **`swcraft.metricsSince`** in the folder's
settings to a list of revisions:

```json
{
  "swcraft.metricsSince": ["v1.2", "v1.3"]
}
```

The first revision is the base the changes are measured from; any others are further points of the
series, in the order you list them. With the setting empty, the comparison is with `HEAD`, and when
`HEAD` cannot be read — the folder is not a git repository yet, say — the report says why in its
*Movement* section and still shows the state. A revision you named that cannot be read fails the run,
and the output channel says which.

The report is written into `dist/metrics/metrics.md` (under your manifest's `dist_root`). Each press
replaces it, and `swcraft clean-outputs` removes it with the rest of `dist/`.

## On the command line { #on-the-command-line }

<!-- purpose: Running the metrics on the command line, against an earlier revision, to a file or as JSON -->

```sh
swcraft metrics --project swcraft.yaml
swcraft metrics --project swcraft.yaml --since-head
swcraft metrics --project swcraft.yaml --report
swcraft metrics --project swcraft.yaml --format json
```

The first gives the state only. `--since-head` compares it with `HEAD`, if that can be read;
`--report` also writes `dist/metrics/metrics.md`; `--format json` prints one JSON document. To
compare with a revision you name, give `--since v1.2`; repeat the option for a series,
`--since v1.2 --since v1.3`, the first named being the base.

A revision is read as `swcraft diff` reads one: a tag, a branch, a commit. Without `--since` or
`--since-head` there is no *Movement* section, and the second answer says no base was named.
`--since` and `--since-head` cannot be given together.

**The command exits 0 on every run that could read the project**, however many findings the model has
and whatever the figures say. It exits 1 when it cannot read the project, and when a revision you
named with `--since` cannot be read: a series with a silently missing column would mislead. With
`--since-head`, a `HEAD` that cannot be read is not a failure; the report says why.

With `--report` the command also writes `metrics/metrics.md` under `dist_root`, after the answer is
printed, and prints `Generated report: <path>` (on the error stream with `--format json`, so that
standard output stays one document). A report that cannot be written is one `Warning:` line and the
run still exits 0. A project pinned to another SWCraft release gets its answer and a warning, and no
file. Every option is on [The command line](reference/cli.md#swcraft-metrics).

## The overview { #the-overview }

<!-- purpose: Reading the metrics' Overview: the two answers in plain words -->

The *Overview* holds the two answers, each at most a few sentences in plain words, so it reads on
its own: no symbol (Ca, Ce, Ct) and no technical term (hub, deviation, cohesion, external stub,
`n/a`). A dependency and a finding keep their everyday sense; *Definitions and method* says exactly
how each is counted.

**What state is the architecture in?** is at most four sentences: the first sentence of *Size* and of
*Integrity*, as those sections say them; which components have far more dependencies, in and out, than
the rest (the hubs, named up to three; a component that depends on another which depends on it back
counts it twice, as Ct does), or that none has, or that with fewer than six components which of
them stand out is not measured; and in how many of the components that give a cohesion figure fewer
than half the pairs of runnables that use a port or a variable have one in common (cohesion below 0.5, still
labelled a starting threshold, not calibrated). A runnable that declares no access is in no pair, as
in the figure itself. A component whose only runnable gives the figure 1 has no pair, so when none is
below, the answer says *no fewer than half*, which holds of it too. Where coupling or cohesion is
`n/a`, the answer leaves it out and the section gives the reason. Coverage is the section's alone.

**Where is it heading?** is at most three sentences: how the dependencies between components changed
since the base, without where the change shows; the components added and removed; and the findings
introduced and resolved. Where the change shows, the mean, and a figure that cannot be compared are
the *Movement* section's. With no base, or nothing to compare, or no figure changed, it is that one
sentence.

Every figure an answer states is a figure its section states, so the overview never disagrees with
the sections below it; it only says less.

## What each section says { #what-each-section-says }

<!-- purpose: Reading each section of the metrics report: its terms, its figures and its limit -->

In the report each section opens with one line, *Terms*, naming the entries of *Definitions and
method* it uses. Below, for each section, are those terms, the long form of their definitions, when
the figures are `n/a`, and the section's limit.

### Size { #size }

<!-- purpose: Reading the Size section: how many elements and nodes the model has -->

**Terms:** Model elements; Size; Node; External stub.

The words first. A **component type** is one software component file; a **placement** is a member
of a composition that puts a type in it, and one type can be placed many times. A **port** is a point
of connection of a component or a composition, providing or requiring an **interface**, the contract
both ends share. A **connector** joins two ports inside a composition; a **delegation** joins a
composition's boundary port to a port of one of its members. A **runnable** is a piece of a
component's behaviour that runs, and a **requirement link** is a requirement id an element names as
one it satisfies: a component, one of its ports or runnables, an interface, a composition, a task or a
hardware element.

The long form: the number of declared components and external stubs, of compositions and how deeply
they nest, of ports (split into those on components, on external stubs and on composition
boundaries), connectors, delegations and runnables, and the count of every other kind of element
the model holds, such as interfaces, data types, units, tasks and hardware. Size counts what the
files declare, one element per file, so a name declared in two files counts twice here and once in
the figures below, except a composition table, which reads every connector file: two connectors
sharing a name and joining different pairs are two links there. A component name two files share,
which `check-model` reports, is read in every figure and sentence below off one of them: its last
declared file, or its last external stub when no declared file holds the name. The other files of
that name — their ports, runnables, safety levels and requirement links — are read by none but the
errors and warnings, which `check-model` reports over every file, so a stub never stands in for a
component a file declares; here a stub sharing a declared name counts among the
components, not the stubs. *Nested N deep* is the longest chain of compositions placed in one another; a
composition that holds no other one is 1 deep. Compositions that hold one another through a cycle,
which `check-model` reports, count as many levels as they are. Ports, connectors, delegations and runnables include
the stubs'. A size is context for the other figures: forty hubs mean something different in a
model of fifty components and in one of five thousand.

An **external stub** is a component the model declares only by reference, typically from an import:
it carries the ports the export showed, and whatever else its file declares, such as a runnable,
which *Size* counts. The size sentence says only that a stub is declared by reference, never what it
holds. The report counts stubs, but leaves them out
of every statistic (below) and of every coverage over the declared components; a stub's link still
allocates a declared requirement id no stub requirement holds, and allocates nothing for an id a stub
requirement holds, even when another file declares that id as a real requirement.

### Coupling { #coupling }

<!-- purpose: Reading the Coupling section: dependencies, hubs and isolated components -->

**Terms:** Dependency; Ca, Ce, Ct; Mean and deviation; Hub; Isolated; Fewer than six components;
Mean Ct without outliers; External stub.

The long form:

- **A dependency points from the component that requires to the component that provides**, read off
  the two ports' directions, for every kind of interface. A receiver depends on its sender, a client
  on its server: you can rebuild a receiver only knowing what its sender publishes, and a client only
  knowing its server's operations, while the converse holds for neither. This is not the data flow,
  and a server's reply does not reverse it.
- **The nodes are atomic components.** A composition is a container, so its connectors count as the
  links between the components they reach through every delegation below them. Wrapping two
  components in a new composition, or moving one a level deeper, moves no coupling figure: the figure
  measures dependency, not drawing.
- **Ca** counts the other components that depend on this one, **Ce** those it depends on, and
  **Ct = Ca + Ce**. Components are counted, not connectors: any number of connectors that make one
  component depend on another is one dependency. Two components that depend on each other count
  twice, once each way: a cycle is more coupling than a one-way link, not the same.
- **Dependencies** is the number of directed dependencies in the model, between a declared
  component and another component.
- **The mean Ct and its deviation** are over the declared components only, n of them: μ = ΣCt / n
  and σ = √(Σ(Ct − μ)² / n), the population's deviation, divided by n and not n − 1. **A hub** is a component whose Ct lies more than two
  deviations above the mean; **statistically isolated**, more than two deviations below it. **The
  mean without outliers** (the trimmed mean) leaves hubs and isolated out, and the report gives the
  highest Ct among the rest beside it; a list that is `n/a` leaves nobody out.
- **The table** lists the ten components with the highest Ct, ties by name; the JSON document holds
  every component.
- **External stubs** are neighbours, not members. A link to a stub counts in its declared neighbour's
  Ca or Ce, because the dependency is real, but a stub's own figure is not measured: its ports are the
  ones an export happened to show, and letting it into the statistics would move every threshold
  with that accident. A link between two stubs gives no dependency and counts in no Ca or Ce; its
  connector still counts in the size, and it is a link of the composition table both stubs are
  members of. The stubs most depended on (up to five) are listed with their count of dependents.

The thresholds are **statistics of this model**: a hub is a component far from *this* model's mean,
not one above a number someone chose. A hub is a place to look — it may well be a gateway that is
meant to be one. The table prints each threshold to two places; a Ct is a whole number, so where that
rounding lands on a whole number the threshold falls short of, the row says *Ct 5 or more* (*Ct 5 or
less* for isolated) instead of *above 5.00*, which a hub at Ct 5 would contradict.

The section's first sentence names the hubs or, when there is none, says *No component is a hub*
and gives the highest Ct. Isolated is decided on its own terms, so a component that is statistically
isolated is named in a sentence of its own beside the hubs: a model can have no hub and still an
isolated component.

**When it is `n/a`.** With no declared component, the whole block. With fewer than six components,
hubs and isolated: with so few values none can lie more than two deviations from their mean, so the
answer would be a zero the model did not earn. When the lower threshold falls below zero, isolated:
no Ct can be below zero, and the components with no link at all are named under *What to look at*
instead.

**The limit.** A component **type** counts once, however often it is placed: two instances of one
type are one node with the links of both, and a connector between two instances of the same type is
dropped. And some connectors give no measured dependency; the report counts each kind when there is
one: a connector with an end that reaches no component (an end that does not resolve, or a boundary
port nothing delegates), a pair of ends of the same direction, and a connector repeating a name
declared before. `check-model` reports the faults behind them.

### Coupling inside each composition { #coupling-inside-each-composition }

<!-- purpose: Reading the coupling inside each composition: the composition table -->

**Terms:** Composition table.

The long form: one table per composition, the root first and the rest by name. Its rows are the
composition's direct members — a component, a nested composition as one block, or an external stub,
marked as such — with Ca, Ce and Ct counted among those members only; a type placed twice is one
row, under its first member's name. A link is a distinct directed pair of members joined by a
connector the composition owns: several connectors between two members in one direction are one
link, and one connector is one link however many components it reaches further down; no delegation
is followed. A connector between two placements of one type gives no link. So the table answers *how tangled is this box inside*, which the coupling block, which
looks through every box, does not.

A member's name is its name in the composition; where its type has another name, the type follows in
brackets, `Logbox (LogBox_ext0)`. Stubs are rows here, unlike in the coupling block, because a
table has no statistics for them to skew, and leaving them out would misdraw the box.

**`Ct = 0` in a table** means only that the member has no link with the other members of this box.
A member wired only through the composition's boundary shows 0, so 0 here is not *isolated* and puts
nothing under *What to look at*.

**When it is `n/a`.** A composition with no member says so instead of an empty table; a model with no
composition says so for the whole section.

**The limit.** A table says nothing about a link that leaves the box (it appears one level up, as the
box's link) or about the components behind a nested block. A connector whose composition does not
resolve, that names an end which is not a member, whose port does not resolve, or that joins two ends
of the same direction is placed in no table, and the report counts each kind. A flat import gives one
root table over all its members.

### Cohesion { #cohesion }

<!-- purpose: Reading the Cohesion section: how well a component's runnables hold together -->

**Terms:** Cohesion.

The long form: for each component, C = s / (m(m − 1)/2), where m is the number of its runnables that
touch a port or a variable and s the number of pairs of them that touch at least one port or variable
in common. A runnable touches the ports its `reads`, `writes` and `calls` name, the port of an
operation, of a data reception or of an asynchronous call's return that starts it (a `data` event: the
runnable works on that port's data even when it does not read the element; a `call_returned` event: it
works on the result of a call through that port even when another runnable makes the call), and the
inter-runnable variables its `reads_variables` and `writes_variables` name. A port and a variable that happen to share a name are two different things,
never one in common. This is the measure as SWCraft computes it, not a textbook one: the choices
below, a runnable with no access left out of m and the `n/a` cases, are its own. A component whose
runnables all work on the same data scores high, whether that data comes through a port or is a
variable they share; one whose runnables share nothing scores 0, a hint that it may be two
components in one. The report gives the mean over the components that have a figure and
names those below 0.5. That threshold is a starting point taken from the source of these metrics and
calibrated against nothing, which the report says wherever it uses it.

**A runnable that declares no access** says nothing about what it touches, so it is left out of the
pairs rather than counted as sharing nothing; an initialization runnable is the usual case. The
report says how many runnables the figure rests on.

**When it is `n/a`.** Per component: it has no runnable; none of its runnables declares an access,
to a port or to a variable; only one of several does; or two or more do and all touch one and the
same port or variable, which would make the figure 1 by construction (a runnable that touches
neither, such as an initialization runnable, is left out of that case too). A component whose only
runnable declares an access scores 1. For the whole section: no component's runnables declare an
access — `reads`, `writes`, `calls`, `reads_variables` or `writes_variables` on runnables would
answer it — or some do, but no component's runnables give a figure.

**The limit.** The figure rests on the runnables that touch a port or a variable in the components
that have a figure; the report says how many those are, of how many runnables the declared components
hold (an external stub's runnables are not counted here, though *Size* counts them), and how many
components have no figure.

### Coverage { #coverage }

<!-- purpose: Reading the Coverage section: safety levels, runnables, tasks and requirement links -->

**Terms:** Coverage; n/a.

The long form; each row has its own whole:

- **ASIL** (the safety level, from QM, no safety level required, up to D) — over the declared
  components: those that declare a level, QM included; those that inherit one (they declare none, and
  the highest level of the requirements allocated to them is above QM, QM(D) counting as QM); and
  those with neither.
- **Components with a runnable** — over the declared components.
- **Runnables on a task** — over the declared components' runnables that are not server runnables. A
  server runnable is one an operation starts and no timing event does; it runs in its callers' tasks
  when it is called directly, so being on no task is its normal state, and the report excludes them.
  A runnable a data reception starts (a `data` event, no timing and no operation event) is counted:
  nobody calls it from another task, so it needs a task of its own. So is a runnable a returning
  asynchronous call starts (a `call_returned` event, no timing, operation or data event): nobody calls
  it either.
- **Requirements allocated** — over the requirement ids no stub requirement holds, a stub
  requirement being a placeholder an import created. The classification is the id's: an id a stub
  requirement holds is excluded with every file that declares it, a requirement that is not a stub
  included, and the row counts those ids (*1 id held by a stub requirement, a placeholder,
  excluded*). An id is allocated when some element satisfies it. When every id is held by a stub
  requirement, the sentence says so: *Every requirement id is held by a stub requirement, a
  placeholder, so none counts as allocated*.
- **Components with a requirement link** — over the declared components, a link of their own or on
  one of their ports or runnables to a requirement the model declares, a stub requirement included. A
  link naming an id no file declares counts for none, so after a partial requirements import a
  component traced only to requirements not yet imported is named under *no link to a declared
  requirement*.

Each sentence names what is missing: the components without a level, the runnables on no task, the
requirements not allocated and the components with no link to a declared requirement. The task
sentence says *in the declared components*, since an external stub's runnables are not read, and when a
declared component has a server runnable it says *non-server runnables*, the ones it counts: *the only
non-server runnable is on a task*.

**When it is `n/a`.** A coverage is a number when the declared components and the requirements hold
that kind of data, and `n/a` when they hold none of it: *0 of 86 runnables on a task* is true of a
model that uses tasks and false of a model that has none yet. So: no declared component or
requirement states a safety level; no declared component declares a runnable; the model has no task,
or no runnable on a declared component; the model declares no requirement. Each says so, and what
stays unanswered. An external stub is not looked at: a level or a runnable that sits on a stub alone
leaves its coverage `n/a`. A component name declared in two or more files is read off the one file
*Size* names, its last declared file, here as in every other section.

**The limit.** A task entry that names an instance marks that runnable covered for its whole type,
since a type counts once.

### Integrity { #integrity }

<!-- purpose: Reading the Integrity section: the findings and the unconnected ports -->

**Terms:** Findings; Unconnected port.

The long form: the errors and warnings of `swcraft check-model`, counted by kind of check (the word
before the first dot of a check's name: `connector`, `safety`, `trace`, …), and the component ports no
connector or delegation reaches. The metrics add no check of their own: a finding here is the same
finding `check-model` prints, and the unconnected ports are the same ones the assistant reads. The
sentence says the findings come *most from* a kind of check only when it holds more than half of
them; otherwise it names the largest share and its count, *the largest share from hardware rules (3 of
10)*.

**The limit.** Ports of external stubs and of composition boundaries are left out, and a port
delegated to an unwired boundary port counts as connected.

### Movement { #movement }

<!-- purpose: Reading the Movement section: the main figures at an earlier revision and now -->

**Terms:** Movement.

The long form: one column per revision and one for the working tree, and the change from the base
(the first revision named) to now. The rows are six size rows (components, external stubs,
compositions, ports, connectors, runnables), the coupling (with **Dependencies** first), cohesion,
coverage and the findings by class; a coverage row shows a count, not a share. A ratio's change is the difference of the two figures
as printed, so `1.67 → 1.83` reads `+0.16`. Below the table: the components added and removed, by
name; the hubs gained and lost (a hub whose own Ct did not move says so, and that the threshold
moved); and the findings introduced and resolved, with how many of the base's findings are still
open. When none of these differs, the section is one sentence: `No figure this report follows changed
since HEAD (a1b2c3d4).` The dependencies "held" only when every revision of the series has the same
total; equal at both ends and not between, they are "back at" the base's total.

What *Movement* does not follow: the other size rows (interfaces, data types, tasks, delegations,
the nesting depth and the rest), the composition tables, which components are isolated, the
unconnected ports and the names behind a coverage count. A change only there reads as no figure
changed; the *State* section shows the working tree's figures for each.

A figure that is `n/a` at one side has no change, and the cell says at which side.

**When `HEAD` cannot be read** (with `--since-head`, and the panel's default), the section is one
sentence giving the cause, the rest of the report stands, and the run succeeds:

| Sentence | Cause |
|---|---|
| `Nothing to compare: git could not be run.` | git is not installed, or is there and may not be executed |
| `Nothing to compare: HEAD's files could not be unpacked.` | git gave the files of the last commit and they could not be written to a temporary folder: none usable, one that may not be written, or a full disk |
| `Nothing to compare: the project is not in a git repository.` | no repository, which is also what git says of a `.git` file that points nowhere |
| `Nothing to compare: git refuses to read the repository the project is in.` | git finds a repository and will not use it, for example one owned by another user, or a `.git` file it cannot open or parse |
| `Nothing to compare: the current branch has no commit yet.` | a repository with no commit yet, or a new branch with none of its own (`git switch --orphan`) |
| `Nothing to compare: HEAD holds no model yet.` | the last commit holds no model directory |
| `Nothing to compare: HEAD keeps the model in a retired layout.` | the last commit stores the model in a layout this release no longer reads |
| `Nothing to compare: a model file at HEAD does not parse: …` | a model file in the last commit is broken; the rest names it |

The sentence says where the dependencies changed most among the components present at both
revisions; an added or removed component is named in the sentence on components, and when one of
them moved too the sentence says *among the components present at both revisions*.

**The limit.** A renamed element reads as one removed and one added. Both revisions are read with
the working tree's `swcraft.yaml`. A finding is the same at both revisions when its rule and its
message are, counted per occurrence, so a finding whose message changed — it names a renamed
element, or a count that moved — reads as one resolved and one introduced. The command does not date the revisions: the series is in
the order you name them.

### What to look at { #what-to-look-at }

<!-- purpose: Reading What to look at: the facts that name a place, in a fixed order -->

**Terms:** What to look at.

The long form: each line is present only when it has something to say, and every list stops at ten
names and counts the rest. The order is fixed, and it is not a ranking:

1. **Errors**, by kind of check.
2. **Hubs.**
3. **No link at all** — components with Ct 0, each with one word of fact: *no port*; *incomplete*
   (it has an unconnected port); *connected, no measured dependency* (its connectors give no measured
   dependency, see the coupling limit); *delegated, reaches no one* (its ports are connected
   only by delegations to a composition boundary that leads to no other component). None of them says *by design*: the model does not
   record intent.
4. **Statistically isolated** components.
5. **Cohesion below 0.5, not calibrated.**
6. **Runnables on no task** — periodic, initialization, data-received, call-returned and event-less
   runnables, when the model has tasks.
7. With a base: **New hubs**, the components whose Ct **rose most** (those at both revisions), and
   the **Findings introduced**.

**The limit.** Each line is a place to look, not a defect.

## How a conclusion is computed { #how-a-conclusion-is-computed }

<!-- purpose: How each sentence of the metrics report is computed from the figures, the same way every time -->

Every sentence in the report is computed from the figures by fixed rules, in the same words for the
same figures. Nothing in it is written by AI, and nothing depends on the machine, the clock or the
language settings: the same model gives the same report, byte for byte.

- A sentence states a fact and what it means, and names the components it is about — at most three,
  then how many more: *A, B, C, and 2 more*. Every list of the report counts its rest the same way,
  a table cell after ten names and a line of *What to look at* after its entries (*A (no port); B
  (incomplete); and 3 more*). When several components tie for *the most* or *the highest*, it names
  each.
- A share reads *One component in 8* when it reduces to one in a whole number, and *3 of 42
  components* otherwise. A percentage is rounded half-up; a change too small to show reads *by under
  1 %*, and a change from zero reads *from 0 to 12*.
- Where a figure is `n/a`, the sentence says what the model lacks and which question that leaves
  unanswered.
- **No sentence judges.** There are no words such as *good*, *bad*, *healthy* or *risky*, because
  nothing calibrates a coupling or a cohesion figure across projects. A hub is defined by this model's
  own spread; the cohesion threshold is labelled uncalibrated wherever it appears.

A conclusion knows nothing of your intent. A component left without links on purpose reads as one
without links; the report tells you where to look, and you decide what it means.

## The JSON document { #the-json-document }

<!-- purpose: Reading the metrics as a program: the fields of the JSON document and its version -->

`--format json` prints one document for a program. It starts with `"document_version": 1` and
`"kind": "architecture-metrics"`; a program should refuse a version it does not know. `answers`
holds the two answers, `state` the figures of the working tree with every component's row and every
composition table, `movement` the comparison (or `null`), `no_base` the sentence of a `HEAD` that
could not be read (or `null`), `look_at` the lines, and `conclusions` each section's sentences and
limit. A ratio is the printed two-place string (`"1.83"`), and a figure that is `n/a` is
`{"not_available": "<reason>"}` where its value would stand. *Definitions and method* and the
sections' *Terms* lines are not in it: they are the same for every model.

## Through the assistant { #through-the-assistant }

<!-- purpose: How the assistant's architecture_metrics answer differs from the command's document -->

The assistant's `architecture_metrics` tool runs the same computation and answers a bounded form of
the document: every list that grows with the model is `{"first": [at most ten], "more": n}`, and the
per-component rows and the table per composition are the command line's alone;
`state.size.compositions` counts the composition files. With `since` (a tag, a commit, `HEAD`),
`movement` holds each figure at both sides and its change, the components added and removed, the
hubs gained and lost, and the findings introduced and resolved; without it, `answers.heading` says
no base was named. An answer too large for one reply, an error text included, is refused, naming
`swcraft metrics`, which prints the whole report or the whole error.

## In a pipeline { #in-a-pipeline }

<!-- purpose: Keeping the metrics in a pipeline as a record over time, without failing the job -->

The command never fails a pipeline over its figures, and it is meant not to: read its numbers, not
its exit code. To keep a record over time, run it with `--since` against the release you compare with
and keep `--report`'s file as the job's artifact.

## What it does not reach { #what-it-does-not-reach }

<!-- purpose: What the metrics cannot tell you, so you know where the report's answer ends -->

- **It is no gate and no qualification evidence.** `swcraft validate` decides; this report describes.
- **A type counts once.** Two placements of one component type are one node.
- **A delegated port that leads nowhere counts as connected** in the unconnected count.
- **The thresholds are this model's own statistics**, and the cohesion threshold of 0.5 is a starting
  point, not a calibrated limit.
- **A rename is a removal and an addition** in *Movement*, and both revisions use today's manifest.
- **The conclusions are in English only.**
