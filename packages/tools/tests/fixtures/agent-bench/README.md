# Agent task benchmark data

The data of SWCraft's agent task benchmark: a fixed set of architect tasks for the SWCraft Architect
agent, each with a result a machine can check, and the models they are written against. Every file
here is test data; nothing in the folder runs.

## What is here

| Path | What it is |
|---|---|
| `tasks/NN-*.json` | One task: `id`, `task` (its number), `group`, `kind`, `status`, `prompt`, `expect`, and for a question a `reference` (the full answer and the command that computed it) |
| `bench/` | The model the tasks are written against: a copy of the Vehicle Control showcase's `model/` and `trace-decisions.yaml`, with a manifest that pins no toolchain |
| `bench/imports/` | The component export task 10 imports, made by `swcraft generate arxml --component` |
| `expected/NN-*/` | A change task's expected state, hand-written: the files a right run leaves (laid over a copy of `bench/`) and `removed.txt`, the files it deletes. Partial trees, which the gates that judge every committed `model/` as a whole model leave out (`committed_trees_support.PARTIAL_TREE_BASES`) |
| `large/tasks/NN-*.json` | The twenty-four tasks on the large bench's elements (the `large-bench` specimen); each question's `reference` names the command that computed its answer |
| `large/expected/NN-*/` | A change task's expected state on the large bench, as `expected/` is for the showcase |
| `templates/NN-*.json` | The any-model set: one template per task but 02, its prompt with `{{name}}` placeholders, the name of its binding and its oracle's class, bound to whatever model it runs on |

| # | Group | Task |
|---|---|---|
| 01 | model change | A sender port, a receiver port and a connector between them |
| 02 | model change | A new component with a port and a runnable, into a composition and a task (verbatim) |
| 03 | model change | A new interface with a data element of a new application type, a port on it, the mapping |
| 04 | model change | Move a runnable from one task to another, inside its partition |
| 05 | model change | Remove a component with everything that refers to it |
| 06 | model change | Rename a data element of an interface |
| 07 | model change | Add a requirement and link it to a port |
| 08 | question | What does a change of an interface affect? |
| 09 | question | Is there enough RAM for a new component of a given size? |
| 10 | mixed | Import a component from an ARXML export and wire it into a composition |
| 11 | search and statistics | How many ports does a component have, and which? |
| 12 | search and statistics | Which components have a port of a given interface? |
| 13 | search and statistics | Which ports are unconnected? |
| 14 | search and statistics | How many components are ASIL B? |
| 15 | relations and containment | Which components are inside a partition? |
| 16 | relations and containment | Which tasks run on a core? |
| 17 | relations and containment | Which component takes the most of the RAM? |
| 18 | requirements and traceability | Which elements satisfy a requirement, with their layer? |
| 19 | requirements and traceability | Which requirements are satisfied by no element? |
| 20 | requirements and traceability | Which links need confirmation because the requirement has a newer version? |
| 21 | requirements and traceability | Find the requirements about a topic (free text) |
| 22 | requirements and traceability | Which elements could realise a requirement, with the reason? |
| 23 | requirements and traceability | Link a port to a requirement |
| 24 | requirements and traceability | A requirement moved to version 2: confirm every link to it |
| 25 | guide question | What does the refusal `stale-baseline` mean, and what now? |
| 26 | guide question | How do I upgrade my project to a new release? |
| 27 | guide question | How do I go back to the previous release? |
| 28 | guide question | What does `swcraft validate` check, and what not? |
| 29 | guide question | Where does `needs.json` come from, and what is `outputs.needs`? |
| 30 | guide question | What does a `toolchain_version` pin that does not match do? |
| 31 | guide question | How do I bring a supplier's component ARXML into the model? |
| 32 | guide question | What do the server's review modes mean? |
| 33 | guide question | How do I hand placement and scheduling to the test team and keep their review? |
| 34 | guide question | How do I connect Codex or Claude Code in VS Code? |
| 35 | model change | Make one read of a runnable implicit, its other read left as it is |
| 36 | model change | Make a call asynchronous and start a new runnable when it returns |
| 37 | guide question | How do I connect Kiro to my SWCraft project? |

## The task format

`scripts/agent_task_check.py` reads a task's `expect` as follows; the code names in parentheses are
that script's.

A `change` task passes when every assertion holds over the model the run left and `check-model`
reports no error, and the run moved nothing the task does not ask for (`unrequested_changes`). A
`question` task passes when the reply meets `expect.answer` and the model is unchanged.
`calls.forbidden` lists the tools the run may not call (`WRITING_TOOLS`).

### Assertions

An assertion names a layer and an identity, never a file; it is one of:

- `{"layer", "name", "has"}` — the element *contains* `has`: every key of a mapping is present and
  contains its value, every list item is contained by some item, a scalar is equal. Without `name`
  some element of the layer must match.
- `{"layer", "name", "lacks"}` — the element exists and does not contain `lacks`.
- `{"layer", "name", "absent": true}` — no element of the layer carries that identity.
- `{"unreferenced": NAME}` — no value in the model is NAME or has it as a segment of an instance
  path (`Chassis/RearBrakeSensor`).
- `{"any_of": [...]}` — at least one of the assertions holds.
- `{"layer", "name", "may_change": [fields]}` — an allowance that always holds: those fields may
  move. It names at least one field, carries no content check (else exit 2 in the rendered form) and
  never admits a new element of that name.
