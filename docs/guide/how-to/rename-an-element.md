<!-- draft: not yet reviewed by the product's author -->

# Rename an element

<!-- purpose: Renaming a component, a port or a data element: a removal and a creation in one change -->

<!-- keywords: rename, new name, change name, call it, retitle, rename component, rename port, rename data element, rename signal, give it a new name, change the name, references follow -->

A name is an element's identity, so it is never edited in place: a rename removes the element
under its old name and creates it under the new one, in one change, and every reference to the old
name moves with it. The model check sees the result whole, so nothing is left pointing nowhere.

## Purpose { #purpose }

<!-- purpose: What a rename changes, and when you need it -->

Use it when a component, a port, an interface, a data element or any other element needs a
better name. Rename in one change and make any other edit in the next: a pure rename keeps the
file's comments and layout.

## Before you start { #before-you-start }

<!-- purpose: What to know before you rename an element -->

- You know what refers to the old name: **SWCraft: Impact of This Element**, `swcraft impact` or
  the assistant's `impact_of` lists it.
- The new name is free, and is a letter followed by letters, digits or underscores.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The component to rename | `ExampleConsumerSWC` | `find` with the name as you know it |
| Its new name | `ExampleReceiverSWC` (*example*) | a name you choose; `find` answers `"none"` for a free one |
| A data element to rename | `value` of `IExample` | `read` of the interface |
| Its new name | `level` (*example*) | a name you choose |

## Steps { #steps }

<!-- purpose: The two steps, by hand in the editor and through the assistant -->

### 1. Read the element and its referrers { #read-the-element-and-its-referrers }

<!-- purpose: Reading the element whole, since the new one repeats it, and listing what names it -->

*On the command line:* `swcraft impact --project swcraft.yaml ExampleConsumerSWC`.

*Through the assistant:* `read` the element in full — the new element repeats every value — and
`impact_of` it. The `read` answer carries the `baseline`.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc", "element": "ExampleConsumerSWC"}, "form": "full"}}
```

### 2. Rename it with its references { #rename-it-with-its-references }

<!-- purpose: The one change that renames the element and moves every reference to the new name -->

*In VS Code:* change the `name:` line, rename the file to match, then change the name in every
file `impact` listed.

*Through the assistant:* one `change_set`: the new element with the old one's values, the
removal of the old one, an `update` of each reference field and, for an entry whose identity holds
the name — a task entry, a sequence step — a new entry and the old one's removal. Create before
you remove where a list must not be left empty. The `baseline` is an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Rename ExampleConsumerSWC to ExampleReceiverSWC.",
  "operations": [
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleReceiverSWC"}, "value": {"name": "ExampleReceiverSWC", "group": "example", "type": "application", "description": "Example software component consuming data from ExampleSWC", "ports": [{"name": "rIn", "direction": "require", "interface": "IExample", "description": "Example input port requiring data"}], "runnables": [{"name": "ExampleConsumerRun", "min_start_interval": 0.01, "events": [{"type": "timing", "period": 0.01}]}], "memory": {"RAM0": 1536}, "handle_termination_and_restart": "no_support", "satisfies": ["REQ_EXAMPLE_001@1"]}},
    {"operation": "remove", "address": {"layer": "swc", "element": "ExampleConsumerSWC"}},
    {"operation": "update", "address": {"layer": "compositions", "element": "Root", "path": [{"field": "members"}, {"member": "ExampleConsumerSWC"}, {"field": "type"}]}, "value": "ExampleReceiverSWC"},
    {"operation": "update", "address": {"layer": "connectors", "element": "Example_to_Consumer", "path": [{"field": "to"}, {"field": "swc"}]}, "value": "ExampleReceiverSWC"},
    {"operation": "create", "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "runnables"}, {"member": {"swc": "ExampleReceiverSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}, "value": {"swc": "ExampleReceiverSWC", "runnable": "ExampleConsumerRun"}},
    {"operation": "remove", "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "runnables"}, {"member": {"swc": "ExampleConsumerSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}},
    {"operation": "create", "address": {"layer": "sequences", "element": "task10ms_consumer_cycle", "path": [{"field": "steps"}, {"member": {"swc": "ExampleReceiverSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}, "value": {"swc": "ExampleReceiverSWC", "runnable": "ExampleConsumerRun"}},
    {"operation": "remove", "address": {"layer": "sequences", "element": "task10ms_consumer_cycle", "path": [{"field": "steps"}, {"member": {"swc": "ExampleConsumerSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}}
  ]}}
```

A data element, a port or a runnable is renamed the same way, inside its element. Each runnable
access naming the old data element — a `reads` or `writes` entry — follows the same way, a new
entry and the old one's removal; `IExample`'s `value` has none:

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Rename IExample's data element value to level.",
  "operations": [
    {"operation": "create", "address": {"layer": "interfaces", "element": "IExample", "path": [{"field": "data_elements"}, {"member": "level"}]}, "value": {"name": "level", "implementation_type": "float32"}},
    {"operation": "remove", "address": {"layer": "interfaces", "element": "IExample", "path": [{"field": "data_elements"}, {"member": "value"}]}}
  ]}}
```

VS Code's review tab shows each removal beside its addition; **Approve** applies them all.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the rename, and what the answer says -->

`ExampleReceiverSWC` stands where `ExampleConsumerSWC` stood: in `Root`, at the end of the
connector, in `Task_10ms` and in the sequence. An approved change answers `"status": "committed"`
with a new `baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the old name is gone everywhere and the model passes its checks -->

*On the command line:* `swcraft check-model --project swcraft.yaml`.

*Through the assistant:* `find` both names; the old one answers `"none"`.

```json
{"tool": "find", "arguments": {"names": ["ExampleConsumerSWC", "ExampleReceiverSWC"]}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals a rename meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`immutable-field`](refusals.md#immutable-field) | an `update` of `name`: a name is never edited in place | remove and create, as above |
| [`broken-reference`](refusals.md#broken-reference) | a reference still names the old name; the message names where | add an operation for it to the same change |
| [`schema-violation`](refusals.md#schema-violation) | a removal left a list empty that must not be — the last data element, the last step | put the create before the remove |
| [`identity-conflict`](refusals.md#identity-conflict) | the new name is taken | choose another name |
| [`invalid-identifier`](refusals.md#invalid-identifier) | the new name is not a letter followed by letters, digits or underscores | correct it |
