<!-- draft: not yet reviewed by the product's author -->

# Experiments: where could a component move?

<!-- purpose: When you want to know where a component could move, or which elements a requirement fits -->

Some questions about an architecture are ones you ask yourself rather than ones a check can
answer. The first one SWCraft answers is about allocation: **which components could move to
another partition, and what would the move buy?** You run one command — with a small file that
narrows the question, or without one — and read a report. Your model is never changed — the experiment proposes, you decide, and
if you want a move you make it the way you make any other change.

A second experiment is about traceability: **which elements could each requirement be linked to?**
It is described in [Trace links](#trace-links-which-elements-could-a-requirement-be-linked-to)
below.

## What it reads from your model { #what-it-reads-from-your-model }

<!-- purpose: What an allocation experiment reads from your model: partitions, cores, tasks and signals -->

An experiment reads what your model already declares: the partitions on the hardware, the cores
they sit on with their CPU limits, the tasks in each partition with their periods and budgets, the
runnables each task runs, the components' safety levels, and the connectors between components.

The figure an experiment needs most is a runnable's execution-time budget, **`wcet`**, with its
**`wcet_type`**: what you allot to one execution of the runnable, written as a whole number and its
unit (`ns`, `us`, `ms` or `s`).

```yaml
# model/swc/body/SeatHeater.yaml (an excerpt)
runnables:
  - name: SeatHeater_Run
    events:
      - type: timing
        period: 0.01
    wcet: 120us
    wcet_type: gross
```

Both are optional and are declared together. [The model](model.md) describes the format, the checks
`swcraft check-model` makes whether or not you ever run an experiment, and the timing description
the generated ARXML carries for them. A component exchange keeps the budget too: `swcraft import
component` compares it like any other field, so the file's budget wins, and a runnable the file
brings without one — an export made before SWCraft wrote budgets — keeps the budget your project
declares for the runnable of the same name, which the report lists when the import writes the file.

A component one of whose runnables declares no `wcet` cannot be moved, and the report says why.
Nothing is guessed: an unknown cost is not a cost of zero. `gross` and `net` budgets are added
alike, so the calls a `net` budget leaves out are counted in no load; the report lists every `net`
budget it added under **Data gaps and assumptions**.

## Writing the question { #writing-the-question }

<!-- purpose: Narrowing an allocation question in a configuration file: moves, candidates and locks -->

An experiment needs no file at all: without one it answers with its defaults — up to three moves,
three candidates, nothing locked. When you want to ask something narrower, write the question in a
small configuration file:

```yaml
# experiments/rebalance.yaml
name: rebalance                      # the report goes to dist/experiments/rebalance/ (default: allocation)
output: dist/experiments/rebalance   # where the report goes, inside the project, never under .git (default: from name)
search:                              # how to search
  max_moves: 3                       # answer for 1, 2 and 3 moves (default 3)
  candidates: 3                      # how many alternatives to list (default 3)
  candidates_at: 3                   # at which number of moves (default: max_moves)
  tolerance: 0.05                    # alternatives up to 5 % worse than the best (default 0)
  time_limit_s: 10                   # the time each number of moves may take, in seconds (default 10)
constraints:                         # what must not be touched: names from your model
  locked: [SafetyMonitor]            # never move these
  allowed_partitions:                # move this one only into these partitions
    DiagnosticsMonitor: [P1, P3]
```

Every key and every section is optional, and an empty file asks exactly what no file asks. The
kind of experiment is not written in the file — the command names it. A key the experiment does not
know is refused, naming it the way the file nests it (`search.max_move`) — a misspelled key would
otherwise quietly answer a question you did not ask. So is a component or a partition your model
does not declare.

**Where the file lives is your choice.** If you want your questions versioned, keep them in a
directory of your own — `experiments/` beside your `swcraft.yaml` is a good name — and commit it; a
new project ignores nothing there. Keep it **outside `dist/` and the diagrams directory** (the
manifest's `dist_root` and `views_root`): `swcraft clean-outputs`, and the first step of every
build, removes those two directories whole, a configuration file stored in them included. A
configuration file survives only when it is stored somewhere else.

## Running it { #running-it }

<!-- purpose: Running an allocation experiment, and where its report.md and result.json are written -->

```sh
swcraft experiment allocation --project swcraft.yaml
swcraft experiment allocation --project swcraft.yaml --config experiments/rebalance.yaml
```

`allocation` is the kind of experiment, and today the only one. The first line runs it with its
defaults; the second with your file, whose path is read from the directory you run the command in,
like `--project`'s.

The command first checks the whole model as `swcraft check-model` does, and refuses a model with
error findings, saying how many: an experiment over a model that already breaks a rule would answer
nothing. A warning — a requirement that no element satisfies — is listed by `swcraft check-model` and
does not refuse an experiment. Then it answers the question and prints one summary line:

```text
Experiment 'rebalance': inter-partition volume 1200 bytes x Hz now; N = 1: 800 (optimal); N = 2: 400 (optimal); N = 3: 0 (optimal); 3 candidates at N = 3.
```

and writes two files: `report.md`, to read, and `result.json`, the same content for a script.
`--format json` prints that document instead of the summary. `report.md` shows a figure to two
decimals, cut rather than rounded, with `~` in front when it is not exact — a core's load and
threshold as a percentage, as the resource report shows them; `result.json` keeps every figure
exact.

**Where the report goes.** Without `output`, into `dist/experiments/<name>/` — with no `name`,
`dist/experiments/allocation/` — so `swcraft clean-outputs` removes it with the rest of `dist/`, and
the `.gitignore` that `swcraft init --integration moon` writes keeps it out of a commit. With
`output`, into that directory, written relative to your project directory and created if it is not
there. The command refuses an `output`
that is written as an absolute path, that is the project directory itself, that lies outside it, that
lies inside your model directory or contains it, or that names a file rather than a directory (or a
directory below a file). It also refuses an `output` inside `.git`, and an existing directory that
holds a `report.md` or a `result.json` no experiment wrote — the message names the file, and nothing
is replaced: point `output` at another directory, or move that file away. A directory where an
earlier experiment run wrote its report is fine, and the new run replaces that report. The default
directory is held to the first rules, so a link under `dist/` that leads out of your project or into
your model is refused too, before anything is solved. **What those refusals do not reach:** a
`result.json` you wrote by hand in a run's own shape counts as a run's; a run's `report.md` you edited
afterwards is still replaced; other tool directories, such as `.vscode`, are not refused; and the
default directory under `dist/` is not checked for files of your own. Nor does anything keep a report
under `output` from being removed by `clean-outputs` when that directory lies inside `dist/`, or
committed when it lies anywhere else.

**What the report records.** Both files say what they answered: the kind, the configuration file by
its path inside your project and its SHA-256 — or *defaults, no configuration file* — the directory
they were written to, and every setting the run used, defaults filled in (`search.max_moves: 3`,
`constraints.locked: SafetyMonitor`). Two reports can therefore be told apart without the file that
produced them. A configuration file outside your project is recorded by its file name alone.

**Links to your model.** In `report.md`, the name of a component, a partition or a task where a move
names it is a link to that element's file in your model, relative to the report, so a Markdown
preview opens it in one click. The table cells that list several names, and every name in
`result.json`, stay plain. A link points at the file that declared the element when the report was
written, after the search, so a file moved or renamed while the run was under way can give a link
to a file the run's fingerprint does not describe, and a report copied to another place, or read
after the model's files moved, can hold links that lead nowhere.

## Reading the report { #reading-the-report }

<!-- purpose: Reading an allocation report: the traffic it reduces, each candidate move and its cost -->

**The number being reduced** is the data exchanged between partitions: for every signal a component
sends to a component in another partition, its size times how often it is sent, in bytes per second.
Sending across a partition boundary costs far more than sending inside one, so less is better. The
size is the logical size of the data type — no alignment, padding or bus overhead — and how often is
the period of the runnable that writes the signal, or, where none writes it or none that writes
it has a timing event of its own, the fastest timing period of the sending component, marked
*estimated*. Client-server calls across partitions are
counted before and after each move, but not weighed into the number.

**The freedom table** lists, for every component, every other partition and whether the component
may move there. Each "no" carries its reasons and a class that says what would change the answer:

| Class | What it means |
|---|---|
| hard | the hardware forbids it: the core is not able to host the component's safety level, or a memory the component uses is not reachable from that core |
| needs a safety decision | the move would break a safety rule — a partition rated below the component, one rated above it without its coexistence argument, a task that would mix levels without its coexistence argument, a memory not able to host the partition's level. The experiment never proposes such an argument; it is yours to make or not |
| removable | the partition has no task of the runnable's period with a declared budget — declaring one would open it |
| locked | your configuration file excludes it |

A component that cannot move at all is listed with the reason: placed more than once or nowhere in
the root composition, an external stub, a component with no runnable, a cyclic runnable in no task,
a runnable with no event at all, runnables in two partitions, or a cost or a budget not
declared. Initialization, server, data-received and call-returned runnables are judged by what they
are:

- An **initialization runnable in no task** — it runs once at start-up — is fine: the component's
  partition comes from its other runnables, and the report lists the runnable among the
  assumptions as outside the steady load.
- A **server runnable in no task** runs in its callers' tasks, at a cost the experiment does not
  weigh, so the component stays where it is; its partition still counts for the data and the calls
  that cross it. A component that calls it can still move: its call is then counted as crossing
  partitions, but whether that call is allowed there is not judged, and the server's time in the
  caller's task is not added to the caller.
- An **initialization or server runnable mapped to a task** keeps the component where it is too:
  moving an initialization mapping, or a server's load at its callers' rate, is not judged.
- A **data-received runnable** — one a `data` event starts, with no timing and no operation event —
  runs when its data arrives, at a rate the model does not hold. Mapped to a task, it keeps the
  component where it is and is never costed. In no task, it is a runnable in no task like a cyclic
  one: nobody calls it from another task, so it needs a task of its own. A timing event beside it
  makes it a cyclic runnable; an operation event beside it, and no timing event, a server runnable.
- A **call-returned runnable** — one a `call_returned` event starts, with no timing, operation or `data`
  event — runs when an asynchronous call returns, at a rate the model does not hold. Mapped to a task,
  it keeps the component where it is and is never costed. In no task, it is a runnable in no task like
  a cyclic one: nobody calls it, so it needs a task of its own. A timing event beside it makes it a
  cyclic runnable; an operation event and no timing event, a server runnable; a `data` event and
  neither, a data-received runnable.
- A component whose runnables are **all** initialization or server runnables in no task has no
  partition the experiment can tell, so it cannot move and its data and calls are left out, with
  that reason.

A partition with no safety level declared is admissible, and listed among the assumptions.

**The curve** gives the best result for 1, 2, … up to `max_moves` moves. Read it for where the gain
flattens: if two moves get almost all of what three get, the third may not be worth its review.
Each point says how sure it is: **optimal** (the solver proved it best, and SWCraft re-checks every
answer exactly), **feasible** (the time limit ran out after
a solution was found; the best known, not proved), **unknown** (the time limit ran out first).
Among equally good answers a point always gives the one with the fewest moves: a move that buys
nothing is never part of it. When no number of moves brings the data exchanged below what it is
now, the summary says *no improvement at any N* and there are no candidates. The curve stops at the
number of components that can move at all: a larger `max_moves` would only repeat the last point,
so the report says so in one sentence instead.

**The candidates** are alternatives at one number of moves, best first. Each lists the moves — which
component, from which partition to which, and into which task each of its periodic runnables
would go (only a periodic runnable is ever moved) — the load of
every core it touches before and after, the data exchanged before and after, the connections that
stop or start crossing a partition boundary, and **what applying it takes**: the `wcet` budget
edit of every task it touches, one per task, exact and written in the finest unit among the task's
budget and the budgets that move (`2ms -> 1500us`). When proved optimal, they are within your `tolerance`
of the best — the best first, then by the data exchanged, then by the number of moves; at a point
the time limit cut short there is one candidate and no such claim. A list the time limit cut short
— the report says so — may hold fewer, and after the best one need not hold the next best. Every
candidate exchanges less data than your model does now, whatever the `tolerance`, and none repeats
an earlier, better candidate's moves plus more. A candidate with fewer moves may still be part of a
better one listed before it: it is fewer moves for less gain.

Every candidate has been checked: before it is reported, the moves are applied to a temporary copy
of your model and the whole copy is run through the same checks as `swcraft check-model`. A move
set the checks refuse is not offered — it is listed under **Dropped move sets** with the rule that
refused it. So is a move set whose budget edit would need more than the 18 digits a budget holds:
it is dropped with that reason, never rounded.

**Data gaps and assumptions** closes the report: what it assumed (a runnable costs the same on every
core; memory stays where it is; no task is created or re-budgeted), and every component, connection
or move it could not judge, with the reason.

## What it never does { #what-it-never-does }

<!-- purpose: What an allocation experiment never does to your model, its tasks or their budgets -->

- It never changes your model. Applying a candidate is your edit, and the checks will judge it.
- It never creates a task, moves a whole task, or changes a budget. A move whose task would be left
  with no declared runnable budget keeps that task's budget unchanged, and says how much the move frees.
- It never proposes a safety argument, and never moves a component into a partition the safety
  rules forbid.
- It is not a check: `swcraft check-model` and `swcraft validate` decide whether a model is allowed
  out, and an experiment decides nothing.

## When it refuses { #when-it-refuses }

<!-- purpose: When an allocation experiment refuses to run, and what each refusal means -->

The command exits with an `Error:` line and writes nothing when the project cannot be read, the
model has an error finding (a warning does not refuse it), the configuration file cannot be read or
is refused — a key it does not know, a
value out of range, a name your model does not declare, an `output` it refuses — the solver it uses
is not installed or cannot be loaded, the numbers in the model are too large for the solver to
represent exactly, the solver fails to run the experiment, or the project is pinned to another
SWCraft release. A refusal names the key as the file nests it, never where your project is stored.
A missing kind, or a kind other than `allocation` and `trace`, is a usage error and exits 2 before
anything is read. Every option is on [The command line](reference/cli.md); what the checks require is on
[The checks](reference/rules.md), and the safety levels the freedom table reads are explained in
[Safety levels](safety.md). In VS Code the panel's **Experiments** section runs the same command —
[In VS Code](vscode.md#experiments) says how.

## Trace links: which elements could a requirement be linked to? { #trace-links-which-elements-could-a-requirement-be-linked-to }

<!-- purpose: Finding which elements a requirement could be linked to with the trace link search -->

`swcraft experiment trace` reads your requirements and every element that can carry `satisfies` —
components, ports, runnables, interfaces, compositions, tasks and hardware elements — and proposes,
for each requirement, a short ranked list of elements it could be linked to, each with the reasons
it was proposed. The search never changes your model: you decide which links to make, and a
separate command, `--apply`, writes the ones you accepted (see
[Deciding and applying](#deciding-and-applying)).

```sh
swcraft experiment trace --project swcraft.yaml
swcraft experiment trace --project swcraft.yaml --config experiments/trace.yaml
```

It writes three files under `dist/experiments/trace/` (or under its configuration's `name` or
`output`): `report.md` to read, `result.json` with the same content, and `proposals.json`, the list
of proposals in a form tools can read. **Each search rewrites `proposals.json` whole, unless it holds
decisions not applied** — then the search refuses, as *When it refuses* below says.

### Writing the question { #writing-the-question_1 }

<!-- purpose: Writing the trace link search's configuration: candidates, passes, documents and element kinds -->

Every key is optional; without a file the search runs on its defaults and reviews every requirement.

```yaml
# experiments/trace.yaml
name: trace
search:
  candidates: 5                # how many places each requirement gets
  labelling: [Brake spec]      # documents given the labelling pass; `all` for every one
  labelling_share: 0.05        # how many of a labelled document's unlinked requirements come first
constraints:
  documents: [Brake spec]      # only these documents
  without_document: true       # whether requirements with no document are searched
  requirements: [SR-12]        # only these requirements
  target_kinds: [swc, port]    # only these kinds of element are proposed
  exclude: [DiagManager]       # never propose these elements (a component's ports and runnables too)
```

The assistant can ask the same search for one requirement. It always uses the default
configuration — never one you pass with `--config` — so a project that searches with its own
configuration can see a different ranking from the assistant than from the command line. It keeps
out what `trace-decisions.yaml` rejects and drops the project's name words, as the command does,
writes no report or proposals file, and a link you accept from it is an ordinary proposal you
review.

To see the documents before you choose, `swcraft experiment trace --documents` prints each document
with its requirements in scope, how many are linked, awaiting confirmation or without a link, how many
pairs were rejected earlier, and the pass this configuration gives it; it writes nothing. To label other
documents for one search without editing the file, pass `--labelling` as JSON — `--labelling='"all"'`,
`--labelling='["Brake spec"]'` or `--labelling='[]'`; it replaces `search.labelling` for that run, is
checked like the key, and the report's *Fingerprint* marks it *from the command line*. The editor's
Trace Links panel starts its searches this way ([In VS Code](vscode.md#trace-links)).

A requirement's `document` groups it with the other requirements of the same source document, and
the links those already have are the strongest evidence the search has. A requirement with no
document, or whose document has no other linked requirement, is ranked from its text alone and is
marked *low accuracy: text evidence only*.

### Two passes: review and labelling { #two-passes-review-and-labelling }

<!-- purpose: Choosing a document's pass: review its existing links, or label requirements not yet linked -->

Each document gets one pass.

- **Review** (the default) lists every requirement of the document with its candidates — linked
  requirements too, which may need further links.
- **Labelling** is for a document whose requirements are mostly unlinked. It lists every requirement
  of the document, in the order that teaches the search the most: first a few to link first
  (`labelling_share` of its unlinked requirements, at least one) — one for each element the document's
  requirements point at most clearly, then the ones the search is least sure about, each saying why —
  then the rest of its unlinked requirements in the same order, then its linked ones. Link the first
  ones, run the search again, and the links you made strengthen the evidence for the rest.

### Reading the report { #reading-the-report_1 }

<!-- purpose: Reading the trace link report: each requirement's places, their scores and their reasons -->

Each requirement shows its existing links and up to five **places**. A place is one element — or one
component together with its ports and runnables, of which at most three are listed — with its score
and its reasons. Every listed element has its own score, its reasons and a **band**:

- **strong** — its name is in the requirement's text, or most of the document's linked requirements
  are linked to it;
- **medium** — at least three other requirements of the document are linked to it (fewer count when
  its own text supports it too), linked requirements with similar text are, or it is the
  requirement's best text match;
- **weak** — it shares only some words with the requirement.

A band says how strong the evidence is. It is not a probability of being right.

The report also lists the document groups with how many requirements are linked, awaiting
confirmation or without a link; the model's findings by rule (the search runs even when
`swcraft check-model` has errors, and lists them); the requirements nothing was found for; and
**Recovering the existing links**: how often the search finds the links your model already holds
when each requirement's own links are hidden. That is a measure on requirements someone already
linked, which tend to be the easy ones — not a promise about the others.

### Deciding and applying { #deciding-and-applying }

<!-- purpose: Accepting or rejecting proposed links in proposals.json, and applying them to the model -->

You decide in `proposals.json` itself — by hand, or in the editor's **Trace Links** panel, which ticks,
rejects and applies for you ([In VS Code](vscode.md#trace-links)). Each candidate
of a requirement has a `decision` that starts as `null`:

- set it to `"accept"` to link the requirement to that element, or to `"reject"` to say it does not
  belong there; a tool that ticks rows for you in bulk adds `"preselected": true` beside an accept it
  made, so the decisions file can tell it from one you made one by one — it belongs on an accept
  only, and a file with it on a rejected or an undecided row is refused;
- to link an element the search did not list, add `{"target": {"kind": "port", "owner": "Ctl",
  "name": "pT"}}` to the requirement's `chosen` list (`owner` is the component of a port or a
  runnable, and `null` for every other kind);
- a link listed as awaiting confirmation (its requirement changed since the link was made) has a
  `decision` too: set it to `"confirm"` once you have checked the link still holds.

Apply marks what it did in the same file: `"applied": true` on a decision it carried out (or found
already done), and `"stale": "<reason>"` on one it could not. Leave the marks as they are: a marked
decision is never applied again, a mark sits only on a decided item and never both on one, and a
file that breaks either rule is refused. To decide a stale item again, search again: the next
search writes a fresh file.

Then look at what would be written, and write it:

```sh
swcraft experiment trace --project swcraft.yaml --apply dist/experiments/trace/proposals.json --dry-run
swcraft experiment trace --project swcraft.yaml --apply dist/experiments/trace/proposals.json --expect-plan sha256:...
```

The dry run writes nothing. It prints what the applied run would do, ending its header in one line
of counts and a `Plan:` line:

```text
Trace apply: proposals of the search 'trace'
Dry run: nothing was written.
Summary: 3 written, 1 confirmed, 2 rejected, 0 already done, 1 stale, 0 not written, 40 undecided; 3 files
Plan: sha256:…
```

and below it the links to write, the links to confirm, the rejections to record, what was already
done, what could not be applied and why, every component whose allocated safety level the new links
raise, and every link it leaves for you to add by hand. Apply adds only the `satisfies` lines to a
file — every other line, your comments, your key order and your quoting included, stays as it was —
and a `satisfies` list written in a form it does not edit (a list in brackets over several lines, a
value with an anchor or a tag, a member listed twice, a file with Windows line ends) is left
alone: its links are listed under *Links not written (edit by hand)* with the reason, neither
written nor marked, so they stay in the file until you add them and Apply again. Pass the `Plan:` value to `--expect-plan` on the
applied run: if anything changed in between, it refuses and writes nothing, so what you previewed is
what is written. The applied run writes everything or nothing.

To keep the report, add `--report PATH` to either run. It is written after the run: a `PATH` ending
in `.md` gets the report as a document with a table per group, any other `PATH` what the run printed —
the text, or with `--format json` the JSON document.

Every decision is checked against your model as it is now, not as the search read it. A decision
whose requirement or element has changed, been renamed or been removed since the search is not
applied: it is listed under *Stale* with its reason, marked in the file, and the next search proposes
the requirement afresh. A link already present is counted as *already done*, and so is a rejection
already recorded in `trace-decisions.yaml` for the requirement's current version (*already rejected*).
Running Apply twice
writes nothing the second time, and it never writes back a link you removed by hand after an Apply.

**The decisions file.** Apply records every decision it carries out in `trace-decisions.yaml` beside
`swcraft.yaml`. Keep it in version control: it is how a reviewer can tell a link accepted one by one
from one accepted from a preselection. A **reject** keeps that element off the requirement's list for
as long as the requirement stays at the version you rejected it at; when the requirement changes, the
pair is proposed again, because you judged the old text. Deleting a reject from the file by hand
proposes the pair again too.

**Re-confirming after an import.** When `swcraft import requirements` changes a requirement, the
links made against its old version await confirmation. The import's report says what changed in each
requirement — the numbers before and after, the terms added and removed, the safety level — which is
what you confirm against (see [Importing requirements](cli.md#importing-requirements)). Set those
links' `decision` to `"confirm"` in the next search's `proposals.json` and apply: the link moves to
the requirement's new version. The assistant can do the same in one proposal: it updates the
requirement's `version` and asks SWCraft to confirm the links, and every link that moves is shown to
you before anything is written.

Apply each portion of decisions before searching again: a new search refuses to overwrite a
`proposals.json` that holds decisions not yet applied, or one it cannot read as a proposals file,
and refuses too when that file changed while it ran (a tick saved meanwhile); `--discard-pending`
makes it overwrite it. `swcraft clean-outputs` and a build empty the two
output folders `swcraft.yaml` names — `dist_root` (`dist/` by default, where the report folder is
unless a configuration's `output` names another) and `views_root` (`views/` by default) — so a
report folder inside either goes without asking, decisions not yet applied included: the default
`dist/experiments/<name>/`, and an `output` such as `views/trace` too. A folder an `output` names
elsewhere in the project (`reports/trace`) is left alone.

### What it never does { #what-it-never-does_1 }

<!-- purpose: What the trace link search never does to your model or your requirements -->

- A search never writes a link into your model, and nothing it does changes a requirement; only
  `--apply` writes links, and only the ones you decided.
- It never proposes an element that only stands for something outside your model (an external stub),
  nor its ports and runnables.
- It is not a check: `swcraft check-model` and `swcraft validate` decide whether a model is allowed
  out.

### When it refuses { #when-it-refuses_1 }

<!-- purpose: When the trace link search refuses to run, and what each refusal means -->

The command exits with an `Error:` line and writes nothing when the project cannot be read, when two
elements of the same kind share a name or two requirements share an id (a proposal could not say
which one it means), when the configuration file cannot be read or is refused — a key it does not
know, a value out of range, a document, requirement or element name your model does not have, an
`output` it refuses — when a `--labelling` value is not JSON, holds a number too long to read, is
neither `"all"` nor a list of documents, or names a document the model or the configuration's `documents` does not have, when
`trace-decisions.yaml` is malformed or cannot be read, when the previous
`proposals.json` holds decisions not yet applied or cannot be read as a proposals file (unless you
pass `--discard-pending`), or when the project is pinned
to another SWCraft release. Every other finding is listed in the report and does not stop the search.

`--apply` exits with an `Error:` line and writes nothing when the proposals file or
`trace-decisions.yaml` is malformed or cannot be read (naming the place in the file), when the proposals file is
outside the project, when the project cannot be read, when the plan differs from the one
`--expect-plan` names, when a file changed while it ran, or when the project is pinned to another
SWCraft release, the dry run included. If writing fails part way, the files already replaced are put
back, and the error names any file that could not be. A dry run, an applied run, a run with nothing
to do and a run whose every decision is stale all succeed.
