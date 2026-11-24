<!-- draft: not yet reviewed by the product's author -->

# Ask about components and ports

<!-- purpose: Who reads or writes a data element, unconnected ports, what a partition or a core runs -->

<!-- keywords: question, ask, which, how many, list, who reads, who writes, reader, writer, signal, data element, unconnected, open ports, not connected, dangling port, partition, core, runs on, scheduled, tasks on a core, components in a partition, query -->

These questions read the model and change nothing. Each is one request, or a count and then a
page: a large model has hundreds of unconnected ports, so the number comes first and the rows
twenty at a time. In the calls below, a name in angle brackets — `<COMPONENT>` — stands for the
name you ask about; send the name in its place.

## Purpose { #purpose }

<!-- purpose: What these questions answer, and when you need them -->

Use it to see who uses a signal before you change it, to find the ports nothing connects yet, and
to see what a partition or a core runs, or which components use an interface. A question about everything a change touches is
[What a change affects](what-a-change-affects.md).

## Before you start { #before-you-start }

<!-- purpose: What the answers rely on, and how to page through a long one -->

- An empty answer is an answer: nothing in the model matches. It is never a refusal.
- A page names its `total` and, when there are more rows, a `next`: send the same request again
  with `after` set to it. A joined read's `next` is a whole row, sent as it is.
- *On the command line*, any call on this page runs as `swcraft query --project swcraft.yaml
  --request request.json`, with the call's `arguments` saved as `request.json` and the names filled
  in. It answers with no page limit. To see every field of a row and not only its name — each
  runnable call's operation, timeout and the rest — write `"form": "full"` in the file, or run
  `swcraft query --project swcraft.yaml swc --members runnables.calls --form full`; `full` does
  not combine with `derived` or `join`.

### The read request { #the-read-request }

<!-- purpose: What each argument of the assistant's read call does, and the answers and refusals it gives -->

The assistant's `read` names one `address`, several `addresses`, or none. With none it answers the
model map, and for a layer the map counts without naming its elements, a filtered or paged `index`
read answers a question about that layer.

- **`form`** says how wide an answer is. `count` answers `{"response": "count", "count": 3}` and
  applies to a whole layer or a collection. In a `summary`, a reference that is not set is left out
  rather than sent as `null`: a port with no `interface` key names none, as an external component's
  port does; every other optional field, a port's `observed_interface` and `description` among them,
  is in `full` only. An `index` publishes each identity in the form the step that reaches it takes,
  so it is replayed verbatim: `"pSpeed"` becomes `{"member": "pSpeed"}`, a composite identity
  `{"swc": …, "runnable": …}` becomes `{"member": {…}}`, and a map key becomes `{"key": "RAM0"}`.
  Every answer carries the `form` it was answered in. A collection too large for one answer is
  refused in either form with hints: page it with `limit` and `after`, or filter it with `where`. A
  large layer is worth asking for in `full` only once its count is known: the summary first, then
  the element, then the field.
- **`where`** keeps what its clauses match. A field not carried, `null` and the empty string are no
  value for `exists`; `0`, `false` and `{}` are values. The answer echoes the `where` it answered, and
  a filtered read is the difference between an answer and a refusal on a large model, so the
  assistant filters rather than reading a layer and sifting it.
- **`limit` and `after`** walk a set in pages. `total` counts the whole filtered set, `returned` the
  page. `discover`'s `reads.hint_page_size` is the size a refusal offers; any size may be asked. The
  cursor is an identity, not an offset: the model is read again on every request, so an offset would
  skip an element when the model changes during a walk, while a cursor keeps the walk exact.
