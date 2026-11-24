<!-- draft: not yet reviewed by the product's author -->

# What a change affects

<!-- purpose: Counting, then listing, everything that refers to an element before you change it -->

<!-- keywords: impact, impact analysis, affect, affected, affects, dependents, dependencies, what breaks, who uses it, referrers, ripple effect, consequences, change an interface, before a change, before editing, depends on, touched -->

Before you change an element, ask what names it. The answer is every element that declares a
reference to it, directly or through the elements that do: change an interface, and its ports, the
components that own them, their connectors, their composition members and their task entries are
all in the answer. The question changes nothing. In the calls below, `<ELEMENT>` stands for your
element's name; send the name in its place.

## Purpose { #purpose }

<!-- purpose: What this question answers, and when you need it -->

Use it before you change or remove an interface, a data type, a component, a requirement, a task
or a hardware element: the count tells you how large the change is, the list where it lands. To
remove an element together with everything that names it, see
[Remove an element](remove-an-element.md).

## Before you start { #before-you-start }

<!-- purpose: What the answer covers before you ask it -->

- The answer is about the files as they are saved now, not a committed revision.
- The element exists under the name you give. A name no layer declares is not refused: the answer
  says no element of that name is in any layer.

## Names to resolve { #names-to-resolve }

<!-- purpose: The name this question needs, with an example and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The element, `<ELEMENT>` | `IExample` | `find` with the name as you know it, or the element's file under `model/` |

## Steps { #steps }

<!-- purpose: The two steps, by hand in the editor and through the assistant -->

### 1. Count, then list, what it affects { #count-then-list-what-it-affects }

<!-- purpose: How many elements refer to it per kind, then the same walk listed by layer -->

<!-- recipe: What a change affects -->

*In VS Code:* open the element's file and run **SWCraft: Impact of This Element**; the **Impact**
view below the panel draws the answer.

*On the command line:* `swcraft impact` and the element's name:

```sh
swcraft impact --project swcraft.yaml IExample
```

*Through the assistant:* `impact_of` walks everything that declares a reference to `<ELEMENT>`,
directly or through the elements that do: first how many, per kind, then — when you ask which —
the same walk listed by layer.

```json
{"tool": "impact_of", "arguments": {"name": "<ELEMENT>", "form": "count", "direction": "dependents", "depth": null}}
```

```json
{"tool": "impact_of", "arguments": {"name": "<ELEMENT>", "form": "summary", "direction": "dependents", "depth": null}}
```

The count answers on any model. A summary of more than twenty dependents answers its first twenty
with the whole walk's count per kind and `next`; send that `after` for the rest. A summary whose
page is still too large for one answer is refused with the narrower request that fits. A positive
`depth` bounds the walk to that many hops; `"direction": "dependencies"` answers what `<ELEMENT>`
needs instead.

The count is about two hundred bytes on any model: `{"response": "impact", "name", "direction",
"depth", "matched"}`, `matched` naming per layer the dependents' `total` and their count `by_kind`,
`{}` when no layer declares the name. Its answer is small; its work is the whole walk, bounded by
`depth` alone. The `full` form is the text `swcraft impact` prints: one line per dependent naming
every edge that reached it — `via ports pA, pB, pC`, and for a connector each end whose owner the
walk reached, owner first, `via ports SwcA.pOut, SwcB.rIn`; an equal value from two parents is two
edges, qualified by parent, `runnables SwcA.Run, SwcB.Run`. With a `depth`, an element at the bound
is not walked further, so an edge out of it is not read; a connector reached only from its own
composition names no port. A row's depth is the level this walk reached it at, not the shortest
path. With `"direction": "dependencies"` the detail still names the edge, not the end.

A page's footer names `page: <returned> of <total>`, the whole walk's count per kind (`in all: swc
30, connector 1457`) and `next`: read the totals there, never from the rows of one page. The cursor
is a row, not an offset: the model is read again on every call, and a cursor whose row was edited
away is refused, so start again without it. When the name matches nothing, `limit` and `after` are
answered by the "not found" line rather than refused. A refusal for size names the wire bytes it
measured, the rows they cover, the budget and the narrower request.

### 2. Read one of them { #read-one-of-them }

<!-- purpose: Reading an affected element before deciding what the change does to it -->

*In VS Code:* open the element's file, `model/connectors/Example_to_Consumer.yaml` here.

*Through the assistant:* `read` one element the walk named, by the layer and the name it gave;
the `full` form answers every field, here the two ends the connector joins.

```json
{"tool": "read", "arguments": {"address": {"layer": "connectors", "element": "Example_to_Consumer"}, "form": "full"}}
```

## What you get { #what-you-get }

<!-- purpose: What the count and the list say for the example -->

In `examples/basic`, `IExample` affects six elements: the two components whose ports speak it,
`ExampleSWC` and `ExampleConsumerSWC`, and through them the connector `Example_to_Consumer`, the
composition `Root`, the task `Task_10ms` and the sequence `task10ms_consumer_cycle`. The count
answers those six per kind — `swc` 2 and one of each other kind — and the summary lists them by
layer.

## How to check it { #how-to-check-it }

<!-- purpose: Telling the direct references from the ones reached through others -->

*On the command line:* the same walk, bounded to the elements that name `IExample` themselves —
`swcraft query` with `--dependents IExample` and `--depth 1`:

```sh
swcraft query --project swcraft.yaml --dependents IExample --depth 1
```

It lists the two components, each with the port that names the interface. Through the assistant,
the same bound is `"depth": 1`.

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals these calls meet, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | the `read` names an element the model does not hold | use the layer and the name as the walk spelled them; the message names the nearest |
| `impact_of`'s own refusal, a line starting `ERROR:` | an unknown `form` or `direction`, a `depth` below 1, a `count` sent with `limit` or `after`, or a summary too large for one answer | send the request the line names: a `depth` of 1, then the `count` |
