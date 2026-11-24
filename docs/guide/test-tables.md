<!-- draft: not yet reviewed by the product's author -->

# Test tables: the placement and scheduling the test team verifies

<!-- purpose: Handing the test team tables of components, placement and scheduling, and keeping their review -->

The test team verifies the software against the architecture: which component runs where, in which
task, how often, on which core. SWCraft writes that part of the architecture as **test tables** — one
row per thing to verify, in plain CSV files a spreadsheet tool opens — and the test team adds its own
columns beside SWCraft's: how a row is verified, a comment, accepted or not.

Their columns are **theirs**. SWCraft never writes into the team's files: you take their folder back,
generate the tables again from the current model, and SWCraft joins their columns to the new rows by
each row's identity. A row whose facts changed since the team accepted it is marked `stale`, so a
review is never silently carried over a change it did not see.

Every value in the tables is computed from the model. The same model and the same annotations give
the same bytes, and nothing in the tables is written by AI.

## In VS Code: one click { #in-vs-code-one-click }

<!-- purpose: Generating and reading the test tables in VS Code: the Test Tables item and its read-only tab -->

The SWCraft panel's *Model* section has **Test Tables**, directly under **Diagrams** (and the palette
*SWCraft: Test Tables*). Click it: it generates the tables again from the model as it is now, with
`swcraft generate test-tables`, and opens them in a tab of their own. While it runs, the item reads
`running…`; afterwards it says `generated` and the time, or `failed`. The command's own output goes to
the **SWCraft** output channel, like every other panel item's.

**The tab is read-only, and that is on purpose.** The tables are a generated result, like the
diagrams: you look at them, and to change one you change the model and press **Test Tables** again.
The CSV files are what goes to the test team, so nothing in VS Code edits them — not the tab, and not
SWCraft. (Opening a CSV file in a plain editor tab works too, but an editor that lines the columns up
for you does it by writing spaces into the file; the tab shows the same text without touching it.)

### The annotations folder { #the-annotations-folder }

<!-- purpose: The first press's question for the test team's folder, where the answer is kept, Change… -->