- **`members`** answers every member of one collection across the layer, each row naming its
  element: `members: "ports"` with a `where` on `interface` is every port on that interface, in one
  call. A map collection's row is `{"element": …, "key": "RAM0", "member": 2048}`, which is how that
  member is addressed. A dotted path's row carries the owner chain, `{"element": "SpeedSensorSWC",
  "through": ["Run10ms"], "member": {"port": "pSpeed", "element": "speed"}}`, in every form and in
  its cursor. The `index` form and the page cursor publish a row's identity, not the member's value (a
  map row its `key` alone), so a `next` names one member of one element and is sent back verbatim as
  `after`. The member read runs first: the filter then selects members, the page windows them, and
  `form` publishes each. The answer echoes `members`, since no row names its collection.
- **`derived`** adds values SWCraft computes: `connection_ends` at the connectors gives each
  connector's atomic provider and requester ends through the delegations, and `base_type` over
  `runnables.writes` or `runnables.reads` gives the implementation and base type each accessed data
  element resolves to. A set-valued name gives a row per value, and a row whose set is empty gives
  none. A row whose value cannot be stated keeps a `null` there and stays; identical rows are
  collapsed. For partition crossings, `crossing_ends` alone carries both ends.
- **`join`** keeps a row once per joined row whose `right` values equal its own `left` values, with
  that row nested under `as`; a row with no match is dropped, identical rows collapse, and a page's
  `next` is the whole row. A table that spans components, connectors and types is one call:
  `members: "runnables.writes"`, `derived: ["base_type"]`, a join on the connectors'
  `connection_ends` and then on the readers' `runnables.reads` answer one row per connection, data
  element, writing runnable and reading runnable.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names these questions need, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The interface, `<INTERFACE>` | `IExample` | the name as the model spells it, in the files under `model/interfaces/`; `find` only when you are not sure of the spelling |
| The data element, `<DATA_ELEMENT>` | `value` | the interface's `data_elements:` list |
| The component, `<COMPONENT>` | `ExampleSWC` | `find` with the name as you know it |
| The partition, `<PARTITION>` | `PARTITION0` | the files under `model/hardware/` with `kind: partition` |
| The core, `<CORE>` | `CORE0` | the files under `model/hardware/` with `kind: core` |

## Steps { #steps }

<!-- purpose: Six questions, each by hand and through the assistant -->

### 1. Who reads or writes a data element { #who-reads-or-writes-a-data-element }

<!-- purpose: The runnables that read or write one data element of one interface, with their ports -->

<!-- recipe: Who reads or writes a data element -->

*On the command line:* `swcraft query` over `swc` with `--members runnables.reads` and
`--where element=value` — every read of a data element of that name, on any interface:

```sh
swcraft query --project swcraft.yaml swc --members runnables.reads --where element=value
```

*Through the assistant:* the readers of `<DATA_ELEMENT>` of `<INTERFACE>` — every read of a data
element of that name, joined to the reading component's port on that interface, so a data element
of the same name on another interface is not counted.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "runnables.reads", "where": [{"field": "element", "equals": "<DATA_ELEMENT>"}], "join": [{"with": {"address": {"layer": "swc"}, "members": "ports", "where": [{"field": "interface", "equals": "<INTERFACE>"}]}, "on": [{"left": "element", "right": "element"}, {"left": "member.port", "right": "member"}], "as": "port"}], "limit": 20}}
```

The writers are the same request over `runnables.writes`:

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "runnables.writes", "where": [{"field": "element", "equals": "<DATA_ELEMENT>"}], "join": [{"with": {"address": {"layer": "swc"}, "members": "ports", "where": [{"field": "interface", "equals": "<INTERFACE>"}]}, "on": [{"left": "element", "right": "element"}, {"left": "member.port", "right": "member"}], "as": "port"}], "limit": 20}}
```

Each row names the component (`element`), the runnable (`through`) and the port (`member.port`);
each answer is a page of twenty rows.

### 2. Ports that nothing connects { #ports-that-nothing-connects }

<!-- purpose: How many ports no connector or delegation reaches, then the first twenty -->

<!-- recipe: Ports that nothing connects -->

*On the command line:* `swcraft query` with `--members ports` — every port, connected or not, one
line each:

```sh
swcraft query --project swcraft.yaml swc --members ports
```

*Through the assistant:* every port that no connector end and no composition delegation reaches,
with its direction and interface; a connected port has no row, so the answer is exactly the
unconnected set. First how many, then the first twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "count", "members": "ports", "derived": ["unconnected"]}}
```

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "ports", "derived": ["unconnected"], "limit": 20}}
```

### 3. A component's unconnected ports { #a-components-unconnected-ports }

<!-- purpose: The ports of one component that nothing connects -->

<!-- recipe: A component's unconnected ports -->

*In VS Code:* the component's `ports:` list, against the connectors that name it.

*Through the assistant:* the ports of `<COMPONENT>` that nothing connects — the same member read
joined on `element` to that component's one row, so only its own ports answer. First how many, then
the first twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "count", "members": "ports", "derived": ["unconnected"], "join": [{"with": {"address": {"layer": "swc"}, "where": [{"field": "name", "equals": "<COMPONENT>"}]}, "on": [{"left": "element", "right": "element"}], "as": "owner"}]}}
```

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "ports", "derived": ["unconnected"], "join": [{"with": {"address": {"layer": "swc"}, "where": [{"field": "name", "equals": "<COMPONENT>"}]}, "on": [{"left": "element", "right": "element"}], "as": "owner"}], "limit": 20}}
```

### 4. Components a partition runs { #components-a-partition-runs }

<!-- purpose: Every component whose runnables a task of one partition runs -->

<!-- recipe: Components a partition runs -->

*On the command line:* `swcraft query` over `tasks` with `--where partition=PARTITION0` — the tasks
of the partition; their `runnables:` name the components:

```sh
swcraft query --project swcraft.yaml tasks --where partition=PARTITION0
```

*Through the assistant:* every component a task of `<PARTITION>` runs — one row per component and
partition, kept where `derived.partitions.partition` is the one asked about, a page of twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "derived": ["partitions"], "where": [{"field": "derived.partitions.partition", "equals": "<PARTITION>"}], "limit": 20}}
```

