<!-- draft: not yet reviewed by the product's author -->

# Command line

<!-- purpose: Choosing the right `swcraft` command for a task, and reading what its answer means -->

Everything SWCraft does is a `swcraft` command. The editor's buttons are these commands with
the arguments filled in, the build pipeline runs these commands, and over SSH they are all
there is. This page is about which one to reach for and what its answer means.

Every option of every command, with its default, is on
[The command line](reference/cli.md) in the reference section — generated from SWCraft's own
definitions, so it describes the version you have rather than the version this page was
written against. This page does not repeat it.

## One shape { #one-shape }

<!-- purpose: The form every command takes, and how to be sure which installation answered -->

```sh
swcraft check-model --project swcraft.yaml
```

A command, then `--project` and the path to the project's `swcraft.yaml`. The path may be
relative or absolute, and everything inside the manifest is interpreted relative to the
directory holding it — so the same command works from anywhere. `swcraft init` is the one
exception: there is no project yet, so it takes a directory instead.

A bare `swcraft` is a path lookup, and on a machine that carries other environments it can
resolve to one of them. There is exactly one SWCraft installation, so when you want to be
certain which one answered, name it:

```sh
$HOME/.swcraft/current/bin/swcraft list --project swcraft.yaml
```

That is the installation itself — `%LOCALAPPDATA%\SWCraft\current\Scripts\swcraft.exe` on
Windows — and it is what the VS Code extension runs, which is why a shadowed path shows up
there as a warning rather than as wrong answers. `swcraft --version` from a fresh terminal is
the quicker version of the same question.

## On Windows { #on-windows }

<!-- purpose: When a report's characters come out garbled in Windows PowerShell, and how to fix it -->

Every `swcraft` command writes UTF-8, on every platform, and a report carries characters beyond
plain ASCII — the `×` of the import report's grouped list, the dashes of a check's message. A
console shows them correctly. **Windows PowerShell 5.1 does not, once the output goes through a
pipeline or a redirection** (`| Tee-Object`, `> file.txt`): it decodes a program's output with the
console's legacy code page, so `×` arrives as `├ù` and a dash as two or three stray characters.
PowerShell 7 and a console already set to UTF-8 read it correctly.

Two answers work. Where the command has its own `--report FILE`, let it write the file — it is
written as UTF-8 whatever the console does:

```powershell
swcraft import model --source export.arxml --project swcraft.yaml --report import-report.txt
```