- `{"layer", "name", "refers": {FIELD: {"layer", "has"?, "optional"?, "refers"?}}}` — each FIELD
  names an element of that layer which contains `has` (any element, without one); `"optional": true`
  lets FIELD be absent; a nested `refers` is checked on the element FIELD names. It admits FIELD on
  the element and, as new, the elements FIELD (and nested targets) name in the run's model, never a
  change to a baseline element.

### Answer checks

- `mentions_all` — each name appears as a whole word; `mentions_none` — none does.
- `number` — the reply states it as a whole number, from 1000 on also grouped (`states_number`), or
  spelled out up to twelve.
- `first_of` — of `candidates`, the one named first is `expected`; case-insensitive.
- `with_layer` — `[{"name", "layer", "owner"?}]`: each element is named and the line naming it, or
  the nearest heading above (a line starting with `#`, ending with `:`, or a whole bold line), holds
  one of `LAYER_WORDS` for its layer. A member site (`owner` given) is met by its owner with the
  member's layer word or `component`'s; the member's name is not required.
- `candidates` — `{"top": [...], "terms": {name: [...]}}`: every top candidate of the trace search is
  named, in any order, with at least one of its shared terms.
- `with_owner` — `[{"owner", "name"}]`: each member is named with its own owner (`owner_credits`
  states the reading). The known owners are the expected ones and every component and composition of
  the baseline; a member given to a known owner without it fails on its own. Some groupings are
  misread: owners listed before one member (`Swc005, Swc006: P1`) give it to the last only.

`"long_list": true` opts `mentions_all` (or `with_owner`, over pairs) into the long-list rule: above
twenty, a reply that does not name every one still passes if it states the number and names the
first twenty in the expected order, and joins no two expected names into a range or an elision
(`pOut000–rIn549`, `pOut000, …, rIn549`). A member given to an owner without it fails either way.

An explicit range of identifiers names every identifier in it: before `mentions_all` and
`mentions_none`, `expand_id_ranges` replaces `<PREFIX>NNN <joiner> <PREFIX>MMM` by the identifiers
it spans. Only a range read one way is expanded: one prefix ending in a hyphen (`SWR-DIA-`), the
same digit count, an ascending pair, at most `ID_RANGE_LIMIT` identifiers; the joiner `to` or
`through` (spaced), an en dash, or a spaced hyphen. Any other form is left as written, and whether
the model holds every identifier inside is not checked.

### Guide questions

A `guide` task asks what the user guide writes; a build without the `guide` tool fails criteria 1
and 2. `expect.guide` holds `sections`, `must` and `must_not` (each entry an `id`, `any_of` exact
strings and `case_sensitive`). The run gets two verdicts: **correct**, the pass verdict, on criteria
1 and 3 (`CORRECT_CRITERIA`), and **form**, criterion 2, reported beside it and never folded in.
Each is PASS, FAIL or N/A (`guide_criteria`):

1. **read** (`guide.read`): an answered `guide` call (its reply no refusal) named one of `sections`
   as `section`, any `part`. A search hit or toc entry is no read; a page id counts only when listed
   itself or, with `page_parts`, when the call asked for a listed part (no `part` reads part 1).
   Without `--guide-calls` this criterion is N/A.
2. **form** (`guide.form`): the reply holds the link `SECTION_LINK_PREFIX` + an expected id, `#`
   written or as `%23`, followed by no character that continues an id (`cites_section`).
3. **content** (`answer.must`, `answer.must_not`): each `must` entry's string is held, or every entry
   of one `one_of` set; no `must_not` string is (`content_text` states the reading; case aside unless
   the entry is `case_sensitive`). The `must` strings are identifiers or the guide's own words, never
   paraphrased facts; `must_not` is a narrow list of known-wrong statements.

Not reached: whether a reply is well argued or right beyond the checked strings (a wrong statement
worded otherwise passes; a `must_not` string inside a denial fails), and how far a field the task
may move was moved.

## The any-model templates

`templates/NN-*.json` is bound to one model by `scripts/agent_task_bind.py`. A template file:

```json
{
  "id": "11-ports-of-a-component", "task": 11, "group": "search-and-statistics",
  "kind": "question", "binding": "ports_of_the_largest_component", "oracle": "structural",
  "prompt": "How many ports does {{component}} have, and which?", "note": "..."
}
```

`id`, `task`, `group` and `kind` are the committed task's; `binding` names a function of the
binder's `BINDINGS`; `oracle` is `structural` (the answer follows from the loaded state alone) or
`product-rule` (a definition the product owns, called in-process). The prompt's `{{name}}`
placeholders are filled by `scripts/agent_placeholders.py`. Ties are broken by name, ascending;
*port order* is a component's file order; *row order* is the order a layer read answers
(`query.ordered_elements`), not name order on a model with more than one group.

## Who reads it

- `scripts/agent_task_check.py`, the checker, which decides whether one run counts;
  `scripts/agent_task_bind.py`, the binder, which binds the any-model set to one model; and
  `scripts/agent_task_bench.py`, which builds the large bench.
- The product tests that use the tasks and the bench as data: under `packages/tools/tests/`
  (through the `AGENT_BENCH` path of its `conftest.py`), `packages/mcp/tests/` and `scripts/tests/`.
- The benchmark runner, which gives an agent each task on a fresh copy of the bench and records what
  the run took.