### 5. Tasks that run on a core { #tasks-that-run-on-a-core }

<!-- purpose: Every task whose partition is on one core -->

<!-- recipe: Tasks that run on a core -->

*In VS Code:* the partitions under `model/hardware/` name their `core:`, and each task its
`partition:`.

*Through the assistant:* every task whose partition is on `<CORE>` (`derived.core.core`), each
naming its partition. A
task whose partition names no core carries `core: null` and is kept by no core. The answer is a
page of twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "tasks"}, "form": "index", "derived": ["core"], "where": [{"field": "derived.core.core", "equals": "<CORE>"}], "limit": 20}}
```

### 6. Components with a port on an interface { #components-with-a-port-on-an-interface }

<!-- purpose: Every port on one interface, each naming its component: which components provide or require it -->

<!-- recipe: Components with a port on an interface -->

*On the command line:* `swcraft query` with `--members ports` and `--where interface=IExample` —
every port on that interface, one line each:

```sh
swcraft query --project swcraft.yaml swc --members ports --where interface=IExample
```

*Through the assistant:* every port of every component on `<INTERFACE>` — one read at the layer, each
row naming the component (`element`) and the port (`member`), a page of twenty. A component with two
ports on the interface answers two rows. The question needs no `find`: the `where` names the interface
as the model spells it.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "ports", "where": [{"field": "interface", "equals": "<INTERFACE>"}], "limit": 20}}
```

## What you get { #what-you-get }

<!-- purpose: What the six answers say for the example -->

In `examples/basic`: no runnable declares a read or a write, so both pages of step 1 are empty; the
two ports `pOut` and `rIn` are connected by `Example_to_Consumer`, so steps 2 and 3 count 0;
`PARTITION0` runs `ExampleConsumerSWC`, whose runnable `Task_10ms` schedules; and `CORE0` runs
`Task_10ms`, in `PARTITION0`; `IExample` answers two ports, `ExampleConsumerSWC`'s `rIn` and
`ExampleSWC`'s `pOut`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming an answer against the model's own files -->

*In VS Code:* open a row's element — the component, the task, the partition — and read the field the
answer is built from: `ports:`, `runnables:`, `partition:`, `core:`.

*Through the assistant:* `read` the element a row named, as it stands.

```json
{"tool": "read", "arguments": {"address": {"layer": "tasks", "element": "Task_10ms"}, "form": "summary"}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals these questions meet, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`unknown-field`](refusals.md#unknown-field) | a `where` field, a `members` collection or a `derived` name the model does not declare — a misspelling | use a name the message lists |
| [`broken-reference`](refusals.md#broken-reference) | a `read` of one element names one the model does not hold | use the name as a row spelled it; the message names the nearest |
| [`over-budget`](refusals.md#over-budget) | a list sent without its `limit` is too large for one answer | send it with `"limit": 20`, or the first hint the refusal names |