The first time you press **Test Tables**, a list asks for the test team's **annotations folder** — the
folder holding the tables they handed back ([below](#the-round-trip-with-the-test-team)):

- **Choose a folder…** opens a folder dialog at the project directory;
- **None yet (no annotations)** generates the tables without any — the usual answer before the first
  hand-over;
- once a folder was chosen, the list offers it first, marked *last used*.

Your answer is remembered at once in the setting `swcraft.testTables.annotations`, in the workspace
folder's settings (`.vscode/settings.json`, which your version control shows): a folder inside the
project as a path relative to the project directory, one outside it as an absolute path, and *None
yet* as an empty value. Only that file's value is read: one set in your user settings is not used.
After the first answer, a press asks nothing and generates with the folder you chose — or without
annotations, if you chose *None yet*. Escape ends the press, and nothing runs.

To choose again, click **Change…** at the top of the tab: it asks the same question, then generates.
The top line names the folder as you stored it — a project-relative path, `none yet`, or `a folder
outside the project` followed by its last name, so the tab never shows where your files are stored.

What the folder may be is the command line's to decide, and it refuses a folder inside the model,
`dist/` or `views/` ([below](#when-it-is-refused)). The refusal reaches the tab as any refusal does.

### What the tab shows { #what-the-tab-shows }

<!-- purpose: Reading the Test Tables tab: the sub-tabs, the row colours, the frozen column, shortened values -->

The top line names the project, the SWCraft release that wrote the tables and the annotations
folder, with **Change…** and **Generate** beside them, and under it each table's counts — rows,
accepted, `stale`, rejected and not reviewed — and the number of orphaned annotations.

Below are four sub-tabs, one per file: *Components*, *Component placement*, *Runnable scheduling* and
*Orphaned annotations* with its row count. Each shows its file as a table:

- **each row is coloured by its review state**: accepted green, `stale` orange, rejected red, a row
  nobody has reviewed left plain. The colours are your colour theme's own, softened behind the text,
  and each coloured row has a bar of its colour at its left edge. The `review_state` cell holds the
  state in words as a coloured label, so no meaning rests on the colour alone. Plain rows are striped,
  and the row under the pointer is highlighted. In a high-contrast theme the rows are not filled: the
  label is framed in the state's colour instead;
- **the header stays at the top** while you scroll down, and **the first column stays put** while you
  scroll sideways — the row's identity, `row_id` (`table` on the orphan tab) — so you always know
  which row you are reading. Scrolling sideways stops with a column's start at the edge of that first
  column;
- **a long value is shortened** with `…`, and the **full value shows when you hold the pointer over the
  cell**. Some cells are drawn more compactly than the file writes them: a list as names separated by
  commas, an empty list as a muted *(none)* (an empty cell stays empty — the two mean different things,
  [below](#an-empty-cell-and-an-empty-list)), the events on one line, and the two hashes by their first
  eight characters. The hover always shows the cell exactly as the file holds it, and the file itself
  is unchanged;
- each header shows the column's name exactly as the file writes it, never shortened; holding the
  pointer over it tells whose column it is — SWCraft's or the test team's — and what it means;
- you can widen or narrow a column by dragging the edge of its header. The tab forgets the widths
  when it closes; nothing is stored.

Nothing in the tab can be typed into. It shows what the last press generated: a press regenerates
and redraws it, and nothing else does — saving a model file does not, and neither does a
`swcraft generate test-tables` you run in a terminal, which the next press shows (it regenerates
anyway). The tab does not mark the tables as out of date when you save the model.

**With no tables yet**, the tab shows one line, *No test tables yet.*, and **Generate**. When the files
in `dist/test-tables/` cannot be read as SWCraft wrote them — one was edited, or is missing — it says
*The test tables cannot be read; generate them again.* with **Generate**, and never shows a part of a
table. **Generate** is the same press as the panel item.

**When the run is refused**, the tab shows *The test tables were not generated. The reason is in the
SWCraft output channel.* with **Generate**, and never the earlier tables beside it: the reason is the
command's `Error:` line in the output channel, the tree item says `failed`, and the command wrote
nothing ([below](#when-it-is-refused)).

The tab is a view of a generated file. It is no check of the model and no evidence of anything:
`swcraft validate` is the gate, and an accepted row is the test team's statement, not SWCraft's.

## On the command line { #on-the-command-line }

<!-- purpose: Running generate test-tables on the command line, with or without the test team's folder -->

```sh
swcraft generate test-tables --project swcraft.yaml
swcraft generate test-tables --project swcraft.yaml --annotations test-annotations
```

`swcraft generate test-tables` reads the model, writes the five files of `dist/test-tables/` (under
your manifest's `dist_root`) and writes nothing into your model. `--annotations` names the test team's
folder, which the command reads and never writes ([below](#the-round-trip-with-the-test-team));
without it, every row is *not reviewed*. The run prints where it wrote and one line per table:

```text
Generated test tables: <project>/dist/test-tables
components.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)
component-placement.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)
runnable-scheduling.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)
```

With `--annotations`, one more line counts the annotations whose row no longer exists:
`Orphaned annotations: 1`. Every run replaces the five files whole. `swcraft clean-outputs` removes
them with the rest of `dist/`, and **Build** neither writes nor needs them: run the command, or
press **Test Tables**, when you hand the tables over.

## The five files { #the-five-files }

<!-- purpose: What dist/test-tables/ holds: three tables, the orphaned annotations and manifest.json -->

| File | One row per |
|---|---|
| `components.csv` | component type |
| `component-placement.csv` | occurrence of a component in the root composition, and a type with none |
| `runnable-scheduling.csv` | runnable of each occurrence, and each runnable of a type with none |
| `orphaned-annotations.csv` | annotation of the test team whose row no longer exists |
| `manifest.json` | — it describes the other four |

`manifest.json` names the project, the SWCraft release, each table's row count and review-state
counts, and every column with whose it is (`swcraft` or `test`) and what it means, units included. It
also carries a digest of every table's facts: two deliveries with the same digest state the same facts,
whatever the test team wrote beside them.

The CSV files are UTF-8, comma-separated, with a header row and every field in double quotes; every
line ends with a single line feed on every platform. A list or a structured value is one cell of JSON
(`["Task_10ms"]`), a yes-or-no value is `true` or `false`, a period is in seconds (`0.01`). The layout
is SWCraft's own: no standard prescribes a table, a CSV file or an *accepted* column.

## What each table says { #what-each-table-says }

<!-- purpose: The columns of components.csv, component-placement.csv and runnable-scheduling.csv -->

Every table begins with `row_id`, the row's identity ([below](#a-rows-identity-and-what-a-rename-does)),
and ends with the same five columns: the test team's `verification_method`, `comment`, `accepted` and
`accepted_facts_hash`, then SWCraft's `review_state`. Before those, SWCraft's own columns:

- **`components.csv`** — `component`, `kind` (`application`, `service` or `ecu_abstraction`),
  `external`, `asil_declared` (the level the component declares), `asil_effective` (the declared one,
  else the highest level of the requirements allocated to it; `QM` when nothing is allocated),
  `memory` (memory name to bytes, as declared), `satisfies` (the requirement links as declared),
  `occurrences` (how many times the root composition uses the type), `runnables` (how many it
  declares), `description`, `source_ref`, `facts_hash`.
- **`component-placement.csv`** — `occurrence` (the path of member names from the root composition,
  joined by `/`; empty for a type with no occurrence), `component`, `placement` (`in-composition` or
  `not-in-composition`), `tasks`, `partitions` and `cores` (the distinct ones its runnables are mapped
  to, sorted), `mapping` (`all-mapped`, `partly-mapped`, `not-mapped` or `no-runnables`), `source_ref`,
  `facts_hash`. The type's memory and safety level are on `components.csv`: they belong to the type,
  not to one occurrence.
- **`runnable-scheduling.csv`** — `occurrence`, `component`, `runnable`, `symbol`, `mapping` (`mapped`
  or `not-mapped`), `task`, `task_position` (the runnable's place in the task's list, counted from 1),
  `task_period_s`, `task_priority`, `partition`, `core`, `events` (each event with its declared fields,
  periods as `period_s` in seconds), `min_start_interval_s`, `concurrent`, `wcet` (the budget as
  written, `400us`), `wcet_type`, `description`, `source_ref`, `facts_hash`. On a `not-mapped` row
  the columns from `task` to `core` are empty.

`source_ref` is the file that declares the component type, relative to the model directory.
`facts_hash` is a short fingerprint of the row's facts — every SWCraft column but `row_id`,
`source_ref`, `review_state` and `facts_hash` itself — so it changes when a fact changes, and not
when a file is renamed or moved. A changed description changes it too: what is verified can depend on it.

### An empty cell and an empty list { #an-empty-cell-and-an-empty-list }

<!-- purpose: What an empty cell, [] and {} mean in a test table -->

**An empty cell means "not declared in the model"** and nothing else. A list or a map is always
written: `[]` or `{}` when the model declares no member (`satisfies: []`, `memory: {}`). False is
`false`, zero is `0`, a safety level of QM is `QM`; what is not mapped is said by a status column —
`mapping` is `not-mapped` — never by an empty cell.

### What the tables do not say { #what-the-tables-do-not-say }

<!-- purpose: The limits of the test tables: no execution order, no measured time, no data flow -->

- **No execution order.** `task_position` is the order in which the task lists its runnables, not the
  order in which they run; the tables say nothing about dispatch, preemption or interrupts.
- **No measured time.** `wcet` is the architect's budget, not a measurement.
- **No sequences, no data flow, no interfaces, no memory access of a partition**: later tables may
  carry them; these three do not.
- **No proof.** The tables are a view of the model and prove nothing about the software. An `accepted`
  row is the test team's statement, never SWCraft's, and no table is verification evidence.

## The round trip with the test team { #the-round-trip-with-the-test-team }

<!-- purpose: Handing the tables over, taking the team's folder back and regenerating with their review kept -->

1. **Hand the tables over.** Run `swcraft generate test-tables --project swcraft.yaml` (or press
   **Test Tables** with *None yet*) and give the test team the folder `dist/test-tables/` — a copy of
   it; how it reaches them is yours to choose.
2. **The team fills its columns.** In each table they fill `verification_method` and `comment` as they
   like, and `accepted` with `yes` or `no` (or leave it empty). They may hand back the generated files
   they edited, or slim files of their own holding `row_id` and their columns only: SWCraft reads the
   test team's columns by name and ignores every other column, because the model is the truth for the
   facts. `manifest.json` handed back with the tables is expected and not read.
3. **Take their folder back** and keep it in the project outside the model, `dist/` and `views/` —
   `test-annotations/`, say — or anywhere outside the project.
4. **Generate again with their folder**:
   `swcraft generate test-tables --project swcraft.yaml --annotations test-annotations` (or press
   **Test Tables** and choose that folder once). SWCraft joins every annotation to the row
   with the same `row_id` and computes `review_state`.
5. **Read `review_state`** in each row, or the counts the run prints:
   - `not-reviewed` — no annotation, or `accepted` empty; a comment or a method alone accepts nothing;
   - `accepted` or `rejected` — `accepted` is `yes` or `no`, and the row's facts are the ones the team
     looked at;
   - `stale` — the team accepted or rejected the row, and its facts have changed since. A `stale` row
     keeps the team's `accepted` value as they wrote it: SWCraft flags it, it does not clear it.

**How SWCraft knows the facts changed.** The first generation after a verdict writes the facts it
refers to into `accepted_facts_hash`: the value the team put there, else the row's own `facts_hash`
in the file they handed back — the facts they were looking at. Every later generation compares that
with the row's current `facts_hash`. When no facts can be found for a verdict, `accepted_facts_hash`
is written as `none`, which matches nothing, so the row stays `stale` until somebody looks at it — a
`stale` file handed back unedited stays `stale`.

**To accept a `stale` row again**, the team looks at its new facts and clears its
`accepted_facts_hash` cell: the next generation takes the row's current `facts_hash` as the facts the
verdict refers to, and the row is `accepted` (or `rejected`) again. Changing `accepted` alone is not
enough.

**Nothing is lost on the way.** An annotation whose row no longer exists goes to
`orphaned-annotations.csv` with the table it was for, never dropped. That file is read back too when
the team hands the bundle back, so an orphan survives every later round, and is joined to its row
again if the row comes back. A row whose four test-team cells are all empty is no annotation: it is
not carried and never becomes an orphan, so a row nobody annotated can leave the model without
leaving anything behind. The team's folder is never written: after a run its files are byte for byte
what they were.

### A row's identity, and what a rename does { #a-rows-identity-and-what-a-rename-does }

<!-- purpose: Which changes keep a row and its review, and which make a new row and an orphan -->

A `row_id` holds names only — never a task, a period, a partition, a core, a file or a comment:

| Table | `row_id` |
|---|---|
| `components.csv` | `component:<Type>` |
| `component-placement.csv` | `occurrence:<path>`, or `unplaced:<Type>` for a type with no occurrence |
| `runnable-scheduling.csv` | `occurrence:<path>:<runnable>`, or `unplaced:<Type>:<runnable>` |

So moving a runnable to another task or changing a period **keeps the row** and the team's
annotation, and a verdict on it turns `stale`. Moving or renaming the file that declares a component
keeps the row and its verdict as they were: only `source_ref` changes, and `source_ref` is not part
of `facts_hash`, so nothing turns `stale`. The model has no identity that survives a rename, so:

- **renaming a component type** gives it a new `component:` row and new `unplaced:` rows (the old ones'
  annotations become orphans); its placed rows keep their ids, because an occurrence is named by its
  path, and turn `stale`, since their `component` changed;
- **renaming a composition member or a runnable**, moving an occurrence to another path, or placing a
  type that had no occurrence gives the affected rows new ids: the old rows' annotations become orphans,
  and the new rows start *not reviewed*.

A spreadsheet tool that changes a `row_id` cell as it saves — reformatting it, say — breaks the join
for that row in the same way: the annotation becomes an orphan, which you see, never a silent loss.

## When it is refused { #when-it-is-refused }

<!-- purpose: Why generate test-tables refuses, and that a refused run writes nothing -->

The command exits 1 with one `Error:` line, and writes nothing, when:

- it cannot read the project, or the project's `toolchain_version` names another release;
- `swcraft check-model` reports an error — the line counts them; run `swcraft check-model` to read
  them. Warnings refuse nothing. A table built over a model the check refuses could publish a guess as
  a fact: a runnable in two tasks, say;
- `--annotations` is given empty; the annotations folder does not exist, lies inside the model
  directory, `dist/` or `views/` (the last two are emptied by `swcraft clean-outputs`), or overlaps
  `dist/test-tables/` — compared without regard to case, so `Dist/review` is refused on every
  computer, as it would be inside `dist/` on one whose disk ignores case;
- a file of the team's folder cannot be used: not UTF-8, a table without a `row_id` column, a header
  naming one column twice, a row with no `row_id` but other cells filled, one `row_id` twice in a file
  or in a table and the orphan file, or an `accepted` value other than `yes`, `no` or empty (case and
  spaces aside: `Yes` reads as `yes`, `TRUE` is refused). The line names the file as you gave it, and
  the line and column.

A file in the team's folder that names no table is listed in one `Warning:` line and ignored; the run
goes on. Spreadsheet habits are read as they are meant: a byte order mark at the start, Windows line
ends, spaces around `row_id`, `accepted`, `accepted_facts_hash` and `facts_hash`, and empty rows at
the end.

When the files cannot be written, the run says so: a failure before the first file is replaced
replaces nothing; on Windows, a file another program holds open can stop the run between two files,
and then the line says the test tables were *only partly replaced* — run the command again. In VS Code
every one of these shows as the refusal page of the tab, with the line in the output channel.
