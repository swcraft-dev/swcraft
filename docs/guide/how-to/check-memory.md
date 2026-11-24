<!-- draft: not yet reviewed by the product's author -->

# Check a memory

<!-- purpose: Whether a memory has room for a new component, and which component takes the most of it -->

<!-- keywords: memory, RAM, ROM, flash, room, space, enough room, fit, fits, budget, headroom, free bytes, kilobytes, KB, bytes left, largest consumer, takes the most, biggest user, memory usage, consumption, resources -->

A memory's budget is its `size` times its `threshold`, not its raw size: `RAM0` holds 262 144
bytes and its threshold of 0.8 leaves a budget of 209 715.2 bytes. Whether a new component fits is
decided against that budget, and the server answers it, so nobody subtracts by hand. In the calls
below, `<MEMORY>` stands for your memory's name; send the name in its place.

## Purpose { #purpose }

<!-- purpose: What these two questions answer, and when you need them -->

Use it before you add a component that needs memory — does the amount fit? — and when a memory runs
short — who takes the most of it? A core's room is the same question, by its share of the core.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model for the answer to be complete -->

- The memory has a `size` and a `threshold`, and the components charged to it declare their bytes
  under `memory:`. A component charged to a memory without a budget for it is counted apart, and
  a "yes" that it could falsify is answered as unknown.
- You know the amount in bytes: 12 KB is 12288 bytes.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this question needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The memory, `<MEMORY>` | `RAM0` | `resource_report` with `{"core": "CORE0"}` lists the memories a core reaches; or the files under `model/hardware/` with `kind: memory` |
| The amount | `40960` bytes (*example*) | what the new component needs, in bytes |

## Steps { #steps }

<!-- purpose: The two questions, by hand in the editor and through the assistant -->

### 1. Ask whether the amount fits { #ask-whether-the-amount-fits }

<!-- purpose: Whether a memory holds the bytes a new component needs, against its budget and its size -->

<!-- recipe: Whether a memory holds a new component -->

*In VS Code:* **Resource Report** in the panel fills the **Resources** view with each memory's
budget, its use and its headroom.

*On the command line:* `swcraft resource-report` with `--fit-bytes` and the number of bytes:

```sh
swcraft resource-report --project swcraft.yaml --fit-bytes 40960
```

*Through the assistant:* `resource_report` asks whether `<MEMORY>` has room for the bytes a new
component needs — here `40960`; send the number you need. The server answers twice:
`within_threshold` against the memory's budget (`size` times `threshold`) and `within_size` against
its raw `size`, each with its `limit` and its `headroom`. `fits` is `within_threshold`'s verdict, so
the answer leads with what `fits` says, then states `answer`, the server's sentence for the pair:
"fits physically, breaks the threshold by N bytes" is the amount that fits the size and not the
budget.

```json
{"tool": "resource_report", "arguments": {"fits": {"memory": "<MEMORY>", "bytes": 40960}}}
```

`fits` and `within_size.fits` are `null` only where their "yes" would rest on a component charged to
the memory that declared no budget for it: the answer is then unknown, and those components are
named. A "no" always stands, because a missing load only adds to the use. A core's room is the same
request as `{"fits": {"core": "<CORE>", "utilization": "0.05"}}`, against the core's threshold and
its 100 %.

### 2. Find the largest consumer { #find-the-largest-consumer }

<!-- purpose: The components charged to a memory, largest first -->

<!-- recipe: The largest consumer of a memory -->

*On the command line:* `swcraft resource-report --project swcraft.yaml` lists each memory's
consumers by name, with their bytes; compare the figures.

*Through the assistant:* `resource_report` answers the components charged to `<MEMORY>`, largest
first, in one answer.

```json
{"tool": "resource_report", "arguments": {"memory": "<MEMORY>"}}
```

The answer is the first row of `consumers`. A component that declared no budget for the memory is
counted in `undeclared` (the first ten are named in `undeclared_first`), not a row.

## What you get { #what-you-get }

<!-- purpose: What the two answers say for the example -->

In `examples/basic`, `RAM0` uses 3584 of its 209 715.2-byte budget, so 40960 bytes more fit:
`fits` is `true` and `answer` is *fits within the threshold and the size*. Its consumers are
`ExampleSWC` with 2048 bytes and `ExampleConsumerSWC` with 1536, so `ExampleSWC` takes the most.

### The other answers { #the-other-answers }

<!-- purpose: The overview and the core answer of resource_report, and how their figures read -->

With no argument, `resource_report` answers the overview: every core and memory, never a consumer,
as `{"response": "resources", "cores", "memories", "findings", "topology"}`. A core's row is
`{"name", "kind", "enforcement", "utilization", "limit", "headroom", "physical_headroom",
"status"}`; a memory's carries `size` (bytes) and `used` in place of `utilization`. A memory's `limit`
is its `size` times its `threshold`, a core's its `threshold`, and `headroom` is the `limit` minus the
use; `physical_headroom` is the raw room, the `size` minus `used`, or 100 % minus a core's
utilization. Whether an amount fits is decided by the report's own exact arithmetic, use plus the
amount against the `limit`. A figure is a string as the command line prints it (`70.00%`,
`27955.2`); a `~` before it means two decimals cannot state it exactly and it is truncated
downward, never rounded up. `status` is `unknown (...)` when a strictly checked budget has a
consumer that declared nothing.

The overview's `findings` are every budget finding, the `info` findings of an advisory resource
included, and a resource's missing budgets as one entry with their `count`; `validate` prints
issues only, so the `info` findings reach you nowhere else. `topology` names a task whose partition, or whose
partition's core, no hardware element declares: it consumes nothing. A model with a data-received
event adds `arrivals` — per event, where its data comes from: a bound from the senders' task
periods, `arrival rate unknown` naming the source outside the model, or `not derived`; derived,
never a measured rate and never a finding. A model with an implicit read or write adds `implicit`:
per task, the implicit reads and writes its runnables declare, then the runnables no task maps and
those not walked; counts only. Both are bounded: `first` holds the first lines, `more` counts the
rest, which the command line prints.

With `core`, the answer lists its tasks largest first, the tasks that declare no `wcet` in
`undeclared` (the first named in `undeclared_first`) and those whose `wcet` is `0` in
`declared_zero`, and the headline row of every memory whose `accessible` names the core.

## How to check it { #how-to-check-it }

<!-- purpose: Reading the memory's own figures the budget is computed from -->

*Through the assistant:* `read` the memory; its `size` times its `threshold` is the budget the
answer used.

```json
{"tool": "read", "arguments": {"address": {"layer": "hardware", "element": "RAM0"}, "form": "summary"}}
```

*In VS Code:* the memory's file under `model/hardware/` holds the same two figures.

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals these calls meet, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | the `read` names a memory the model does not hold | use the name the hardware files spell; the message names the nearest |
| `resource_report`'s own refusal, a line starting `ERROR:` | two questions in one call, a `fits` without its `bytes`, or a memory name no memory carries — the line names the declared ones | ask one question per call, with a name the line lists |