Otherwise, tell PowerShell once, in that session, to decode as UTF-8 before the pipeline:

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
swcraft check-model --project swcraft.yaml
```

## The two commands that grade { #the-two-commands-that-grade }

<!-- purpose: Checking the model and validating the build: the two commands whose exit code is a verdict -->

These are the ones whose exit code is a verdict. Both exit 0 when the architecture is sound
and non-zero when it is not, and both report *every* problem they find rather than stopping
at the first. Sound means free of every check that is an error; a check that is a warning is
reported too and changes no verdict, as the paragraph on warnings below says.

```sh
swcraft validate --project swcraft.yaml
```

**`validate` is the gate.** It reads the model, runs every check, generates the ARXML and
checks that ARXML against the AUTOSAR XSD, the standard's schema — or structurally, saying so in
its output, when no licensed schema is configured on the machine. Nothing else in SWCraft is authoritative
about whether the architecture is allowed out, and nothing in the editor is: a file with no
red squiggles in it can still be refused here, and that is the two surfaces doing different
jobs rather than disagreeing.

**A failing run is not tidy, and it is worth knowing which kind of failure you have.** A model
problem stops the run before anything is generated, so there is nothing on disk to mistake for a
good file. A problem the *schema* check finds is different: by then the AUTOSAR file has already
been written — the run names it, on the line above the failure — and `validate` does not remove it
afterwards. What is sitting there is the file that was refused, not one you can hand on. Writing it
is still all-or-nothing, so it is a complete file rather than a half-built one; that is exactly what
makes it easy to mistake for a good one. Nor does a failure tidy up after an *earlier* success —
removing previous outputs is a build's clean step, not `validate`'s — which is why a pipeline cleans
first, and why the file timestamp is worth a glance when you are staring at output you did not
expect.

```sh
swcraft check-model --project swcraft.yaml
```

**`swcraft check-model` writes nothing at all.** It answers the model half of the same question and
stops before generating anything. That makes it the fast one to run while you are editing —
and the one a build pipeline runs *first*, because `validate` would rewrite the very ARXML the
next build step is about to check.

`check-model` is not a command that always succeeds. It grades, and it exits non-zero on a
model with a problem in it, exactly as `validate` does. Writing nothing and judging nothing
are different properties, and this command has only the first.

**A warning is reported, never a refusal.** Not every check is an error: a requirement that
no element satisfies is a warning, and [The checks](reference/rules.md) says of every check which
of the two it is. Both commands print their findings on the error stream, every `ERROR:` line
first and then every `WARNING:` line, so a run you redirect keeps them together with the rest of
its output, and then one line counting both, such as `1 error(s), 2 warning(s).` — printed only
when there is a finding, so a clean run prints nothing there. A run whose only findings are
warnings still ends with its success line and exits 0: `validate` generates the ARXML and checks it exactly as on a clean model, and `check-model` writes
nothing as always. A warning is a gap to close, not a reason to hold the file back — which is the
right verdict while you are editing, and the wrong one for a release. For that there is one flag,
on these two commands and no other:

```sh
swcraft validate --project swcraft.yaml --warnings-as-errors
```

With it every warning counts as an error: each line is still printed as `WARNING:` — the prefix
names the check's class, the flag changes the verdict — the run ends, after the count, with one
line naming the verdict, `Error: 1 warning(s) treated as errors (--warnings-as-errors).`, and
exits 1; `validate` then
generates nothing, exactly as on an error. A pipeline that must not let a known gap through runs
its gate with the flag; a developer's build without it. Every other command refuses the flag as
an option it does not have.

For a program, `check-model --format json` prints the same findings as one document on standard
output — each finding's check, whether it is an error or a warning, its message and the elements it
names — and nothing else there: no success line, so a run whose only findings are warnings prints
the document alone. The error stream and the exit code are the text form's, and a run that cannot
read the project prints no document. The VS Code extension reads this document to fill its
Problems panel.

**A model file that cannot be read stops the run before any check**, `check-model`, `validate` and
every other command that reads the model alike, and exits 1. The error stream gives one line per
field at fault, each naming the file, the field and what is wrong with it:

```text
Error: Invalid task YAML in <project>/model/tasks/Task_bad.yaml: period: Input should be a finite number
Error: Invalid task YAML in <project>/model/tasks/Task_bad.yaml: priority: Input should be a valid integer, unable to parse string as an integer
```

A field inside a list is named with its position, as `ports[0].interface`; a rule about the element
as a whole has no field; a line the YAML reader cannot parse is named by its line and column, as
`line 2, column 9: mapping values are not allowed in this context`. The run stops at the first such
file, so a second one is reported once the first is fixed. A command that reports the refusal
inside a sentence of its own — `swcraft diff`, `swcraft change-report`, `swcraft metrics` — gives the same
lines joined by `; ` on one line.

### The build report { #the-build-report }

<!-- purpose: Writing a build report that lists every finding, for a review or a record -->

```sh
swcraft validate --project swcraft.yaml --report
```

With `--report`, `validate` also writes a short Markdown document of the run, `build-report.md`,
in the manifest's `dist_root` — `dist/build-report.md` in a scaffolded project — and prints
`Generated report:` with its path. **It changes no verdict**: the exit code is the same as without
the flag, and so are the ARXML and every other line the run prints. It is a document for a person
to read, computed from the model and the files on disk and written by no AI, and it is not
qualification evidence: the exit code is the verdict. If the report cannot be written, the run
prints `Warning: the build report was not written:` with the reason on the error stream and exits
exactly as it would have without the flag. If its path holds a control character — a `dist_root`
with a line break in it — the report is written but the `Generated report:` line is replaced by a
warning on the error stream, so no line of the output can be mistaken for another; the experiments
and `change-report` do the same.

**When it is written.** Once the run has generated the ARXML — whether the check of that ARXML then
passes or fails, because the refused file is on disk by then and the report says so. Not when the
run refuses before generating anything: an error in the model, `--warnings-as-errors` with a
warning, a manifest it cannot read, a `toolchain_version` that does not match — or a run whose
ARXML could not be generated. An earlier report under that name is then left as it is, as the
earlier ARXML is. Each run that writes the report replaces it whole; it carries no timestamp and
names every file by its path inside the project — a file the manifest places outside the project
by its file name alone — never by where the project is stored.

**What it holds**, in five sections:

- **Verdict** — one sentence: passed against the AUTOSAR schema, passed with the structural check
  only (XSD validation was skipped, and the sentence says so), failed, or not validated because
  the schema could not be used — then the number of warnings. The check is named in every case, so
  a structural pass never reads as a schema check. Why a check failed is not copied in: the run's
  own output gives the reasons.
- **What was built** — the ARXML (written by this run, with its AUTOSAR version), the diagrams'
  `index.html` under `views_root` and the `needs.json`, each *on disk* or *absent*. *On disk*
  says the file is there, not which run wrote it. After a build that starts with `clean-outputs` —
  VS Code's **Build**, `moon run :build` — every file listed is that build's own, because its first
  step removed them all. After `swcraft validate --report` on its own, only the ARXML is this run's;
  the diagrams and the `needs.json` are whatever an earlier command left.
- **Findings** — the model check's warnings, each with its check's name. Never an error: an error
  stops the run before the report.
- **Resources** — every core and memory with its use, limit, headroom, physical headroom and status,
  a memory's limit and headroom in whole bytes, rounded down, where the text of
  `swcraft resource-report` prints the exact figure ([the report file](#the-report-file)). The
  elements that use each one are in that report and in the VS Code **Resources** view, not here.
- **ARXML changes** — the ARXML elements added, removed or changed against the previous ARXML,
  the ARXML's own name with `_old` before its extension, beside it (`dist/model_old.arxml` in a
  scaffolded project), one row each. Or one sentence instead: no previous ARXML is kept, so nothing
  is compared; the previous file cannot be read, is no XML document, or is one whose elements cannot
  be put in canonical form, so it is not compared; the generated ARXML is no XML document — its
  check failed, and the verdict says so — so nothing is compared; or no element changed. When the two files declare
  different schemas — after a run with `--arxml-version`, for one — a further line names both, each
  schema by its file name, and the elements are compared all the same.

**What the previous ARXML is.** It is whatever the last move aside left, and the report does not
know its history:

- after a build that starts with `clean-outputs`, and when the manifest places the ARXML directly in
  `dist_root`, it is the ARXML that first step moved aside — the last build's that wrote an ARXML. A build that stopped before generating the ARXML left none, so
  the next build moves nothing and `model_old.arxml` stays what it was. A model edited while such a
  build runs, after its `generate arxml` step and before its `validate` step, makes `validate`'s
  ARXML differ from the one that step wrote, so that one is moved aside instead, and the changes are
  then counted from it, not from the last build;
- after `swcraft generate arxml` or `swcraft validate` on their own, it is the last ARXML whose
  content differed from the one then written, since an identical file is not moved. Running
  `validate --report` twice on an unchanged model therefore compares with the same older file both
  times — or, when there is none, says that no previous ARXML is kept;
- an ARXML the manifest places in a subdirectory of `dist_root`, or directly in `views_root`, is
  not moved aside by `clean-outputs`, which removes that directory with everything in it, and a
  `dist_root` inside `views_root` loses the kept copy with `views_root`; a build of such a project
  never has a previous ARXML to compare with;
- an ARXML the manifest places outside both `dist_root` and `views_root` (`artifacts/model.arxml`)
  is neither moved nor removed by `clean-outputs`, so a build of such a project is the case above of
  `swcraft generate arxml` or `swcraft validate` on their own: the previous ARXML is the last one whose
  content differed, and an unchanged model compares with the same older file every time.

**What the comparison does not reach.** It compares each element inside a package, found by its
path of short names, by its exact XML content, and knows nothing of what AUTOSAR means by it: a
reference list in another order is a change. The package tree itself is not compared, and neither
are the component exports, the diagrams or the `needs.json`.

Only `validate --report` writes the report. VS Code's **Build** and a scaffolded project's
`moon run :build` pass the flag; `validate` without it, and by default the assistant's `build` and
`validate` tools, write none.

**Through the assistant.** By default the assistant's `validate` tool runs this same gate in its own
process — `check-model`, then, for a valid model, the ARXML generated and checked — so it and the
command cannot reach opposite verdicts on one model, and a project that is only a `swcraft.yaml` and
a `model/` needs no build tooling for it. Its answer is `{"success", "errors", "warnings",
"output"}`: `warnings` holds every finding whose check is a warning, on every answer the run
produced, a refused model and a rejected ARXML included, and `[]` when there is none or when the
answer is built from an error the run raised. Setting `SWCRAFT_MCP_VALIDATE_COMMAND` in the server's
environment runs the project's own validation command instead (`moon run :build` in a scaffolded
project); its text is then the `output`, and `warnings` is `[]`. The ARXML version is the
manifest's, with no override per call.

## The six commands that only look { #the-six-commands-that-only-look }

<!-- purpose: Asking about the model without changing it: list, query, diff, impact, resources and metrics -->

```sh
swcraft list --project swcraft.yaml
swcraft query --project swcraft.yaml base_types --where encoding=IEEE754
swcraft diff --project swcraft.yaml
swcraft impact --project swcraft.yaml IVehicleSpeed
swcraft resource-report --project swcraft.yaml
swcraft metrics --project swcraft.yaml
```

These six report. **On a successful run they always exit 0**, however uncomfortable the
answer — a resource report that is 40% over budget exits 0, an impact query that finds
nothing exits 0. They are instruments, not gates, and a build pipeline that treats a clean
exit from one of them as a verdict is reading a thermometer as a diagnosis.

The converse still holds, and matters: a project one of them could not *read* — no manifest,
a model root that is not there, a single element file that does not parse — is a failure and
exits non-zero. "Exits 0 when the answer is uninteresting" is not "exits 0 when it could not
answer".

**`query` has one more way to fail than the other five, and it is not about the model being
uninteresting.** It exits non-zero on a request it cannot answer, and that comes about in two
ways. The model's **vocabulary** may not admit the question — a `--where` or `--exists` naming
a field no element in that layer declares, a `--members` naming something that is not a nested
list, an `--after` cursor that is not in the slice you asked to continue, and in a request file
a layer that does not exist, a computed value `query` does not know or asks for in a layer it
does not belong to, a filter on a computed value the request does not ask for or on a part of it
the value does not carry, or a join path that names nothing a row carries. Or the **arguments**
themselves may not be a question — a `--limit` or a `--depth` below 1, an `--after` with no
`--limit` to continue, a `--request` file that cannot be read or is not a valid request, a
`--request` beside a layer name or any other slice or walk option, and the combinations that
mix a layer slice with a walk (`--where` beside `--dependents`, a `--depth` on a layer, neither
of the two given or both). Either way
that run read the project perfectly well and produced no answer, which is a different thing
from an answer you did not like, and it is reported as the error it is rather than as an
empty result.

What each is for:

- **`list`** prints every declared element by layer, and takes an optional layer name to
  narrow it. It is the quickest way to find out what a name is actually spelled like. The seven
  layers under `data_types/` are `application_types`, `implementation_types`, `base_types`,
  `mapping_sets`, `units`, `compu_methods` and `data_constraints`, and `impact` and `query` name an
  element of one by the layer's word in the singular — `implementation_type float32`,
  `base_type float32_base`.
- **`query`** answers the narrower questions `list` cannot: *which* elements, and *how many*.
  It filters one layer by declared field — `--where FIELD=VALUE` for equality and
  `--exists FIELD` for "this element carries a value for it" — a field left empty, as an
  unwritten `description` is, counts as carrying none. Both are repeatable, and an element
  has to satisfy every clause you give to appear. It slices one nested list across a whole
  layer with each member's owner beside it (`--members ports`), and a list nested inside
  another one when you write the path to it: `--members runnables.writes` lists every data
  element every runnable writes, each with its component and, under `through`, the runnable.
  And it pages, so a layer with
  a thousand elements answers in windows: `--limit N` prints the first N and the cursor to
  continue from, `--after CURSOR` takes the next. Each row is the element's or member's name;
  `--form full` prints every field its kind declares under it as well — a field you have not set
  as its declared default (`false`, `""`, `[]`, `{}`), or as `null` where the field declares none,
  a nested list as one line of JSON — so `--members runnables.calls --form full` shows
  each call's operation, timeout and the rest. That is the answer the assistant's `read` tool
  gives at `form: "full"`; with `--format json` the document's `schemaVersion` is 5 and it carries
  `"form": "full"`. `--dependents NAME` and `--deps NAME` walk
  the references in either direction — what points at this, and what this points at — with
  `--depth N` to stop after N steps. The one filter the options cannot spell is the negative
  one — there is no option for "this field is *absent*" — but a request file can (below).
- **`diff`** compares two revisions of the model in the model's own vocabulary — elements
  added, removed and changed — rather than as YAML lines. Its arguments read like `git diff`:
  no revisions compares the last commit with your working tree, one compares that revision
  with the working tree, two compare the two revisions. To see what a change *does* — findings,
  requirement links, resources, the overviews before and after — run `swcraft change-report`, which
  takes the same revisions and writes a report: [The Change Report](change-report.md). It writes
  files and can fail a pipeline, so it is not one of these six.
- **`impact`** answers the question you have before a rename or a deletion: *what points at
  this?* It follows references transitively across every layer, so an interface reports the
  ports typed by it, the connectors carrying it and the tasks scheduling the runnables involved.
  Asked of a requirement, it lists the elements whose `satisfies` name it — a link on a port or a
  runnable reaches the component that owns it — and the requirements derived from it; a component
  no longer lists its requirements, since it is the component that names them. Each line names one
  dependent and **every** port,
  runnable or step through which it was reached — `swc BrakeController (via ports
  pBrakeRequest, pBrakeRequestAux)` for a component with two ports on the interface. A
  connector's line names each of its endpoints whose component or composition the report
  reached, owner first — `connector Brake_link (via ports BrakeController.pBrakeRequestAux,
  BrakeConsumer.rBrakeRequest)` — so read the ports on a line rather than counting lines: a
  connector carries the interface when one of its endpoints is a port that its owner's own line
  names. On a large model, `--depth 1` stops at the direct dependents and is the first look that
  fits a console; a bounded report ends with a `(depth: N)` line, and a `--depth` below 1 is
  refused the way a mistyped option is, before anything is read. Two limits are worth knowing.
  With `--depth N`, an element N steps out is listed but not walked further, so what leads out
  of it is not on any line: a connector is two steps from an interface, and its endpoints are
  named from `--depth 2` on. And a connector the report reaches only through the composition it
  is drawn inside names no port — none of its endpoints' owners was reached, so it does not carry
  the interface. When two elements reach one line with the same value — two components each
  mapping a runnable named `Run` into one task, or charging the same budget to one memory — the
  line names both, each after the element it came from: `runnables SwcA.Run, SwcB.Run` — and
  with its layer too where the two elements share a name, as a component and a composition may:
  `via ports swc X.p, composition X.p`.
  It is the report to read and to paste into a review; `query --dependents` walks
  the same references and prints a plainer listing, which is the one to reach for when you are
  going to feed the answer to something else. For a program, `impact --format json` prints
  exactly the document `query --dependents --format json` prints, each row carrying the
  rendered detail and the same details as a list of `kind` and `value` pairs: `kind` is one of
  `port`, `endpoint` (a connector's, its value `Owner.port`), `runnable`, `member`, `step`,
  `budget`, `core`, `access` and `partition`, and `value` is always a string — a memory budget's `"512"`
  included. A detail named after the element it came from also carries `owner`, that element's
  name, and its `value` stays the plain value; one named with its layer also carries `ownerLayer`.
- **`resource-report`** prints one block per declared core and memory: its budget, how much
  of it is used, the headroom left to the budget and, on a `PHYSICAL HEADROOM` line, to the whole
  core or the memory's whole size, and the tasks or components consuming it — including how
  many of them declared no budget at all. A core or a memory set to `advisory` is reported
  the same way and says so in its heading. The blocks are budgets and only budgets, so a task
  whose `partition` names a partition no hardware file declares, or whose partition names a core
  none declares, is in nobody's arithmetic: it is in no block, and no total moves because of it.
  It is named in the report's `TOPOLOGY` section instead — see below. A model with a runnable
  started by a `data` event also gets an `ARRIVALS` section, which says where each such runnable's
  data comes from, and a model with an implicit read or write an `IMPLICIT ACCESS` section, which
  says which tasks hold them.
- **`metrics`** answers *what state is the architecture in, and where is it heading?*: size,
  coupling between components and inside each composition, cohesion, coverage and the findings, each
  explained in a sentence computed from the figures, and with `--since REV` or `--since-head` how
  they moved since that revision. It has [a page of its own](metrics.md). One difference from the
  others: a revision you name with `--since` that cannot be read fails the run, because a comparison
  with a silently missing side would mislead; with `--since-head`, a `HEAD` that cannot be read is
  said in the report and the run succeeds.

`diff`, `impact`, `list`, `query`, `resource-report` and `metrics` also take `--format json` for a
pipeline that wants to read the answer rather than show it. None of them writes a file, with two
exceptions you ask for: `resource-report --report` ([the report file](#the-report-file)) and
`metrics --report`, which writes `metrics/metrics.md` by the same rule.

### A whole question in one file { #a-whole-question-in-one-file }

<!-- purpose: Asking a whole query from one JSON file, the same request the assistant's read tool takes -->

`swcraft query --project swcraft.yaml --request table.json` reads the whole question from a JSON
file instead of from options: the request the assistant's `read` tool takes, without its
`operation`, addressed to one layer — save its `count` and `summary` forms, which `query` refuses:
count the rows an `index` request prints. `"form": "full"` asks what `--form full` asks. It is
given alone — beside no layer name and no other slice or walk option. A file that asks only what
the options can ask prints exactly what they print,
and it can also ask the one thing they cannot: the `where` clause
`{"field": "description", "exists": false}` keeps only the elements that do **not** carry a
description — whether the file leaves it out or writes it empty. An empty text counts as no value;
`0`, `false` and an empty map are values.

Two more keys go further than the options, and a request carrying either must say
`"form": "index"`:

- **`derived`** asks for values SWCraft computes from the model rather than reads from a field.
  `connection_ends`, on `connectors`, is each connection's providing and requiring component and
  port, followed through compositions down to the components that hold the ports; a connector
  that fans out through a composition gives one row per pair of ends. `base_type`, on the data
  elements runnables read or write (`--members` `runnables.reads` or `runnables.writes`), is the
  implementation type the element resolves to — through the component's mapping set where the
  element has an application type — and that type's base type; either is `null` where the model
  does not determine it, and the row is kept. Five more answer the safety, partition and
  connection questions. `effective_level`, on `swc`, is the level a component runs at: its own
  `asil`, else the highest level a requirement allocates to it, `QM` when there is none — and its
  `rating`, the plain level (`B` for `B(D)`). `partitions`, on
  `swc`, is each partition whose tasks run one of the component's runnables, with that
  partition's core (`null` where the core is not declared), in name order; a component no
  partition runs has no row. `crossing_ends`, on `connectors`, is each pair of ends
  `connection_ends` gives whose components run in different partitions, each end with its
  partition; a connection that crosses nothing has no row, so the answer is exactly the
  connections that cross a partition. Both partition values read component types, as the
  partition checks do: two placements of one type are not told apart. Asking for
  `effective_level` and `partitions` together gives one row per partition, each carrying the
  level. `unconnected`, on `swc` with `"members": "ports"`, is one row per port that no connector
  end and no delegation reaches, with its direction and interface — read by component type too.
  `core`, on `tasks`, is the task's partition and that partition's core, `null` where it does not
  resolve. The components no partition runs are the layer's index minus the components a
  `partitions` request returns. Three more answer the traceability questions, on `requirements`,
  by the same reading `check-model`'s trace warnings use. `realised_by` is one row per
  `satisfies` entry naming the requirement, anywhere in the model: the layer, the element, the
  port or runnable that holds it (`null` for the element's own list), its kind (`swc`, `port`,
  `runnable`, `interface`, `composition`, `task`, or the hardware element's kind) and the version
  it states (`null` when it states none). `allocation` is how many such entries there are, on how
  many elements, and whether the requirement is allocated (`false` when nothing names it, `null`
  for a stub). `stale_links` is one row per entry whose version is not the requirement's current
  one, or states none, with that current version.

  A `where` clause may name a computed value the request asks for, as `derived.NAME.PATH` —
  `{"field": "derived.effective_level.rating", "equals": "B"}` keeps the components rated B, their
  decompositions included, and `{"field": "derived.allocation.allocated", "equals": false}` the
  requirements nothing satisfies; it is `equals` and `exists` only, and a clause naming a value the
  request does not ask for, or a path the value does not carry, is refused. So is `exists: false`
  or `equals: null` over `connection_ends`, `partitions`, `crossing_ends`, `unconnected`,
  `realised_by` or `stale_links`: an element with no such value yields no row, so no clause can
  find it — the elements without one are the layer's index minus the rows the value returns, as
  above; `allocation` answers the requirements nothing satisfies directly.
- **`join`** combines each row with the rows of further reads where the values you name are
  equal, and nests the match under a name you choose. A row with no match is dropped. A path that
  names nothing a row can carry is refused before anything is read, and so is a `join` of more
  than eight entries.

The table a test team builds of every sender-receiver connection — who writes each data element,
in which runnable, and who reads it, in which — is one such file:

```json
{"address": {"layer": "swc"}, "form": "index", "members": "runnables.writes", "derived": ["base_type"],
 "join": [
   {"with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
    "on": [{"left": "element", "right": "derived.connection_ends.provider.swc"},
           {"left": "member.port", "right": "derived.connection_ends.provider.port"}],
    "as": "connection"},
   {"with": {"address": {"layer": "swc"}, "members": "runnables.reads"},
    "on": [{"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
           {"left": "connection.derived.connection_ends.requester.port", "right": "member.port"},
           {"left": "member.element", "right": "member.element"}],
    "as": "reader"}]}
```

Each row names the writing component (`element`), its port and data element (`member`), the
writing runnable (`through`), the types (`derived`), the reading component and port
(`connection`) and the reading runnable (`reader`'s `through`). With `--format json` the rows are
printed under `rows` in place of `elements`, in a document whose `schemaVersion` is 4 — every
other `query` document keeps its own, so a program that reads those is not handed a shape it does
not know. The text format prints one row per line. `limit` and `after` page the rows as they page
a layer: the cursor is the whole last row, printed as `next`, and you give it back unchanged.

What the table does not do for you: the rows are per component **type**, so a component placed
twice in your compositions joins its rows across both places; a connection to an external
component that SWCraft only knows by name has no runnable to read it and drops out at the second
join; a `null` value matches nothing, so a row that cannot state a joined value drops out; and a
value is joined by its whole text, so a join of a requirement's `id` with the members of a
`satisfies` list finds only a member written as the bare id, never one written `<id>@<version>`.
Renaming the columns, filtering to the components a team cares about and printing the values the
way a spreadsheet wants them is left to whatever reads the answer.

### The report's `TOPOLOGY` section { #the-reports-topology-section }

<!-- purpose: Reading the resource report's TOPOLOGY lines: references to hardware it cannot count -->

A budget block can only be printed for hardware that exists, so anything wrong with the
*references* between the software and the hardware would otherwise leave the report silent
about work it is not counting. Above the blocks, `resource-report` prints one line per such
finding — the same sentence `check-model` prints, with the name of the check in front of it:

```text
TOPOLOGY 1 finding(s)
  - hardware.unreachable-memory: Task 'Task_10ms' runs in partition 'PARTITION1' on core 'CORE1', but SWC 'ExampleConsumerSWC' budgets memory 'RAM0', which is not accessible from 'CORE1'.
```

A model with nothing to say there gets no section at all, which is what you should see on a
model that passes the checks.

Two things it is not. It is not a verdict: the command still exits 0 with the section in it,
because it reports and does not grade, and a `TOPOLOGY` section is a reason to run
`check-model` rather than a result that replaces it. And it is not part of the arithmetic —
the numbers in the blocks below are unchanged by it, so a core whose only task is named up
there still reads `CONSUMERS 0 task(s)`. `resource-report` is the one place these budgets are
printed: the generated requirements documentation carries no budget section.

### The report's `ARRIVALS` section { #the-reports-arrivals-section }

<!-- purpose: Reading the resource report's ARRIVALS lines: runnables started when their data arrives -->

A runnable started by a `data` event runs when its data arrives, and the model holds no rate for that
([Working with the model](model.md)). So for each `data` event of each placed occurrence of its
component, `resource-report` adds one line below the blocks, saying where that data comes from:

```text
ARRIVALS 2 line(s)
  - SpeedEstimator.FilterSpeed (R_WheelSpeed.FrontLeft): data arrives at most every 10 ms (from WheelSpeedSensor.SampleSpeed in Task_10ms)
  - SpeedEstimator.MonitorSpeed (R_WheelSpeed.FrontRight): arrival rate unknown: the source is outside the model (a writer runs in no task)
  These are bounds taken from the senders' task periods, derived from the model; they are not measured rates. Where the RTE activates the receiving task on arrival, a bound limits that task's activations from the sender's side only.
```

A line names the occurrence — its path of composition member names from the root composition, as a
task's `instance` is written — then the runnable, the event's port and element, and one of three
answers:

- **A bound.** The command follows the connectors that reach the port, through delegations down into a
  composition and up out of one, to the runnables whose `writes` hold the element, and to the tasks
  they run in. When every source ends in such a runnable mapped to a task, the line gives the
  shortest of those tasks' periods, in milliseconds, with the writer and its task, and counts the
  others (`, and 2 more`). Every task has a period, so every mapped writer gives one — whatever starts
  that writer, a `data` event of its own included.
- **Unknown**, when any source ends outside the model, because one sender nobody can predict makes
  the arrival unpredictable whatever the others do. The line names the first such case and counts the
  others. There are five: *no connector reaches the port*, *delegated out of the top composition*,
  *the sender is an external component*, *no runnable of the sender writes the element* (also where
  the sender sends by a route the model does not hold) and *a writer runs in no task*. A port on an
  external interface is followed like any other port; only an external component is outside.
- **Not derived**, where no answer can be given, with the line naming the component instead of an
  occurrence: *the composition layer has no single root*, *the component is placed in no
  composition*, *the event does not resolve* (which `check-model` reports), and, for a component
  placed more than 64 times, one line saying how many occurrences were left out
  (*the occurrences past the first 64 are not walked (3 more)*).

The header counts lines, not events: a component placed twice gives each of its `data` events two
lines. A writer that two tasks run, which `check-model` reports, is bounded by the shorter period. A
task entry naming a component placed more than once by `swc:` alone reaches none of its occurrences,
which `check-model` also reports, so a writer mapped only that way reads *a writer runs in no task*.

The closing sentence is printed once and means what it says: the figure is a bound taken from the
senders' task periods, not a measured rate, and it says nothing about response time. The line does
not compare the bound with the task the receiving runnable runs in. Like the `TOPOLOGY` section it is
not a verdict: no finding is counted for it, the numbers in the blocks do not change, and the command
still exits 0. A model with no `data` event gets no section, and its report is what it was, with one
exception: to find out whether a model has a `data` event the command reads every component file on
every run, so a component file that does not parse now stops the report with an error even where the
`hardware` folder is missing, which used to be reported alone. A model with a `data` event also has its
interfaces, connectors, tasks and compositions read, and a file among them that does not parse stops the
report the same way.
The Resources view in VS Code does not show the section.

### The report's `IMPLICIT ACCESS` section { #the-reports-implicit-access-section }

<!-- purpose: Reading the resource report's IMPLICIT ACCESS tables: which tasks hold implicit reads and writes -->

A read or a write marked `by: implicit` ([Working with the model](model.md)) works on a copy the RTE
makes for the runnable. Where that copy is made is decided per task, so `resource-report` adds a
section below the blocks that says, per task, how many implicit reads and writes its runnables
declare:

```text
IMPLICIT ACCESS (implicit reads and writes the task's runnables declare; when the RTE copies them, and what that costs, is not in the model)
  Task        Core   Implicit reads  Implicit writes  Runnables
  Task_10ms   CORE0  1               2                2
  Task_100ms  CORE0  1               0                1
  RUNNABLES IN NO TASK
    Runnable                     Implicit reads  Implicit writes
    BrakeController.Diagnose     1               0
    RearAxle/Sensor.SampleSpeed  0               1
```

The first table has one row per task that runs a runnable with an implicit access: the task, its
core, the implicit reads and the implicit writes its runnables declare, and how many runnables they
are. It counts each placed occurrence of a component, as the `ARRIVALS` section does: a component
placed twice whose runnable a task runs in both places counts twice, a runnable two entries of one
task name counts once, and one two tasks run counts in each. When no task runs such a runnable, the
table is the sentence *No task maps an implicit access.*

The second table, *Runnables in no task*, is printed only when a runnable with an implicit access
runs in no task. A row names the occurrence and the runnable, with its implicit reads and writes; a
component placed in no composition is named by itself, and its cell says *(placed in no
composition)*. A component placed more than 64 times is one sentence under the table saying how many
occurrences were left out (*the occurrences past the first 64 are not walked (3 more)*).

**The section gives no time and no memory figure, on purpose.** When the RTE fills and publishes the
copies, and whether two runnables share one, is the RTE's configuration, and the model holds no data
sizes — so a count of accesses is not a count of copies, and nothing here is a cost. It is a marker
for whoever plans a task: it says where implicit communication sits. Like `ARRIVALS` it is not a
verdict: no finding is counted for it, the numbers in the blocks do not change, and the command still
exits 0. A model with no implicit read or write gets no section, and its report is what it was. The
command reads the task, composition and connector files only once a component has an implicit
access. A call's being asynchronous reaches no part of the report. The Resources view in VS Code does
not show the section.

### Does it fit? { #does-it-fit }

<!-- purpose: Checking whether the software fits its memory and CPU budgets, with a margin you choose -->

```sh
swcraft resource-report --project swcraft.yaml --fit-bytes 12288
swcraft resource-report --project swcraft.yaml --fit-utilization 0.05
```

`--fit-bytes N` asks whether `N` bytes more fit every memory, and `--fit-utilization U` whether a
share `U` of a core more — `0.05` or `1/20` for five percent — fits every core. You ask one of the
two per run, and the answer is one more line in each block of that kind, after `PHYSICAL HEADROOM`:

```text
RESOURCE RAM (memory, size 32768 bytes, strict)
  STATUS within budget
  CONSUMPTION 14336 bytes of a 24576 byte budget (headroom 10240 bytes)
  PHYSICAL HEADROOM 18432 bytes of a 32768 byte size
  FIT 12288 bytes: fits physically, breaks the threshold by 2048 bytes
```

Two verdicts stand behind that sentence. **Within the threshold** is the budget: whether the use
plus the amount stays within the size times the threshold (for a core, within its threshold). That
is the answer to *does it fit*. **Within the size** is the hardware: whether it stays within the
memory's size, or within the whole core. They part exactly where the line above does — the amount
fits the memory and breaks its budget. The sentence is one of five:

| Sentence | Within the threshold | Within the size |
|---|---|---|
| `fits within the threshold and the size` | yes | yes |
| `fits physically, breaks the threshold by N` | no | yes |
| `does not fit: over the size by N` | no | no |
| `breaks the threshold by N; within the size is unknown: …` | no | unknown |
| `unknown: … consumer(s) declare no budget for it, …` | unknown | unknown |

A verdict is *unknown* when the figure it would rest on leaves a load out, which is a `strict`
resource with a consumer that declares no budget, and only when its answer would be a yes. A *no*
is always given, within the threshold as within the size, because a missing load can only add to
the use.

The question names no resource, so it cannot name one the model does not have, and the command
still exits 0 on every run that read the project. An amount it cannot read — a negative one, `12KB`,
or both options at once — is a usage error, exit 2, before anything is read.

With `--format json` the same report is one versioned document on standard output: its
`schemaVersion`, the finding counts, the topology records, the question (`fit`, or `null`) and every
core and memory with its figures, its consumers largest first, its findings and, on the asked kind,
the two verdicts and the sentence. A model with a `data` event adds `arrivals`, one object per line —
`occurrence`, `runnable`, `port`, `element` and `verdict` (`bounded`, `unknown` or `not-derived`), then
`periodMs`, `writer` and `task` for a bound or `case` otherwise, then `more`, the count of the others
— and `arrivalsNote`, the closing sentence; a model without one has neither key, and `schemaVersion`
stays 1, because a key was added and none changed. A model with an implicit read or write adds
`implicit` — `tasks` (`task`, `core`, `reads`, `writes`, `runnables`), `noTask` (`occurrence`,
`runnable`, `reads`, `writes` and `case`) and `notWalked` (`type`, `case`, `more`) — and
`implicitNote`, the section's sentence, under the same rule. Nothing else is printed there, so a version-pin warning stays on
the error stream; a project that cannot be read prints no document, exits 1 and says why on the
error stream. The VS Code extension's Resources view reads this document, so a memory's `limit` and
`headroom` there are in whole bytes, rounded down, as the view shows them; the verdicts and the
findings keep the exact figures.

With `--report`, the file below carries the same `FIT` line.

### The report file { #the-report-file }

<!-- purpose: Writing the resource report to a file, for a review or as a pipeline's record -->

```sh
swcraft resource-report --project swcraft.yaml --report
```

A report command follows one rule: without `--report` it reads and prints, and with it, it also
writes a file. Here the file is `resource-report/resource-report.md` under the manifest's
`dist_root`, written after the answer is printed; nothing goes into the model. The command prints
`Generated report: <path>` after the text, or on the error stream with `--format json`, so that
standard output stays one document. Each run replaces the file. It is the report VS Code's
**Resource Report** opens in a tab.

The file says what the text says, and differs from it in these ways:

- A core's table of tasks has a **Partition** column, and a second table gives each partition's
  name and its share of the core, largest first. A partition has no budget of its own, so a share
  carries no status and no verdict. A partition no task names is in neither table, and a task that
  declares no budget counts as zero, as it does in the core's total.
- The `ARRIVALS` section is an **Arrivals** section at the end of the file, the same lines with the
  closing sentence once.
- The `IMPLICIT ACCESS` section is an **Implicit access** section with the same sentence and the same
  two tables, the second under its own heading *Runnables in no task*.
- A memory's budget and headroom read in **whole bytes, rounded down**: a budget of `209715.2`
  bytes reads `209715`. That is the reading only, and the decision stays on the exact figure. The
  text printed in the terminal keeps the exact figures, as a finding and a `FIT` line do in the file
  too, so the file and the text of one run can differ by that fraction.

The file changes no exit code. A report that cannot be written is one
`Warning: the resource report was not written: …` line on the error stream, and the run still exits
0 once it has read the project. A project pinned to another SWCraft release gets its answer, the
pin's warning and that line, and **no file** and no `Generated report:` line: a mismatch stops every
command that writes, and this is the one write of an otherwise reading command.

## Generating { #generating }

<!-- purpose: Generating the ARXML, the diagrams, the Sphinx-Needs file and the test tables from the model -->

```sh
swcraft generate arxml --project swcraft.yaml
swcraft generate diagrams --project swcraft.yaml
swcraft generate needs --project swcraft.yaml
swcraft generate test-tables --project swcraft.yaml
```

Four outputs from one model, one `swcraft generate` each: the AUTOSAR file for the toolchain, the diagrams
([Diagrams](diagrams.md) is about reading them), the architecture requirements and their trace
as a Sphinx-Needs file for the requirements team
([below](#publishing-the-architecture-to-the-requirements-team)), and the tables of component
placement and runnable scheduling for the test team, which `generate test-tables` writes into
`dist/test-tables/` and joins with the test team's own columns when `--annotations` names their folder
([Test tables](test-tables.md) is about the files, the round trip and the refusals; it refuses a model
`swcraft check-model` refuses, and writes nothing then). `--annotations` belongs to `generate
test-tables` alone: any other format refuses it by name. The ARXML file and the
`needs.json` are written to the paths the manifest declares under `outputs` — the
`needs.json` to `outputs.needs`, `dist/needs.json` unless you say otherwise — overwriting what is
there; the diagrams go into `views_root`, and a run removes only the diagram
files an earlier run wrote for a view that no longer exists. None of them empties the directory
first, so a file you left in an output directory by hand survives until something cleans it.

The AUTOSAR release is a property of the project — `arxml_version` in the manifest, defaulting
to 4.2.2 — and `--arxml-version` overrides it for **one run** without writing anything back.
That is the flag for producing a 4.3.0 file from a 4.2.2 project for somebody who asked,
without turning the project into a 4.3.0 project.

**Through the assistant.** By default the assistant's `build` tool runs the same checks and
generators in its own process — the model check, the ARXML and its check, the diagrams into
`views_root`, the `needs.json` — so a project that is only a `swcraft.yaml` and a `model/` needs no
build tooling. Its answer carries `warnings` as `validate`'s does (above, under *The two commands
that grade*), and the ARXML version is the manifest's, with no override per call. Setting
`SWCRAFT_MCP_BUILD_COMMAND` in the server's environment runs the project's own build command instead
(`moon run :build` in a project made with `swcraft init --integration moon`); its text is then the
`output`, and `warnings` is `[]`.

## Publishing the architecture to the requirements team { #publishing-the-architecture-to-the-requirements-team }

<!-- purpose: Publishing the architecture and its trace to the requirements team as a needs.json file -->

`swcraft generate needs` writes one file — `outputs.needs` in the manifest names it,
`dist/needs.json` unless you say otherwise — in the Sphinx-Needs `needs.json` format. The
requirements team loads it into its own Sphinx-Needs documentation, and each of its requirements
then shows which of your architecture requirements derive from it and which architecture elements
satisfy it. Nobody runs Sphinx on your side, and the team installs nothing of SWCraft's.

**What the file holds:**

- every **architecture requirement** (`source: architecture`), as a need of type `arch_req`
  (`req_type: req`) or `arch_spec` (`req_type: spec`), with its title, its description as the
  content, and `asil` when it declares a level other than `QM`;
- every **architecture element** that satisfies a requirement or that an architecture requirement is
  derived from, as a need of type `arch_element` with its kind in `tags` (`swc`, `port`, `runnable`,
  `interface`, `composition`, `task`, `processor`, `core`, `memory`, `partition`). Its id is its
  kind and its name joined by `-` — `SWC-BrakeController`, `PORT-BrakeController-pWheelTemp`,
  `RUN-BrakeController-Run10ms`, `IF-IBrakeTemperature`, `COMP-Vehicle`, `TASK-Task10ms`, and `HW-`
  for every hardware element. A linked port or runnable is a need of its own, titled
  `<component>.<member>`; its component is written only when it carries a link itself or an
  architecture requirement names it. An element nothing links is not written;
- the **links**, each by the id your model holds: an element's `satisfies` and an architecture
  requirement's `derived_from`. A `derived_from` naming an element (`swc:BrakeController`) names that
  element's need, and is published as unconfirmed when your model holds no such element;
- on every need, **`swcraft_path`**: the file in your model that declares it, relative to the model
  root and written with `/` — `requirements/AR_001.yaml`, `swc/brake/BrakeCtrl.yaml`. A port or a
  runnable is declared in its component's file, so its `swcraft_path` is that file. It is never an
  absolute path: the team joins it to wherever the architecture project can be opened (below).

**An incoming requirement is never written.** The requirements the team delivered stay theirs: they
appear in the file only as the ids your links name. No id is rewritten — a requirement's id goes
through exactly as your model holds it.

**Confirmed or unconfirmed.** A link is published under `satisfies` or `derived_from` only when it
names the version it was confirmed against and that is the requirement's current version. Every
other link — one with no version, one whose requirement changed since, one to a stub (the
placeholder an import writes for a parent it did not receive), one to an id no requirement or two
requirements declare — is published under `satisfies_unconfirmed` or `derived_from_unconfirmed`. The
team sees *unconfirmed*, never your version number; `check-model`'s warnings name the links to
confirm. So a confirmed link always names a requirement your model imported or an architecture
requirement or element in your own file — and the team still holds the imported one as long as your
requirements come from an import of its current delivery. A requirement written by hand, or imported
before the delivery was recorded, carries no `baseline`, and nothing tells whether the team holds it.

**The delivery.** The file names the requirements delivery its links were set for: its
`current_version` is the `baseline` your imported requirements carry, the version name of the last
delivery you imported. A model with no imported requirements names none (`""`).

Two needs of such a file, an architecture requirement and a port, from a file of seven:

```json
{
  "current_version": "SYS-2.1",
  "project": "brake-architecture",
  "versions": {
    "SYS-2.1": {
      "needs": {
        "AR_001": {
          "asil": "B",
          "content": "BrakeCtrl shall forward a brake request to the actuator within 10 ms.",
          "derived_from": ["SYS_001", "SWC-BrakeCtrl"],
          "derived_from_unconfirmed": ["SYS_002", "CUST_001"],
          "id": "AR_001",
          "swcraft_path": "requirements/AR_001.yaml",
          "tags": [],
          "title": "Brake request handling",
          "type": "arch_req"
        },
        "PORT-BrakeCtrl-pBrakeReq": {
          "content": "The brake request to the actuator.",
          "id": "PORT-BrakeCtrl-pBrakeReq",
          "satisfies": ["SYS_001", "AR_001"],
          "swcraft_path": "swc/brake/BrakeCtrl.yaml",
          "tags": ["port"],
          "title": "BrakeCtrl.pBrakeReq",
          "type": "arch_element"
        }
      },
      "needs_amount": 7
    }
  }
}
```

The file itself writes each list item on a line of its own, and every key in sorted order, so the
same model always gives the same bytes.

**What the command refuses.** It exits 1 with one `Error:` line naming every case, writes no new file
and removes an earlier one (below), when:

- two needs would take one id — an architecture requirement `SWC-Brake` beside a component `Brake`,
  or one id declared by two architecture requirement files;
- a need's id is the id of an incoming requirement your model holds: the team's own need would
  replace it in their build;
- a title or a description holds `[[` once its backslashes, spaces and non-printing characters are
  removed, because Sphinx-Needs reads `[[…]]` there as a dynamic function;
- your imported requirements come from more than one delivery — only a hand edit makes such a
  model: import the current delivery, then generate;
- a link names an id holding `.`. Sphinx-Needs 9.2.0 reads every link value up to its first `.` as
  the need's id and the rest as a part of that need, and no setting turns that off, so the link would
  name another need or none — the team cannot link such an id even in its own documentation. **SWCraft
  does not support an id holding `.` in a published link**: change the link or the id; `check-model`
  warns about such a link first. A need's own id holding `.` is written as it is, and an id holding `[`
  is published as it is (below, `parse_conditions`);
- an element it would write is declared by more than one file (which `check-model` reports as an
  error too): its `swcraft_path` could not name the one file that holds it.

`outputs.needs` names a file of its own: one equal to the ARXML file `outputs.arxml` names, to the
previous ARXML kept beside it (`model_old.arxml`), to `dist/audit.jsonl`, to the build report
`dist/build-report.md` or to `swcraft.yaml`, one directly in `views/` named `report.txt` or
ending `.svg` or `.html` (the names `swcraft generate diagrams` writes there), one in or equal to
`dist/metrics/`, `dist/resource-report/`, `dist/change-report/` or `dist/test-tables/` (where
`metrics --report`, `resource-report --report` and `change-report` write their reports and
`generate test-tables` its tables), or one equal to
`dist/components` or `dist/experiments`, is refused by every command that reads the project,
because writing it would overwrite that file. The check does not reach any other file of the
project outside `dist/` and `views/` (such as `moon.yml` or `.vscode/settings.json`), a file you keep
in `dist/` yourself, a component export below `dist/components/` or an experiment's report below
`dist/experiments/`: choose a name of its own.

A refused run removes an earlier `dist/needs.json`, so a file that no longer describes your model
cannot be handed over. When that removal itself fails — a folder at that name, or, on Windows, another
program holding the file open — the `Error:` line says the earlier file could not be removed and must
not be handed over, and the file stays. A write that fails leaves the earlier file as it was. **Hand the team the file
a successful build left.** How it reaches them is yours to choose; the team commits it at the path its
`needs_external_needs` entry names.

**What the requirements team adds to its `conf.py`.** Three need types, two fields, four link types
and one `needs_external_needs` entry:

```python
# --- SWCraft's architecture: the need types, fields and link types its needs.json uses ---
needs_types = [
    # ... the project's own types (a project on the defaults lists req, spec, impl, test, need) ...
    {"directive": "arch_req", "title": "Architecture requirement", "prefix": "AR_"},
    {"directive": "arch_spec", "title": "Architecture specification", "prefix": "AS_"},
    {"directive": "arch_element", "title": "Architecture element", "prefix": "AE_"},
]
needs_fields = {
    "asil": {"description": "The safety integrity level allocated to the requirement.", "nullable": True},
    "swcraft_path": {
        "description": "The file in the SWCraft model that declares the need.", "nullable": True,
        "parse_dynamic_functions": False,
    },
}
needs_links = {
    "satisfies": {
        "outgoing": "satisfies", "incoming": "satisfied by",
        "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "satisfies_unconfirmed": {
        "outgoing": "satisfies (unconfirmed)", "incoming": "satisfied by (unconfirmed)",
        "allow_dead_links": True, "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "derived_from": {
        "outgoing": "derived from", "incoming": "derives",
        "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "derived_from_unconfirmed": {
        "outgoing": "derived from (unconfirmed)", "incoming": "derives (unconfirmed)",
        "allow_dead_links": True, "parse_conditions": False, "parse_dynamic_functions": False,
    },
}
needs_external_needs = [
    {
        "base_url": "",
        "target_url": "https://git.example.com/our-org/architecture/src/branch/main/model/{{need.swcraft_path | urlencode}}",
        "json_path": "swcraft/needs.json",
        "css_class": "swcraft",
    },
]
```

These lines were built with **Sphinx-Needs 9.2.0** and `sphinx-build -W` reported no warning; other
releases were not tried. Each line is needed:

- **A type the team does not declare refuses the need**, so all three are declared. Setting
  `needs_types` at all replaces Sphinx-Needs' default types, so a project on the defaults lists them
  where the comment stands. The architecture requirements have types of their own, so the team's own
  `req` and `spec` counts do not include them.
- **`asil` and `swcraft_path` undeclared are dropped** with a warning, which fails a `-W` build.
- **`swcraft_path` is read as text** (`"parse_dynamic_functions": False`): Sphinx-Needs otherwise reads
  `[[…]]` in a field as a dynamic function, so a group folder or a file name holding `[[` would drop
  the need with a warning.
- **The four link types read an id whole** (`"parse_conditions": False` and
  `"parse_dynamic_functions": False`): without the first, Sphinx-Needs reads `[…]` in a link as a
  condition, so a link to a requirement whose id holds `[` would name another need or none; without
  the second, it reads a link value starting with `[[` as a dynamic function and drops the whole
  SWCraft need with a warning.
  An id holding `.` is not reached by any setting (the refusal above).
- **The two unconfirmed link types allow a dead link**, and only they: an unconfirmed link may name a
  placeholder the team never delivered, or a requirement it has since withdrawn, and the team's build
  reports it as an information line. A confirmed link names a requirement the team holds when your
  requirements come from an import of its current delivery, so a dead one means the two files
  disagree, and the team's `-W` build fails on it.
- **`target_url`** is where a SWCraft need links to. It is a template over the need, so
  `{{need.swcraft_path | urlencode}}` makes each link open the element's own file, its `#`, `[`, `]`,
  spaces and other such characters escaped and its `/` kept; the part before it is wherever the
  architecture project's model folder (the manifest's `model_root`, `model` above) can be opened:
  - the repository's file view, as above:
    `https://git.example.com/our-org/architecture/src/branch/main/model/{{need.swcraft_path | urlencode}}`;
  - a checkout on the reader's machine: `file:///…/architecture/model/{{need.swcraft_path | urlencode}}`,
    where `…` is the folder the checkout sits in, written with `/` — on Windows starting with its
    drive letter and a colon;
  - the same checkout opened in VS Code:
    `vscode://file/…/architecture/model/{{need.swcraft_path | urlencode}}`, the folder written the
    same way — a click opens the element's YAML in VS Code, with no network involved;
  - the architecture project as it is open in the reader's VS Code, through the SWCraft extension:
    `vscode://swcraft-local.swcraft-vscode/model?path={{need.swcraft_path | urlencode}}` — one
    address for every colleague, whatever folder each checkout sits in. It opens the element's YAML
    in the project open in the window, which must hold it; it opens no diagram (the `diagram` link
    above the element's name does that). VS Code asks once before it hands such a link to the
    extension. A link that names no file of the model opens nothing and leaves one line in the
    **SWCraft** output channel; with no SWCraft project open in the window, a notification says so.

  The repository address was built and checked, and the extension's link tried by hand on one
  machine, a Mac. Whether the others open is the reader's browser's and system's: a browser often
  refuses a `file:` link from a page it loaded over `http` or `https`, a `vscode:` link needs VS
  Code installed on the reader's machine, and the extension's link also the SWCraft extension with
  the project open in it.

  A fixed address with no `{{…}}` sends every SWCraft need to that one page. **`json_path`** is where
  the team commits your file, relative to its `conf.py`. No version is selected: the entry reads the
  file's `current_version`.
- A team that already declares `satisfies`, `derived_from` or `asil` with a meaning of its own shares
  that name with SWCraft's file; an `id_prefix` does not separate them, because the names are link
  types and a field, not ids.

**What the team sees:** on each of its own requirements, *satisfied by* and *satisfied by
(unconfirmed)* elements, and *derives* and *derives (unconfirmed)* architecture requirements; and
SWCraft's needs in any table, filtered by type or by tag:

```rst
.. needtable::
   :filter: is_external
   :columns: id, title, type, tags
```

An element is its title and its id, linking to its file through `target_url`. A component is not shown as the
container of its linked ports and runnables, and every element has one type, so a `needflow` draws
them alike.

**The team's next delivery to you.** The declarations above put the four link types, their `_back`
lists, `asil` and `swcraft_path` on every need of the team's own, so its next `needs.json` carries them, and
`swcraft import requirements` refuses a key it neither reads nor is told to ignore. The mapping the
import reads with `--mapping` lists the keys it does not read. With SWCraft's own names (the team's
parents in `derived_from`, its level in `asil`):

```yaml
ignore: [satisfies, satisfies_unconfirmed, derived_from_unconfirmed, swcraft_path]
```

A team whose type is `sysreq`, whose parents are a `parent` link and whose level is a `safety` field
ignores all six:

```yaml
types:
  sysreq: req
fields:
  derived_from: parent
  asil: safety
ignore: [satisfies, satisfies_unconfirmed, derived_from, derived_from_unconfirmed, asil, swcraft_path]
```

**`needimport` is the other way in, and not the recommended one.** It creates SWCraft's needs in the
team's own documents, so the team's `needs.json` carries them back to you; its mapping then also
ignores SWCraft's three types — `arch_req: ignore`, `arch_spec: ignore` and `arch_element: ignore`
under `types` — beside the same `ignore` line.

## Starting a project { #starting-a-project }

<!-- purpose: Starting a new project in an empty directory, and what the scaffold holds -->

```sh
swcraft init my-arch
```

Scaffolds the manifest, the layer directories and the editor's configuration.
[Getting started](getting-started.md) walks through what appears and what to put in it.

`init` never overwrites a file, nor writes through a link: a file, or a folder on the way to one,
that is a link — even one pointing nowhere — is left as it is and reported as *Skipped*. So a
`swcraft.yaml` already in the directory is kept, and `init` then reads it. The editor's `.vscode/settings.json` takes the model, `dist` and views folders from
the manifest in place, the kept one included, so that Code Spell Checker leaves them alone
([In VS Code](vscode.md#the-red-squiggles-are-not-the-gate)); a manifest that cannot be read gives
`model`, `dist` and `views`. A kept `swcraft.yaml` whose `toolchain_version` names another release
refuses `init` with the pin's sentence and exit 1, before anything after its *Skipped* line is
written.

`--integration moon` scaffolds a build graph alongside it, for a project that wants the
commands on this page wired into a task runner rather than typed. It also writes the files a chat
client needs to work with the project, for the clients `--client` names — `copilot`, `codex`,
`claude` or `kiro`, given once per client:

```sh
swcraft init my-arch --integration moon --client copilot --client claude
```

Without `--client` only Copilot's files are written. Which files each client gets, the editor
settings added when Copilot is chosen with Codex or Claude Code, and the warnings `init` prints
for Copilot with Claude Code and for Kiro with Codex or Claude Code are in [In
VS Code](vscode.md#turning-a-folder-into-a-project); Kiro's files are on [In
Kiro](kiro.md#onboard-project-in-kiro).
`--client` without `--integration moon` is refused, and nothing is written.

## Exchanging with other tools { #exchanging-with-other-tools }

<!-- purpose: Exporting a component to another tool, or importing a model or a component from an ARXML file -->

Two directions, and they are deliberately asymmetric.

**Out**, one component at a time or all of them, as self-contained ARXML files that carry
everything the component depends on:

```sh
swcraft generate arxml --project swcraft.yaml --component SpeedSensor
swcraft generate arxml --project swcraft.yaml --all-components --output-dir dist/components
```

A component whose files reach an [external](model.md) interface or type is refused, naming each
one with its path, because the file would reference a definition it does not contain and could not
be imported back whole; `--all-components` lists such a component as failed.

`--all-components` writes each component to `<name>.arxml` in the directory `--output-dir` names,
and `export-summary.json` beside them names each component's file and says whether it was written. Two
components whose names differ only in case would share one file on a filesystem that ignores case,
so the name that sorts first keeps `<name>.arxml` and the other takes `<name>~1.arxml`; only the
file name changes, never a name inside the file. A file an earlier run left in the directory is
never removed, so read the file names from the summary.

`--component` without `--output` writes to `<dist_root>/components/` under the same file name the
batch gives that component, so both members of such a pair can be exported one by one and neither
replaces the other. The name follows the model as it is when the command runs: a twin added later
moves the other member's default to `<name>~1.arxml`. A path given with `--output` is used exactly
as given and is not compared with any other file.

**In**, either a whole export into a project that does not have a model yet, or one
component-scoped file merged into a project that does — `swcraft import model` or
`swcraft import component`, with `--source` naming the file:

```sh
swcraft import model --source export.arxml --project swcraft.yaml
swcraft import component --source SpeedSensor.arxml --project swcraft.yaml
```

Both ARXML imports take `--dry-run`, which computes the whole thing and prints the report
without writing a file. Use it first, every time. Both take `--report PATH` too, which also writes
the report to a file: `import component` writes it as Markdown when PATH ends in `.md`, like
SWCraft's other reports, and as the printed text otherwise; `import model` always writes the
text. The whole-model report lists what it skipped as
one line per reason, with the number of elements that reason covers; add `--verbose` to list every
skipped element under its reason. The list is in three sections, each printed only when it counts
something, and the `SKIPPED` line gives all three counts:

- **Defects of the export** — the file breaks a rule of the AUTOSAR standard, and each line cites the
  rule after the element's tag, as `(R4.2.2 [constr_1086])`. One citation is not a rule's: a
  runnable's `SYMBOL` that is not a C identifier cites the standard's own definition of one, as
  `(R4.3.0 [GST primitive CIdentifier])`. A label on a scale of a linear computation method is a
  defect too, since the standard's table of what each category carries allows none there; it
  changes no behaviour and stops nothing. The fix belongs to whoever made the file.
- **Not supported by SWCraft** — something SWCraft does not read or does not allow. The fix is
  SWCraft's.
- **Outside the file** — the file references content it does not declare. Bring the file that
  declares it.

A line is a defect of the export only when the standard is cited for it — a rule, or that one
definition; a line that looks like one but breaks nothing SWCraft can cite stays under *Not
supported by SWCraft*. A port with no interface
reference is listed there, because the standard lets a file leave the reference out and SWCraft
cannot import a port without one. The citation names the
release the file declares in its schema location; a file that declares any other schema, or none, is
cited from AUTOSAR 4.3.0, and the section's heading says so and names what the file declared. A
line skipped only because an earlier element was skipped is in the section of that element, and its
reason names it. In a file that carries variation points a repeated name may be a variant
alternative, so every repeated name there is under *Not supported by SWCraft*. `--dry-run` with
these sections is a check of an export that writes nothing, and it exits 0 however many defects it
names. A section with no line is not a statement that the export has none: what SWCraft does not
read is not looked at. `import component` groups the lines of its refusal message under the same
headings, a defect's line citing its rule.

`import model` never changes `swcraft.yaml`. When the file's schema location names one of the two
AUTOSAR releases SWCraft writes and your project's `arxml_version` is the other — 4.2.2 when the
manifest states none — the report gains one line under its title, for example *ARXML version: the
file declares AUTOSAR 4.3.0 (AUTOSAR_4-3-0.xsd); this project's arxml_version is 4.2.2, and import
model does not change it -- set arxml_version: 4.3.0 in swcraft.yaml, or check-model applies 4.2.2's
rules.* The two releases differ in a rule or two — whether every asynchronous call needs a result, for
one — and `swcraft check-model` and `swcraft generate arxml` follow the manifest. A file that declares
no schema, or a release SWCraft does not write, gets no such line, and `import component` prints none:
it judges a component's asynchronous calls at your project's `arxml_version` itself, and `swcraft
check-model` judges the project it leaves at that version too. In a 4.2.2 project `import component`
lists each asynchronous call the component it writes leaves with no result under *ASYNCHRONOUS CALLS
WITHOUT A RESULT* — on a new component and on every `--update`, a carried `asynchronous: true` whose
`call_returned` event went with a runnable the file removed included; a call the project's component
already had that way is not listed — and writes the component anyway, on `--dry-run` and in the
assistant's import plan too. Each line names the runnable, the port and the operation, the rule
`swc.call.async-without-result` and `[constr_2006]`, and what to do: add `poll: true` to the call or a
`call_returned` event naming it, or set `arxml_version: 4.3.0` in `swcraft.yaml`. A 4.3.0 project lists
none. Any other asynchronous call finding the written component holds and the project's did not is listed
the same way, at either version, under *ASYNCHRONOUS CALL FINDINGS*, each line the rule and the finding as
`swcraft check-model` words it. An `--update` from a file that states no asynchronous call keeps the
project's own asynchronous calls and `call_returned` events, and that can leave one: a call the file adds
beside a call kept asynchronous (`swc.call.mixed-synchrony`), or a `call_returned` event kept for a call the
file no longer makes (`swc.call-returned-event.unknown-call`).

Both modes also read what each
runnable touches — the data it receives and sends and the operations it calls — into the runnable's
`reads`, `writes` and `calls`; every access point the importer cannot place is one skip line with
its reason, and a pair the file declares twice under two names is collapsed to one entry and listed
under *Collapsed duplicate access points* in the report. The same section lists an exclusive area a
runnable's file names twice in one list, kept once.

Both modes read how each access and call is made. A read or a write in the file's implicit
containers becomes an entry with `by: implicit`; an asynchronous call point becomes a call with
`asynchronous: true`, and the `TIMEOUT` it states, any value, becomes its `timeout` — a synchronous
call point's `TIMEOUT` other than 0 is still one line, since SWCraft writes 0 there. A result point on
the runnable that makes the call, which no returns event names, becomes that call's `poll: true`, and
a returns event that starts the runnable owning its result point becomes a `call_returned` event
naming the call's port and operation; a result point's or an event's name that differs from the one
SWCraft would derive is kept as `result` or `name`. Each reference is bound by its exact path. A
runnable that reads one element both implicitly and by argument keeps the first in the file's order,
and the other is listed under *Collapsed duplicate access points*; a result point naming a call point
collapsed that way is bound to the kept one, with a line naming both. An implicit read of a queued
element, or of an element its port's communication specification states an update flag for, is
imported as the file states it, with a line saying that `swcraft check-model` refuses the model.
So is every asynchronous call finding the file itself states, each one line under *Defects of the
export* that cites the rule and words the finding as `swcraft check-model` does: one operation of
one port called both synchronously and asynchronously (`[constr_2022]`), its asynchronous calls
stating different timeouts, a missing timeout beside a stated one included (`[constr_2023]`), a
result point on a runnable an init event starts (`[constr_1259]`), and two result points of one
call point (`[constr_2006]`). An asynchronous call with no result point is imported as it is too;
`check-model` reports it in a 4.2.2 project only, and the import lists it there as such a line when
the file declares AUTOSAR 4.2.2, which asks one result point of every asynchronous call. A file that
declares neither release SWCraft writes has no rule of its own to cite, so in a 4.2.2 project the same
call is a line under *Not supported by SWCraft*: it names the call and the rule
`swc.call.async-without-result`, says that your project's 4.2.2 rules apply, and gives the repair —
`poll: true` on the call, a `call_returned` event naming it, or `arxml_version: 4.3.0` in
`swcraft.yaml`. What cannot
be carried is one line with its reason: a returns event that starts no
runnable (a wait point's), one whose result point belongs to another runnable or that states disabled
modes, a result point of another runnable's call that no returns event names (SWCraft models a poll
on the calling runnable only), a second result point of one call on one runnable, a result point of
a call point collapsed into another of the same runnable when the kept call's result is already
taken (each call point had one result point, and the one call SWCraft keeps holds one; a returns
event's is kept before a poll, since the event starts a runnable), a returns event of a
call that two or more runnables of the component make asynchronously (a `call_returned` event names
the port and the operation, not the runnable that calls it; the result point is not made a poll
either, since the event starts a runnable when the call returns and a poll starts none), and a
reference to a synchronous call point, to another behaviour or to nothing. In `import component`
every such line stops the import.

**What this does not reach:** a `SCOPE` stated on a read, write or call point through a port, in
either form, is dropped without a line — only a local variable access's `SCOPE` is a line; and a
result point of a call through a port whose interface the export declares nowhere is one line and is
not bound, because the result points are bound before SWCraft decides that port. A wait point, and so
a runnable that blocks until a call returns, is not modelled. When a returns event is not carried because
two runnables make its call, their calls stay asynchronous with no result, so a 4.2.2 project's
`swcraft check-model` reports each call that nothing else fetches; the import's line for the event is
the only one, since the file gave each call a result. A 4.3.0 file in a 4.2.2 project gets no line
for a call with no result — the version line under the report's title says which rules `check-model`
applies. Two synchronous calls of one operation with different timeouts are not looked at.

Both modes read which operation starts each server runnable, as an `operation` event naming the
component's own provide port and the operation, and whether the runnable may run concurrently, as
`concurrent`; a concurrency value that is none of `true`, `false`, `1` and `0` is not read, and is
one skip line naming the value, the runnable written with `concurrent: false`. An event the importer cannot bind to a port of that same component and an operation of
that port's client-server interface is one skip line with its reason: a port the component does not
declare, a require port, a sender-receiver interface, or an operation the interface does not declare.
Where the port's interface is one the export declares nowhere, `import model` writes an external
interface for it (below) that lists the operations the file names directly under its path, and an
event naming one of them is kept; an event naming anything else through that port — a path deeper
than one segment below the interface's — is the line, and it names the external interface when one
is written. In `import model` the rest of the component is imported. In `import component` an event
naming an operation the interface does not declare — the interface the file carries or the one your
project answers with — and every event through an external interface is a reference the project
does not have: the component is written without its runnables, and the report lists the reference
(below). Every other such event stops `import component` with nothing written, like every element
that command cannot carry. A port
and operation served twice, and a runnable whose operations could not share one entry point, are
imported as the export has them, for `swcraft check-model` to report. A runnable that the export
starts both by an operation and by a timing or an initialisation event is imported as it is: the RTE
specification describes mixing as unsupported in explanatory text, with no tagged requirement;
SWCraft does not judge it. A provided operation whose queue length is not 1 is listed under *Server
queue lengths not carried*, because the generated file always writes 1; nothing is skipped for it.

Both modes name what a component's behaviour and its runnables carry that SWCraft does not read:
the nesting orders of exclusive areas and a runnable's references to those nesting orders, the
exclusive area API options of AUTOSAR 4.3.0, per-instance memories and parameters, service
dependencies, parameter accesses, mode access and mode switch points. Each kind of content
is one skip line naming the container of a behaviour or of a runnable it came from, and
`import model --verbose` lists each item's path under its line. A behaviour's
`SUPPORTS-MULTIPLE-INSTANTIATION` that is not `false` is a line too, and so is a runnable with no
`SYMBOL`, every port API option — one line per option, since SWCraft carries no port API option
and the standard gives an absent option no default — and every behaviour of a component after the
first, which SWCraft does not read. In `import component` every such line stops the import with
nothing written, so a component file another tool wrote may be refused — except the line of a port
API option with both of its features off, which requests nothing: that one is listed under *Not
carried from the file* and the component is imported (an earlier SWCraft release wrote one such
option per port). What these lines do not reach: content *inside* an event, an access or a data type
mapping reference — except a variation point stated on a read, write, call or variable access or on
an exclusive area, which is one line under *Not supported by SWCraft*, as one on a variable or a data
element is: the access or the area is imported without its condition, and in `import component` the
line stops the import.

Both modes read a component's inter-runnable variables, explicit and implicit, into its `variables`
— each with its type, initial value, description and calibration access — and each runnable's reads
and writes of them into its `reads_variables` and `writes_variables`. An access is bound to a
variable of the same component's behaviour by its exact path, and an access name that differs from
the one SWCraft would derive is kept as the entry's `name`. A variable whose implementation type the
export declares nowhere is kept, typed by an external type written for it. What cannot be carried is
one line with its reason: a variable whose type was not imported, or whose application type the
export declares nowhere, is skipped, and so is every access to it; an access that names a part of a
variable or a variable of another behaviour, which the standard does not allow for these accesses,
is skipped under *Defects of the export*, citing the rule. An initial value that cannot be carried is
one line, judged as a data element's own value is, and the variable is kept without it; in `import
component` that line stops the import. A stated implementation policy, data properties other than
the calibration access, an access's scope and its variation point are one line each under *Not
supported by SWCraft*,
and the variable or the access is kept without them; in `import component` each stops the import,
except a policy of `STANDARD`, which says what no policy means and is listed under *Not carried from
the file*. An export that lists one variable twice
in one runnable's reads, or twice in its writes, is imported with one entry, and the other is listed
by name. An empty container writes nothing and reports nothing. A variable read with no initial
value is imported as it is, and `swcraft check-model` then warns about it. In `import component`
every skip stops the import with nothing written.

A component file SWCraft writes says that it carries the component's variables, so `import component
--update` with it replaces the project's variables and accesses with the file's, and removes one the
file no longer has. A component file from an earlier SWCraft release, which never wrote a variable,
leaves the project's variables and accesses as they are, and the report lists what it kept, the
mapping sets the kept variables need among it; a file
from another tool that does state variables is compared as it stands. An earlier SWCraft release
refuses every component file this release writes.

Both modes read a component's exclusive areas into its `exclusive_areas`, each with its description,
and each runnable's references to them into its `can_enter` and `runs_inside`, by the list the
reference stands in. A reference is bound to an area of the same component's behaviour by its exact
path. What cannot be carried is one line with its reason, under *Not supported by SWCraft*: a
reference that names something other than an exclusive area, an empty reference, a reference to an
area of another behaviour, a reference to an area the behaviour does not declare, and every reference
to an area that was skipped. A runnable that names one area twice in one list is imported with one
entry, and the other is listed under *Collapsed duplicate access points*. A runnable that names one
area both to run inside and to enter is imported as it is. An empty container writes nothing and
reports nothing. `import model`'s report counts the areas and references it imported, per
component. In `import component` every skip stops the import with nothing written.

A component file SWCraft writes says that it carries the component's exclusive areas, so `import
component --update` with it replaces the project's areas and the runnables' references with the
file's, and removes one the file no longer has. A component file from an earlier SWCraft release,
which never wrote an area, leaves the project's areas and references as they are, and the report lists
what it kept; a file from another tool that does state areas or references is compared as it stands,
and a runnable it gives no reference keeps none. An area's description is compared as the file
writes it, an empty one included: unlike the component's, a port's or a runnable's, an empty area
description in a file that states areas does not keep the project's text, so it is a difference, and
`--update` writes the area with no description — as it does a variable's. The report of a component
it writes says how many areas and references that component holds, when it holds any.

Both modes read which runnable a received value starts, as a `data` event naming the component's own
require port and the data element, with the modes that disable it and a name that differs from the one
SWCraft would derive. The port is bound to a port of that same component by its exact path, and the
element to a data element of that port's sender-receiver interface. Where the port's interface is one
the export declares nowhere, `import model` writes an external interface for it that lists the elements
the file names, and an event naming one of them is kept. An event the importer cannot carry is one skip
line with its reason: an event through a port that both provides and requires (SWCraft models no such
port), a port of another component, a provide port, a client-server interface, an element the
interface does not declare, or a reference that is missing. `import model`'s report counts the
`data` events it imported, per component, under *IMPORTED … data-received events*. In `import
component` every such skip stops the import with nothing written, and an element your project's
interface lacks is a reference the project does not have (below). A runnable that the export starts by
a received value and by a timing event, and one started by a value it does not read, are imported as
they are. The other three data events of AUTOSAR — on a reception error, a completed send and a
completed write — are still not read: each is one line under *Not supported by SWCraft*, and stops
`import component`.

A component file SWCraft writes says that it carries its runnables' `data` events, so `import
component --update` with it replaces the project's with the file's, and removes one the file no longer
has. A component file from an earlier SWCraft release, which never wrote a `data` event, leaves each
runnable's `data` events where they are, in their place among its other events, and the report lists
what it kept under *DATA-RECEIVED EVENTS preserved from the target project*; a runnable the file no
longer has goes with its events. A file from another tool that states a `data` event on any runnable is
the truth for every runnable's and is compared as it stands, so a runnable it gives none keeps none. A
file carrying the statement with a value this release does not write is refused with nothing written.
The report names the rule that decided, on its *DATA-RECEIVED EVENTS regime* line.

A component file SWCraft writes also says that it carries its runnables' implicit reads and writes,
and their asynchronous calls with their `poll`, `result` and `timeout` and the `call_returned` events,
so `import component --update` with it replaces the project's with the file's. A component file from an
earlier SWCraft release, which could write none of them, leaves each `by: implicit` on a pair the file
still reads or writes, each `asynchronous: true` with its `poll`, `result` and `timeout` on a pair the
file still calls, and each `call_returned` event on a runnable the file still has, and the report lists
what it kept under *IMPLICIT ACCESS preserved from the target project*, *ASYNCHRONOUS CALLS preserved
from the target project* and *CALL-RETURNED EVENTS preserved from the target project*; a kept event whose call the file no longer makes stays, and `swcraft check-model` reports
it. A kept `by: implicit` can also land on a read the file has since changed — its port's communication
specification now states an update flag for the element, or its interface now makes the element queued
— and the import writes it anyway and lists each such read under *IMPLICIT ACCESS FINDINGS*, on
`--dry-run` and in the assistant's import plan too: the rule and the finding as `swcraft check-model`
words it (`[constr_1103]` or `[constr_2020]`). A read the project's component already had that way is not
listed. A file from another tool that states an implicit access, or an asynchronous call or a returns
event, is the truth for every runnable's and is compared as it stands. A file carrying either statement
with a value this release does not write is refused with nothing written. The report names the rule that
decided on its *IMPLICIT ACCESS regime* and *ASYNCHRONOUS CALLS regime* lines.

A runnable's `SYMBOL` is carried: where it differs from the runnable's name it becomes the
runnable's `symbol`, taken as written. One that is not a C identifier — surrounding whitespace
included — and one that two runnables of the component state alike are dropped, each runnable then
written under its own name, and named under *Defects of the export*; a symbol that would only
collide with a sibling SWCraft writes under its name is dropped the same way and named under *Not
supported by SWCraft*. `import component` refuses the file for any of them.

A behaviour's own documentation — its long name, category, introduction, annotations and description
— and a runnable's long name, category, introduction and annotations are not carried either: each one
that holds text is one line under *Not supported by SWCraft*, and so it stops `import component` too.
Descriptions are carried: a runnable's, a data element's, an operation's, an argument's, an
enumeration literal's, a record field's, an implementation record element's, a composition member's,
a connector's and a delegation's `DESC` lands in the element's `description`, as a component's, a
port's, an interface's, a type's, a unit's, a computation method's and a data constraint's does. A
description the model has no place for — on an event, an access, an array's element, or a
computation method, unit or data constraint read into its type's own fields (below) whose text
differs from its type's — is one line under *Not supported by SWCraft*. SWCraft's own generated ARXML
carries each description as the element's `DESC`, and a composition's title as its long name.

Both modes keep the source's names of a component's behaviour, of its events and of its runnables'
read, write and call points: wherever the file's name differs from the one SWCraft would derive, the
element states it, and the generated file writes it back as it is. A source name that is not a legal
AUTOSAR identifier, or that would collide ignoring case with another name beside it, is not stated:
the element keeps the derived name, and the line *SHORT-NAME of … not carried: SWCraft names it
itself* says why — *because the source's name would collide with …* or *because the source's name
is not a legal AUTOSAR Identifier …* — under *Not supported by SWCraft*. `import component` compares
a stated name as it stands, so a project that states a name equal to the one SWCraft would derive
reads back from its own exported file as stating none, and the import reports that difference.

Both modes judge every initial value against the file's own types. A value in the form the standard
asks of its type is carried; a data element's own value in another form is not carried and is one
line under *Defects of the export* citing the rule. A number given under an enumeration, or under an
implementation type that references a table, is read through that table: a whole number that exactly
one single-value entry of the table gives is carried as that entry's literal, and the report says so
in one line — under *Defects of the export*, citing the rule, for a data element's, a port's or a
variable's own value, and under *Not supported by SWCraft* for a member of a record or array value;
`import component` carries the value too and goes on. Every other such number is
one line under *Not supported by SWCraft* and is not carried: a number no single entry gives, a
fraction, a number under a bit-field table, under a table one of whose entries states a symbol, under
a table SWCraft dropped an entry of, or under a type that collapsed into its implementation twin. A
value cannot be held in part, so the element then has
no initial value at all, and in `import component` that line stops the import. A port's own
initial value — the one a port's communication specification states — is different: one that is
not carried is a line under *Not carried from the file* in both modes, and the port is written
without it, as with every other value of a port's communication specification SWCraft does not
carry. Four more lines are only
descriptive, and `import component` lists them under *Not carried from the file* and goes on: *the
unit of a value is not carried: a value's unit is its type's*; *written in the numerical form:
the type is the implementation type its twin collapsed to*; *collapsed into its implementation twin,
written as 'X' (the same-name pair): SWCraft writes the one implementation type*, for an application
type reduced as described below; and *the map row of the same-name pair 'X' is folded into its
collapse: both sides are the one implementation type 'X'*, for the mapping set's row that paired
that application type with its twin — the set is written without it. A mapping set row whose two
sides are one type for any other reason still stops the import.

Both modes read the implementation side of a structure, an array and a pointer — an
`IMPLEMENTATION-DATA-TYPE` of category `STRUCTURE`, `ARRAY` or `DATA_REFERENCE` — as an
[implementation record, implementation array or pointer](model.md), when it has the one shape those
kinds hold: every element a reference to a named implementation type, an array of fixed size with one
element, a pointer whose target is a named implementation type. Element names and their order are
the export's own, never changed. Every other shape — an element declared inline, an array whose size
varies, a union, a pointer to a function or to `void`, a pointer to a pointer declared inline — is one
skipped-element line saying what it is, and nothing is reshaped to fit. A pointer that leads back to
itself through other types is skipped as a cyclic type reference — SWCraft's own restriction, not
one it attributes to AUTOSAR: a pointer does not contain what it points to, but SWCraft does not
model a type that reaches itself through one.

An application type, an implementation type and a base type live in three layers of their own, so
one name declared in two of them is two elements under that one name, nothing renamed: a record and
an implementation structure of one name, a value type and the base type it rests on, an application
type and the implementation type it is mapped to. One pair is reduced to a single element: an
application type that states no unit, conversion or range, paired by a mapping set with an
implementation type of its own name and named by no record field or array element, says nothing its
twin does not, so the implementation type is kept alone. `import model` lists the pair under
*Type-name reductions (app/impl twin collapse)*; `import component` lists it under *Not carried from
the file*. On `import component` a pair your project declares both members of — an `application_type`
element (not an enumeration, record or array) and an implementation type of its name — is kept as
your project keeps it, both elements and the mapping set's row between them, even where nothing in
the file itself names the application type: a component file SWCraft exported from your project is
then read against the pair your project holds rather than reduced. Into a project that declares no
such pair, the pair is reduced as `import model` reduces it. Records and arrays whose members are
other records and arrays are imported in the order their members need, whatever order the export
declares them in.

Both modes create a unit, a computation method or a data constraint element for each one the file
declares in a shape SWCraft holds, under the file's name, and every type that names it references
that one element. A form outside those shapes — a unit stating an SI relation, a computation method
stating the physical-to-internal direction, a table that cannot be an enumeration, a constraint with
several rules or internal limits — is not created and is one skipped line. One case is read back
into the type's own fields instead: a unit, conversion, range or table that lies in its type's own
package, is named like the one SWCraft generates for that type (`{type}_Unit`, `{type}_Compu`,
`{type}_Range`, `{enum}_CompuMethod`), is referenced by that type alone and says nothing the type's
own field cannot — which is how SWCraft's own file comes back as it was written. A foreign file laid
out exactly that way is read the same way. A limit or a conversion coefficient that no model number
holds exactly is not carried, and the line says so. A physical limit of a range or of a constraint
that is infinite on its own side — `-INF` below, `INF` or `+INF` above, or any limit marked
`INTERVAL-TYPE="INFINITE"`, whatever number it holds — is imported as no bound, which states the
same interval, and listed once under *Represented in another form*; the same holds for
`swcraft import component`. A `LINEAR` conversion's scale limits are carried
into its `limits`, each with its interval; a limit infinite on its own side states no bound and is a
descriptive line, and a pair that cannot be carried whole — a limit that cannot be read, or two that
state no value between them — is not carried at all, with a skipped line for each limit stated.

A mapping set's pair of a record with an implementation record, or of an array with an implementation
array, is written as the export declares it. Where SWCraft's check of such a pair refuses it — the
[model page](model.md) says what that check judges and what it does not — or where the pair names a
pointer, the pair is still written, and listed under *Data type maps kept that SWCraft's composite
check refuses*, for `swcraft check-model` to report and `swcraft generate arxml` to refuse until you
correct it. A pair whose members are named differently on the two sides is not refused and not listed.

A runnable's execution-time budget is read from a timing description (`SWC-TIMING`) only for a
component the import creates, and only from an execution-time requirement that names one of that
component's runnables, states its type (`GROSS` or `NET`) and a maximum, and carries no condition
or instance scope the budget would lose. A maximum in an AUTOSAR time unit other than `ns`, `us`,
`ms` and `s` is written in the coarsest of those four that holds it exactly (`10 ns × 85` becomes
`850ns`) and listed under *Represented in another form*, as is a factor spelled in hexadecimal,
octal or binary, which is written in decimal. A maximum below a nanosecond, or longer than 18 digits
of its unit, is not imported, and the line says why. A requirement that cannot be kept whole — two
on one runnable, a conditional one, one naming no runnable the import carries — stops the import;
what the model has no place for — a minimum, a supplier's timing guarantee, the timing
description's own name or package — is listed and the import goes on. The same holds for
`swcraft import component`.

An export may reference something it does not contain — a type or an interface that lives in
another file of the same delivery. SWCraft keeps the element that made the reference either way.

Where the export declares the target **nowhere**, and the reference is a port's interface, a data
element's or an argument's type, a reference type's base, an implementation record's element, an
implementation array's element or a pointer's target, SWCraft imports the target as an
**external** interface or type: a file of its own holding the source's path and the kind the
reference named, which the port or the type names in place of the target ([Working with the
model](model.md) says what an external element is). One path is one external element, named by the
path's last segment followed by `_ext` and a number: `_ext0` where no other external element of its
kind has a path ending the same way, and `_ext0`, `_ext1`, `_ext2` … where several do, numbered in the
alphabetical order of their paths — so a name never depends on where in the file a path first
appears. The suffix marks the element as one the import brought in from outside the file, and every
external element's name is listed among the renames. Where the name with its suffix would be longer
than 128 characters, the last segment is shortened and the suffix kept whole. An external element
you write by hand is named as you choose. When an import keeps only some packages, an external
element keeps the number the whole file gave it, so a number can be missing beside it; importing
the written result again numbers it afresh.
The report counts them on a `STUBBED` line and lists each under *External interface stubs* or
*External type stubs* — path, kind and the name it was given.

An external interface lists the members the file names through its ports: the operations of a
client-server one and the data elements of a sender-receiver one, each a name exactly one path
segment below the interface's path, and nothing more — no type, no argument. An event, a read, a
write, a call or a port's communication specification that names a listed member through such a port
is kept, and the generated file names that member as the interface's path plus `/` plus its name.
Nothing else behind such a port is guessed: a member named deeper than one segment, of the other kind,
through a mode-switch interface, which lists nothing, or by a name that is not a legal AUTOSAR
identifier or differs from another only in case, is skipped with a reason naming the external
element, and so is an initial value on such a port or on a data element whose type is external. An
external element stops nothing from checking or generating, and the generated file refers to each
target exactly as the export did.

A composition member whose component type the export declares nowhere becomes an **external
component**, listed under *External component stubs*. It declares the ports its connectors reach —
each with the name and the direction the connector's end states, and no interface ([Working with the
model](model.md) says why) — and those connectors and delegations are imported with it. Where every
connector on such a port came from ports typed by one interface, that interface is recorded on the
port as `observed_interface`, a hint `swcraft query` shows and the diagrams do not draw; where they
name different ones, none is recorded
and the report lists the candidates. The report adds up to four blocks, each printed only when it
counts something:

```text
External component stub ports (1 stub(s); name and direction as the connector ends state them, no interface is declared -- an observed interface is the direct neighbours' hint and is never judged):
  - Dcm (/Delivery/Services/Dcm): 2 port(s) -- 0 provide, 2 require; 1 with an observed interface

Stub ports with no observed interface (1; the direct neighbours name different interfaces):
  - Dcm.rAux -- candidates: IOther, ISig

1 connector(s)/delegation(s) reaching an external stub were skipped because the target port path is not owned by the context component's type (R4.2.2 [TPS_GST_00162]) -- a defect of the export; see "Defects of the export" below.
```

A further block, *Stub ports not imported* (printed second when it counts something), lists a port
the export states with two directions: no port
is written for it and every connector reaching it is skipped, because SWCraft does not pick one. The
last line above counts a connector skipped for a **defect of the export**: its end names the port
under a path that is not the external component's own — the same short name in another package, or
another type — which AUTOSAR does not allow, so SWCraft neither guesses which component was meant nor
writes a reference the export did not make. The line cites the rule in the release the file declares
(here 4.2.2), and each such connector is listed with that reason under *Defects of the export* in the
skipped list. So is a connector whose end names the port with a segment that is not a valid AUTOSAR name
(a letter first, then letters, digits or underscores, at most 128 characters): no port is written for
it, and the name is never repaired. Nothing checks that the other file's component really has the ports the connectors name,
and no connector to an external component is checked for compatibility beyond its direction.

Every other reference to something the export does not contain is written into the field exactly as
the export spelled it and listed under *Unresolved references*, saying whether the target is absent
from the export, outside the packages you selected, or declared twice with different definitions — a
target absent from the export lands here when its role cannot take an external element (a record
field, an array element of the physical view, a value type's base, a mapping set, any application type), or when the
references to it name two different kinds, which the report lists under *DEST disagreements*.
`swcraft check-model` reports every unresolved reference, and `swcraft generate arxml` refuses to
write a file it cannot resolve and writes nothing at all: supply the missing file and import again,
or narrow `--packages` to a set of packages that is complete on its own. A reference the export does
contain but SWCraft could not import — an unsupported kind, a nameless element — is neither and stays
in the skipped list where it was.

Where one name is declared twice in two packages, SWCraft keeps one element if the two
declarations are the same in everything it can read, and keeps neither if they differ — AUTOSAR
itself does not say which is right, so the report names every source path and says what differed,
under *Merged declarations* and *Conflicting declarations*. Units, computation methods and data
constraints are judged the same way, and a type is compared by the element it references, so two
declarations of a type naming two differently named units of equal content conflict. A conflicting
name stays taken, so nothing else quietly claims it; a description that differs alone is not a
difference — whitespace alone included — and the report names each text that was not carried under
the merged element, as it names a composition's title the kept composition does not have. A
`--packages` selection that keeps exactly one of two conflicting declarations imports that one and
says so. A selection that keeps both keeps the conflict: neither is imported, every element that
references the name keeps an unresolved reference, `swcraft check-model` reports each, and the
project does not generate until whoever made the file repairs it.

A base type the export declares without a `NATIVE-DECLARATION` is written without
`native_declaration` — nothing is invented — and listed under *Base types written without a native
declaration (check-model reports each)*. The import still succeeds and nothing is counted as
skipped; `check-model` reports each such base type, and `generate arxml` refuses the project until
you fill the field in.

Two names that differ only in case — `float32` and `Float32` — are two elements, and both
modes write both. AUTOSAR does not let two such names share one package of the generated file, so
when the export keeps them in two packages of its own, the import places one of them in a sub-package
named after its own source package — `package: PlatformTypes` in its file — and lists it under
*Placed in a sub-package to keep a case-insensitive name unique*; the name and every reference stay,
and only that element's path in the generated ARXML moves. On a first import of another tool's
export, the one placed is the one whose file takes the suffix below; in a file SWCraft generated,
the one written directly in the root package stays there, so the placement imports back unchanged. A
`package` you set yourself on an element with no such twin is read back by both modes from a file
SWCraft wrote: an element whose path in the file is its home package, one sub-package and its name —
`/DataTypes/Implementation/Legacy/float32`, say — keeps `package: Legacy`. A file written by an
earlier release, whose data types sit directly below `/DataTypes`, has no layer package in its
paths, so a data type's `package` is not recovered from it, and no line says so. Two such names in
**one** package of the export are the export's own defect: the whole-model import keeps the first
and lists the other under *Defects of the export*, and the component mode refuses the file. The
component mode keeps a sub-package a SWCraft-written component file places an element in, and never
moves an element your project already has: when an incoming element differs only in case from one of
yours and the file puts it in the root package, it refuses and names your element, which you can
place first. An element the project already declares keeps the file that declares it,
found by the name inside, whether the import rewrites it or leaves it alone; only an element no
file declares is given a file name, and where another element's file already holds that name,
compared without regard to case, the new file takes the nearest free `~1`, `~2`, … instead.
Each such file is one line under *Files suffixed for a case-insensitive collision* in the
report, naming the element, the file written and the name that was taken; the element's name
and every reference to it are unchanged. Both modes decide which of two such names gets the plain
file by where each element sits in the export, so the answer is the same in either mode and on
every machine. Each mode's `--dry-run` decides every file name the way the import does, without
writing one, so it lists the same lines the import reports — with one exception: when the export
carries a root composition of its own, a whole-model `--dry-run` on a freshly initialised project
previews a suffix for an imported composition whose name differs from the scaffolded `Root` only
in case, which the import does not take, because it removes that empty root before writing the
compositions. A file name another element already holds is no longer a reason for the component
mode to stop.

The component mode is the careful one: it
refuses rather than overwrite a definition of its own that has diverged, and `--update` is
how you say you meant it — for that one component's file only, never for a shared interface
or data type, which always aborts. A run without `--update` whose only conflict is that component's
own divergence (*SWC '…' already exists in the target project and diverges*) exits with status 3
instead of 1, its refusal and its nothing-written unchanged, so a script can tell the one refusal
`--update` resolves from every other; any other refusal, that divergence beside another conflict
included, exits 1.

In VS Code, **SWCraft: Import Component** runs that dry run for you, shows its report and imports
only on your click; on status 3 it offers the update's own dry run, then the update
([In VS Code](vscode.md#importing-a-component)).

What the file cannot say, the update does not erase: a
component export carries no memory budget and, until the writer emits access points, no
`reads`, `writes` or `calls`, so an update keeps the target's budget and each runnable's lists
where the export's runnable of the same name carries none, and the report says what it kept. The
same holds for a component's `handle_termination_and_restart`: a file that does not state
`HANDLE-TERMINATION-AND-RESTART` — every component file an earlier release wrote — keeps the
project's value, and the report says so in a line beginning *HANDLE-TERMINATION-AND-RESTART
preserved from target* and naming the value. A file that states a value replaces yours; a file cannot ask for the value to be
cleared — edit the project for that. The component's own description, each port's and each
runnable's are compared like everything else, so a file whose text differs is a difference, which
`--update` takes; where the file's text is empty — every component file an earlier release wrote —
the project's text is kept, one line per part (*DESC of component '…' preserved from target: source
element carries none*, and the same for a port or a runnable), and so is the component's `package`.
A runnable's execution-time budget, `wcet` with its `wcet_type`, is compared the same way: the
file's budget wins, so a budget that differs from yours is a difference, which `--update` takes. A
runnable the file brings with no budget — every component file an earlier release wrote — keeps the
budget your project declares for the runnable of the same name before anything is compared, so such
a file whose only difference is the missing budget is reported unchanged, with or without
`--update`, and nothing is written; when the import does write the file, the report lists the
budgets it kept on a line beginning *WCET preserved from the target project*. A component file
carries no requirement link, so an update keeps the links your project wrote — the component's own
`satisfies`, each port's and each runnable's by name — and lists them under *LINKS preserved from
the target project*; a link on a port or a runnable the file no longer declares has nowhere to go
and is listed under *LINKS NOT carried from the target project*. The units, computation
methods and data constraints the file's types name are created or reused like the types, and one
that differs from your element of that name refuses the import (*shared unit '…' diverges from the
target project's existing definition*, and the same with *compu method* or *data constraint*). A
shared interface, type or one of those elements your project already has keeps its descriptions too: a different one in the file is listed under *Descriptions not taken from
the file*, and the import goes on. A file without `HANDLE-TERMINATION-AND-RESTART` still imports, and
a component it creates then has no value, which `swcraft check-model` reports until you set one.

Operation events and `concurrent` follow the same rule, decided by what the file says about itself.
A component file this release writes states that it carries a runnable's operation events and its
concurrency, so an update from it replaces the target's — removing an operation event the file does
not declare, and resetting `concurrent` where the file says `false`. A file without that statement —
one an earlier release wrote, which could carry neither, or one another tool wrote — cannot say
*none*, so an update from it keeps each runnable's own operation events and `concurrent` where the
file's runnable of the same name says nothing about them, and the report lists what it kept under
*OPERATION EVENTS preserved from the target project*. A runnable of such a file that itself declares
an operation event or `concurrent: true` is compared, and an update replaces that runnable's with its
own. Timing and initialisation events are compared and replaced either way; `data` events follow a
statement of their own, and `call_returned` events follow the asynchronous calls' statement, both
above. Such a file whose only
difference from the target is that its runnables say nothing about operation events is therefore
reported unchanged. A file carrying the statement with a value this release does not write is refused
with nothing written: export the component again. An operation event a component file names but the
import cannot bind to an operation of the port's interface is a reference the project does not have,
below; one it cannot bind for another reason — a port the component does not declare, a require
port, an operation named under another interface's path — stops the component import, like any
other element it cannot carry.

### What the project already has { #what-the-project-already-has }

<!-- purpose: How a component import uses the interfaces and types your project already has -->

A component file need not carry everything its component uses: a file from another tool often names
an interface your project already holds and leaves the definition out. `import component` therefore
answers every reference of the component against the project the import would leave — first the
file's own definition at exactly the path the reference names, then your project's interface or
mapping set of the same name — an interface only when it is of the kind the reference names, so a
reference to a mode-switch or client-server interface is never answered by a sender-receiver
interface of that name. A definition the file carries under another path never answers, and
neither does one of your external interfaces by its name alone.

What the component **describes** — its ports with their communication specifications, the
interfaces and types the file brings, its description, how it handles termination and restart, and
every mapping set it names that the file or your project declares — is always written. What it
**does** — its runnables with their events, reads, writes and calls — is written only when every
reference it makes is answered. When one is not, the component is written without its `runnables`
and without each mapping set found nowhere, and the report lists each missing reference by layer and
element in a block of its own; nothing in that block is a path. Unsupported content in the file still stops the import with nothing written, as
above.

**Answered by your project.** The file leaves out the interface `ISig` that its port `rSig` uses,
and your project declares `ISig` with the data element the runnable reads. The port is written on
your interface, and since the import neither created nor reused it from the file, it is not listed:

```text
SWC Comp: created
CREATED 1 interface(s)
  - ISrv
REUSED 0 interface(s)
```

**Withheld.** Your project's `ISig` does not declare the data element `a` the runnable `R` reads.
The component is created with its ports, and its runnables wait until the project declares what they
use:

```text
SWC Comp: created
References the project does not have (1; the component's runnables and any mapping set listed below are withheld; its ports, interfaces, description and resolved mapping sets are written):
  - swc 'Comp' runnable 'R' read through port 'rSig' names the data element 'a', which the interfaces element 'ISig' does not declare
```

Add the element to the interface — or correct the file — and import again with `--update` to take
the runnables.

**Found nowhere.** Neither the file nor your project declares the interface at the path the port
names — or your project's interface of that name is of another kind. The port is written on an
[external](model.md) interface holding that path, as `import model` writes one, and named the same
way; where your project already holds an external interface for exactly that path and of the kind
the reference names, that one is reused and nothing is created; where it holds one for exactly that
path but of another kind, the import is refused naming both — one path cannot be two kinds of
interface, and a second external interface for it would hand your project that contradiction.
Every read, write, call or event
through such a port is a reference the project does not have — an external interface lists only
what communication specifications named, and nobody declared it — so the runnables are withheld:

```text
SWC Comp: created
References the project does not have (1; the component's runnables and any mapping set listed below are withheld; its ports, interfaces, description and resolved mapping sets are written):
  - swc 'Comp' runnable 'R' read through port 'rSig' names the data element 'a' of the external interface stub 'ISig_ext0', which neither the file nor the project declares
CREATED 2 interface(s)
  - ISig_ext0 (external interface stub)
  - ISrv
```

A reused one is marked the same way in the `REUSED` list. Its list of members is not changed by the
import: a communication specification on its port naming a member it does not list is a line under
*Not carried from the file*. A path whose references disagree on what kind of interface it is, or
that is not a valid AUTOSAR path, gets no external interface and stops the import, as before.

**Into a component your project already has.** When the file's runnables are withheld and your
project holds the component, your project's runnables, events, accesses, mapping sets and
termination handling are kept. Without `--update`, a component whose ports, interfaces and
description equal yours is reported unchanged, and any other difference is a conflict as always.
With `--update`, the file's ports and description replace yours over the kept behaviour, with the
same memory budget, safety level, package, measures and requirement links carried as on any update — but only when
nothing the kept runnables use breaks. A port they use that the file removes, a direction the file
changes, an interface without the element they read, a data element a by-value read now finds
queued: each is a conflict naming the reference, and nothing is written:

```text
Error: component import aborted with nothing written: SWC 'Comp' keeps the project's own behaviour (the file's is withheld), and the update would break it: SWC 'Comp' declares no port 'rOld', which runnable 'K' accesses (1 entry).
```

A component your project holds only as an external component — what `import model` writes for one
it could not read — has no runnables to keep, so with `--update` the file's component replaces it as
it would be created, its runnables withheld.

A problem your project already had before the import is left to `swcraft check-model` and does not
stop the update. A file whose runnables are all answered replaces yours exactly as described above.

What this does not reach: a data type the file names but does not declare is not looked for in your
project — the import still stops on it — and the tasks your runnables are mapped to are neither read
nor changed. Whether your tasks still map the component's runnables after an update is
`swcraft check-model`'s question. A component whose ports speak an application type or an
enumeration needs a mapping set that pairs it: a set the file or your project declares stays named
when the runnables are withheld, so the type stays paired, while a set found nowhere is listed among
the missing references and `check-model` reports the type it would have paired — and
`swcraft generate arxml` refuses — until you declare it. A port an update adds whose type your kept
mapping sets do not pair is reported by `check-model` after the update in the same way.

## Importing requirements { #importing-requirements }

<!-- purpose: Bringing requirements in from a requirements tool's needs.json, with a mapping and a dry run -->

```sh
swcraft import requirements --source needs.json --mapping team.needs-mapping.yaml --project swcraft.yaml --dry-run
swcraft import requirements --source needs.json --mapping team.needs-mapping.yaml --project swcraft.yaml
```

`swcraft import requirements` brings the requirements your requirements team delivers into the
model: `--source` names the file, `--mapping` the mapping, and `--dry-run` makes the run a check.
SWCraft reads one JSON file:
the `needs.json` the team's Sphinx-Needs build writes, as it is, or the same format written by a
converter from another tool (SWCraft ships no converter). The dry run is the complete check: it reads
the file, compares it with your model and prints what the import would change, writing nothing. Use
it first, every time. In VS Code, **SWCraft: Import Requirements** runs that dry run for you, always
shows its report, and imports only when you click **Write**
([In VS Code](vscode.md#importing-requirements)).

**The team's `needs.json`.** The requirements team builds it from its Sphinx project with the
`needs` builder:

```text
sphinx-build -b needs docs/ build/needs
```

which writes `build/needs/needs.json`. Files from Sphinx-Needs 7.0 to 9.2 are read as they are:
Sphinx-Needs' own keys on every need (`docname`, `lineno`, `type_name`, `parent_needs`, the `_back`
lists …) are ignored, and any other key your model does not hold is refused, naming it — so a
misspelt field is never dropped in silence. A key a later Sphinx-Needs release adds is refused until
SWCraft knows it, or until your mapping lists it under `ignore`. A team with several Sphinx projects
delivers **one** file: it pulls the other projects' `needs.json` into one project with `needimport`,
whose needs the builder writes. (Needs loaded with `needs_external_needs` are left out of the build's
file unless the team changes the builder's filter.)

**The mapping.** The team names its need types and fields its own way. `--mapping FILE` is a YAML file
of your own that says how they map to the names SWCraft uses:

```yaml
# team.needs-mapping.yaml - how the requirements team's needs map to SWCraft's.
types:                 # the team's need type (the directive name) -> req | spec | ignore
  sysreq: req
  swreq: req
  spec: spec
  test: ignore
fields:                # SWCraft's field <- the team's need key
  description: content # the default, written out only to show the shape
  asil: safety
  derived_from: parent
  document: docname
  revision: rev
  status: state
removed_status: [rejected, obsolete]   # these statuses remove the requirement; default [rejected]
ignore: [verifies, component, reviewer] # the team's own keys SWCraft does not hold
```

Every key is optional:

- `types` maps each of the team's need types — the directive name, `type` in the file — to `req`,
  `spec` or `ignore`. A need of an `ignore` type is not imported. When `types` is there, it is the
  whole list: a type it does not name is refused. Without it, only `req` and `spec` are accepted.
- `fields` names, for each of SWCraft's fields, the team's key it is read from: `title`,
  `description` (from `content` by default), `asil`, `document`, `revision`, `derived_from` and
  `status`. A field you leave out is read from its own name. Two fields may read one key
  (`description: title`, for a team that writes the requirement in its title), and `id` and `type`
  cannot be mapped: the id is the requirement's identity, taken character for character.
- `removed_status` lists the statuses that remove a requirement; without it, `rejected` alone.
- `ignore` lists the team's own keys SWCraft does not hold. Listing a link type (`verifies`) ignores
  its `verifies_back` mirror with it.

A mapping with an unknown key, a name listed twice, a key both read and ignored, a key written twice
or a YAML alias (`*name`; write the value out instead) is refused, naming the place (`Error: team.needs-mapping.yaml: fields/asil: …`). Without `--mapping`,
SWCraft's own names apply.

**Where to keep it.** Name the file so that it ends in `needs-mapping.yaml` —
`team.needs-mapping.yaml`, or just `needs-mapping.yaml` — and VS Code completes and checks it as you
type, with each key explained on hover; the command reads it under any name. Keep it **outside your
model directory**, best next to `swcraft.yaml`: a mapping inside the model directory is refused,
because the model's loader would read it as an element. It is never part of `swcraft.yaml`, which
describes the project, not an import's rules. Keep it out of any folder named `requirements`, too:
VS Code applies the requirement file's schema to every YAML file in such a folder, so it would flag
the mapping's keys as wrong, although the command reads the file all the same.

**The delivery's version.** Every requirement the import brings records which delivery it came from,
in its `baseline`. By default that is the file's own version, `current_version` — the Sphinx project's
`version` from `conf.py`. `--baseline TEXT` gives another, the team's commit for example:

```sh
swcraft import requirements --source ../reqs/build/needs/needs.json --mapping team.needs-mapping.yaml \
  --baseline "$(git -C ../reqs rev-parse --short HEAD)" --project swcraft.yaml --dry-run
```

A file that names no version (an empty `current_version`) and no `--baseline` is refused: every
delivery records its version.

**A delivery is the complete set.** A requirement you imported before that the new delivery no longer
holds — or holds as a type the mapping ignores — is **removed**, and the report names it and every
link in your model that now names no requirement, which the next `swcraft check-model` reports as an
error. One case is different: a requirement another requirement still derives from is not removed
but kept as a placeholder (a *stub*), at a raised version, so the links to it ask to be confirmed
again. A placeholder nothing names any longer is removed. A requirement that comes back after a
removal starts above every version its old links name, so those links stay unconfirmed. So one model
takes one requirements source: a second team's file would remove the first team's requirements.
**Read the dry run's report before you write.**

A requirement in a file written in SWCraft's own names looks like this:

```json
{
  "current_version": "SRS-2.3",
  "versions": {
    "SRS-2.3": {
      "needs": {
        "SWR-12": {
          "id": "SWR-12",
          "title": "Wheel speed plausibility",
          "content": "The software shall reject a wheel speed above 400 km/h.",
          "type": "req",
          "status": "approved",
          "asil": "B(D)",
          "document": "SRS Brake Control",
          "revision": "7",
          "derived_from": ["SG-1"]
        }
      }
    }
  }
}
```

`id`, `title`, `content` and `type` (`req` or `spec`) are required; `status`, `asil`, `document`,
`revision` and `derived_from` (the ids of the requirements this one is derived from) are optional,
and an empty `asil`, `document` or `revision` reads as absent. `version` and `source` are refused:
SWCraft keeps each requirement's version itself, and every imported requirement is written
`source: external`. The version named by `current_version` is the one imported; earlier versions the
file keeps are not checked beyond their shape. `content` is taken as written, markup included.
[The requirements interchange file](reference/requirements-interchange.md) carries the schema of
this form — the file without a mapping — so a converter's output can be checked before it is
imported.

In a parent link the team's build writes, a part (`SG-1.p1`) names its requirement and a condition
(`SG-1[status=="approved"]`) is dropped, as Sphinx-Needs reads them; a parent listed twice is read
once. So a dotted requirement id (`SR.3.1`) cannot be named in a link field, in Sphinx-Needs as here:
when the file holds the requirement `SR.3.1` itself, the import refuses the link and names it, because
Sphinx-Needs 9.2.0 cannot link such an id; when the file does not hold it, the link names `SR`.
A parent that holds no `.` and is itself the id of a need in the file (`SYS_REQ[12]`) is read whole:
the team's link type then sets `"parse_conditions": False`, which the file does not record, and a
file holding both `SG-1` and `SG-1[c]` gives `SG-1[c]`.

Imported into a model that holds no `SWR-12` and no `SG-1`, that file writes
`requirements/SWR-12.yaml`:

```yaml
id: SWR-12
title: Wheel speed plausibility
description: The software shall reject a wheel speed above 400 km/h.
req_type: req
version: 1
source: external
derived_from:
- SG-1@1
asil: B(D)
document: SRS Brake Control
baseline: SRS-2.3
revision: '7'
```

`status: approved` is read and not stored. `SG-1` was found neither in the file nor in the model, so
the import also writes it as a **stub** — a placeholder whose title is its id, marked `stub: true`,
at version 1 — and links `SWR-12` to it at `@1`. `swcraft check-model` warns about every stub, and a
later import that brings the real `SG-1` replaces it.

**What the import changes, and what it leaves.** Each requirement in the file is compared by `id`
with the model's requirement of that id:

- a new id is added at version 1;
- a requirement whose title, description, type, ASIL or set of parents differs is overwritten and its
  version raised by one, so every link confirmed against the old version asks to be confirmed again;
  leaving `asil` out clears the level, which is a change too;
- `document`, `baseline` and `revision` describe where the requirement came from: they are
  overwritten, or cleared when the file leaves them out, without raising the version;
- each parent is written at its version after the import, and a parent's version moving on its own
  does not raise the child's;
- `"status": "rejected"` removes the requirement — or, with a mapping, any status its
  `removed_status` lists; any other status is read and not stored;
- a requirement the file does not mention is removed, or kept as a placeholder while another
  requirement derives from it (above);
- every requirement the delivery holds records the delivery's version in `baseline`;
- only `source: external` requirements are changed. A file naming the id of one of your architecture
  requirements is refused, naming it, and that requirement is left as it is.

There are no modes and no `--force`. The source tool is the truth: an external requirement you
edited by hand is overwritten by the next import that differs from it.

**Fill `document`, and spell it the same way every time.** The trace link search
([Experiments](experiments.md#trace-links-which-elements-could-a-requirement-be-linked-to)) groups
requirements by their `document`, and a requirement's neighbours — the requirements of the same
document that are already linked — are the strongest evidence it has. A requirement without the key
falls into the group with no document: it has no neighbours, its first proposal comes from its text
alone, and the search's report marks it *low accuracy*. When the search was checked against real
tool exports the maintainer holds privately, linking a share of each document's requirements first
made the first proposal right far more often than the text alone did. That is no promise for
yours, and the search's labelling pass puts only a small share first by default (5 % of a
document's unlinked requirements). So a converter that writes this
file should put each requirement's source document into `document`, written identically every time:
two spellings are two documents.

**The report** is the same for a dry run and for the import itself. Its header names the
delivery's version — followed by `(from the file)` when it is the file's own — and the mapping, says
how many needs of types the mapping ignores were not imported, says when it is a dry run, and ends in
one summary line of five counts, always in this order. A dry run after the description of `SWR-12`
changed in your tool — a limit raised from 600 to 650 — with a component `ExampleSWC` that satisfies
`SWR-12@1`:

```text
Requirements import: baseline SRS-2.3 (from the file)
Mapping: none (SWCraft's own names)
Dry run: nothing was written.
Summary: 0 added, 1 changed, 0 removed, 0 stubs created, 0 source updates

Changed:
  SWR-12 v1 → v2 (description)
    text: numbers 600 → 650

Links now needing confirmation:
  SWC 'ExampleSWC' satisfies 'SWR-12@1'
```

A run that changes nothing ends with `No change.` after the summary. The sections, each printed only
when it lists something: *Added*; *Stubs created*, with the requirements naming each; *Changed*, with
the old and new version and the fields that moved, and under each entry what changed in it, or
*replaced by a stub* with its reason; *Removed*, with the version it had and why — `absent from this
delivery`, `status 'rejected'`, `type 'test' not imported` or `stub no longer named`; *Removed by
status, not in the model*; *Source information
updated*; *Parent versions updated*; *Links now needing confirmation*, the links written against the
version a requirement had before; *Links confirmed by this import*, links written ahead of the new
version; and *Links now naming no requirement*, the links to a requirement this run removed, for
whatever reason. The
links are read from every `satisfies` of components, their ports and runnables, the other elements
that carry one, and from the `derived_from` of the requirements the run leaves alone. `--report PATH`
writes the same report to a file — as Markdown when PATH ends in `.md`, as the printed text
otherwise; its folder must exist, and the import refuses before writing anything when it does
not.

**What changed in a requirement** is printed under its *Changed* entry, one line per kind of change,
each only when it applies:

- `text:` — the title and the description together: the numbers before and after (`numbers 600 →
  650`, `numbers removed 5; added 9`), the words added and removed (`terms added: overheat; terms
  removed: temperature`), or `wording only (the same terms and numbers; order, case, spacing,
  punctuation or common words changed)` when no term and no number was added or removed. A negation,
  a modal or an *only* that moved is named — *not*, *no*, *shall*, *may*, *must*, *can* and *will*
  among them — while other common words such as *at*, *for* or *the*, and a single letter such as
  the *s* of `60 s`, are not; words are lower-cased and not stemmed, so *brake* and *brakes* are two
  words, and a change of word order or of common words alone is *wording only* even when it changes
  the meaning — read the text itself before you confirm a link. A number is read as written (`600`, `-40`, `1.5`); `1,5` is two numbers, a number
  glued to letters (`SR2`) is none and its digits are compared as a word (`SR600` → `SR650` prints
  `terms added: 650; terms removed: 600`, even beside a `600` that moved as a number), and `SR2.5`
  reads as the number `5`;
- `type: req → spec`;
- `ASIL: B → D`, `ASIL set: D` or `ASIL cleared (was B)`;
- `parents added: …; removed: …`.

These lines are what you check a link against before you confirm it at the requirement's new version
([Experiments](experiments.md#deciding-and-applying)).

**What it does not do.** The import is not a check: it does not run `swcraft check-model`, so a link
it leaves dangling is in its report and then an error of the next `check-model`, and a link needing
confirmation is a warning there. A misspelt parent id becomes a stub rather than a refusal — look at
*Stubs created*. It records nothing beyond the files it writes, so an import shows in your version
control like any edit. It exits 0 when it imported or completed a dry run and 1 when it refused,
with nothing written. Among what it refuses: a file it cannot read, one that breaks the format
(naming the key, as a path into the file), a mapping it cannot read or that breaks its rules, a
delivery with no version, an id your model cannot hold, and an id one of your architecture
requirements holds. Like every command that
writes, it refuses a project pinned to another SWCraft release, the dry run included. If writing the
files fails part way, the files it already replaced are put back; only a run killed, or a machine
losing power, in the middle can leave a partial import, which version control shows.

## Experiments { #experiments }

<!-- purpose: Running an experiment, such as where a component could move, from the command line -->

```sh
swcraft experiment allocation --project swcraft.yaml --config experiments/rebalance.yaml
```

Answers a question about your model — today, which components could move to another partition and
what each move would buy. `allocation` is the kind of experiment; without `--config` it runs with
its defaults, and with it the configuration file narrows the question. The report goes under
`dist/experiments/`, in a directory named after the experiment (`allocation` unless the file names
it), or into the directory the file's `output` names inside the project. It changes nothing in the
model, and it is not a check: it refuses to run on a model `check-model` refuses, and like every
command that writes it refuses a project pinned to another SWCraft release. It exits 0 whenever it
could answer, including when no move improves anything or no component is free to move.
`clean-outputs` removes the reports under `dist/` with the rest of it — and a configuration file
stored under `dist/` with them, so keep yours elsewhere. [Experiments](experiments.md) is about
writing the question and reading the answer. The second kind, `trace`, proposes requirement links,
and its `--apply` form — the one form of `swcraft experiment` that writes into your model — writes the
links you decided ([Deciding and applying](experiments.md#deciding-and-applying)). Two more forms serve
the editor's Trace Links panel and work on their own: `trace --documents` prints each document's links
and the pass the configuration gives it and writes nothing, and `trace --labelling` chooses the labelled
documents for one search, as JSON (`'"all"'` or `'["Brake spec"]'`), without changing the
configuration file.

## This guide { #this-guide }

<!-- purpose: Opening this guide from your own installation, without a network -->

```sh
swcraft docs
```

Serves the copy of this guide that came with your install and opens it in a browser. Nothing is
downloaded and nothing leaves the machine: the pages are served to `127.0.0.1` alone, on a port
the system picks, so a second `swcraft docs` never collides with the first. Press `Ctrl+C` to
stop it.

`--no-browser` prints the address and leaves the browser alone; `--port N` asks for a
particular port instead of letting the system choose. If the install carries no guide, the
command says so and tells you which archive to unpack instead.

## The assistant's server { #the-assistants-server }

<!-- purpose: Starting the assistant's server for a project, for a chat client that is not VS Code -->

```sh
swcraft mcp-serve --project swcraft.yaml
```

Starts the server the editor's chat talks to. You rarely run this yourself — the SWCraft
extension registers the server with VS Code itself, for every open folder whose root holds a
`swcraft.yaml`, so for Copilot there is no configuration file to write and none to repair. Codex
and Claude Code read the server from files in the project, which **Onboard Project** writes —
[Codex and Claude Code in VS Code](vscode.md#codex-and-claude-code-in-vs-code) — and so does Kiro
([In Kiro](kiro.md#kiro)). If you do run it
by hand, read [The assistant](assistant.md) first, because a server started that way has to be
told how to reach a human before it will apply anything.

The conversational host is a subcommand of the same program:

```sh
swcraft agent chat --project swcraft.yaml
```

### The model guard { #the-model-guard }

<!-- purpose: What the model guard decides before an assistant's tool call, and why you never type it -->

`swcraft guard` is not a command to type: it is what an assistant's hook runs before a tool call.
It reads the call on its standard input and prints the decision, and it changes nothing. The
project's `.github/hooks/swcraft.json` runs it for Copilot, and `.codex/hooks.json` runs
`swcraft guard --host codex` for Codex, which answers in the form Codex reads and also judges
Codex's file edits and shell commands. The project's `.claude/settings.json` runs
`swcraft guard --host claude` for Claude Code: it judges Claude Code's reads, searches, edits and
shell commands, denies its `PowerShell` tool, and needs no git repository
([The model guard under Claude Code](vscode.md#the-model-guard-under-claude-code)). The project's
`.kiro/hooks/swcraft-guard.json` runs `swcraft guard --host kiro` for Kiro before every tool call;
Kiro reads no answer on standard output, so this one denies with exit code 2 and one line on its
standard error, and prints nothing to allow
([The model guard under Kiro](kiro.md#the-model-guard-under-kiro)). Each runs the
`swcraft` found on the path — for Codex, the path your login shell sets, and only in a project that
is a git repository — so that has to be this release. What each one denies and what it does not
reach is on [In VS Code](vscode.md#the-model-guard); every rule, for every assistant, is on
[The model guard's rules](model-guard.md).

## In a build pipeline { #in-a-build-pipeline }

<!-- purpose: Setting up a build pipeline: the commands in the order that works, and why -->

The order that works, and the reason for it:

```sh
swcraft clean-outputs --project swcraft.yaml
swcraft check-model   --project swcraft.yaml
swcraft generate diagrams --project swcraft.yaml
swcraft generate needs --project swcraft.yaml
swcraft validate      --project swcraft.yaml
```

`clean-outputs` first, so the previous run's artifacts are gone from their own names before this
one starts. That matters most when the run then fails: without it, a build that stopped at
`check-model` leaves yesterday's ARXML and diagrams in place, indistinguishable from today's. It
moves the previous ARXML aside as `dist/model_old.arxml` and prints `Previous ARXML kept:` with its
path, so one earlier copy stays at hand to compare with, and removes the rest of the two directories the manifest declares (`dist_root` and
`views_root`) and nothing else; it keeps `dist/audit.jsonl` too — the assistant's record of every
model change is history, not build output. The kept copy is the previous ARXML that was
*written*, which is not necessarily one that passed. It is kept one level deep only: an ARXML the
manifest places in a subdirectory of `dist_root`, or directly in `views_root`, is not moved, and
when `dist_root` lies inside `views_root` the copy is removed with it. The scaffolded `moon.yml`
makes it the graph's first task, with `cache: false`.

`generate arxml` and `validate` make the same move themselves: before writing, an existing ARXML
whose content differs from the new one moves to `model_old.arxml` and the command prints
`Previous ARXML kept:` with its path. When the content is the same, as when `validate` regenerates
what `generate arxml` just wrote, nothing moves. A one-off `generate arxml --arxml-version` run
followed by a plain `validate` does move the other version's file aside, because its content
differs. The per-component exports are never moved aside.

`check-model` next, so a bad model fails the pipeline before anything is generated and without
generating anything. Then whatever outputs the pipeline publishes. Then `validate`, which
regenerates and checks the ARXML, as the last word. Give it `--report` and it also leaves
`dist/build-report.md` for a person to read ([above](#the-build-report)); `clean-outputs` removes
that report with the other outputs, so a run that fails before `validate` leaves no report of an
earlier run behind. The report decides nothing: the exit code does.

Only `check-model` and `validate` — the second and the last — decide anything. Add `resource-report` for the log if
you want the budgets visible in the output — but read its number, not its exit code.

## Reporting a problem without sending the model { #reporting-a-problem-without-sending-the-model }

<!-- purpose: Reporting a problem with a model passport, without sending the model itself -->

```sh
swcraft passport --project swcraft.yaml --output passport.json
```

Writes a model passport: a small JSON file that describes the shape of your model -- how many
components, ports, interfaces, data types, connectors and the other elements it holds, and how they
are put together -- without naming any of them. It carries no element name, no value and no text
from the model, so it can go with a problem report when the model itself cannot leave your
machine. Without `--output` the passport goes to standard output, so you can read every number
before you send it.

A model `check-model` rejects still gets its passport: a broken model's shape is often what the
report needs. When a model file cannot be read at all, the command fails and says which layer and
what kind of error, never the file's name or its content; `swcraft check-model` tells you which
file it was. What the passport cannot carry: anything the counts do not show, such as a problem
that depends on a particular name or description.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next: every option, the model passport and every check -->

| If you want to | Read |
|---|---|
| every option, argument and default | [The command line](reference/cli.md) |
| what a model passport holds, field by field | [The model passport](reference/model-passport.md) |
| what each check requires | [The checks](reference/rules.md) |
| what goes in the files these commands read | [Working with the model](model.md) |
| let an assistant run them for you | [The assistant](assistant.md) |
| see the state of the architecture and where it is heading | [Architecture metrics](metrics.md) |
| ask where a component could move | [Experiments](experiments.md) |
