<!-- draft: not yet reviewed by the product's author -->

# Remove an element

<!-- purpose: Removing a component with everything that refers to it, or any other element once nothing does -->

<!-- keywords: remove, delete, drop, get rid of, take out, unused, unused element, delete component, remove swc, dependents, referrers, what refers to it, everything that points to it, software component, unlink, clean up -->

Removing an element is easy; what takes care is everything that names it. A component is named by
its connectors, its composition members, its task entries and its sequence steps. One change
removes it with all of them; any other element goes only once nothing names it any more.

## Purpose { #purpose }

<!-- purpose: What this change removes, and when you need it -->

Use it when a component leaves the architecture, or when an interface, a task or another element
is no longer needed. To give an element a new name instead, see
[Rename an element](rename-an-element.md).

## Before you start { #before-you-start }

<!-- purpose: What to know about the element and its referrers before you remove it -->

- You know what refers to the element: **SWCraft: Impact of This Element**, `swcraft impact` or
  the assistant's `impact_of` lists it.
- Deleting a memory or a core with a budget removes its check: VS Code asks about that separately.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The component to remove | `ExampleConsumerSWC` | `find` with the name as you know it |
| Another element to remove | `IDiagnosticService` | `find`, or the element's file under `model/` |

## Steps { #steps }

<!-- purpose: The two steps, by hand in the editor and through the assistant -->

### 1. See what refers to it { #see-what-refers-to-it }

<!-- purpose: Listing the element's referrers before anything is removed -->

*On the command line:*

```sh
swcraft impact --project swcraft.yaml ExampleConsumerSWC
```

It lists the task entry in `Task_10ms`, the connector `Example_to_Consumer`, the member of `Root`
and the step of `task10ms_consumer_cycle`.

*Through the assistant:* `impact_of` answers the same list. For a component the next step does
not need it: the server finds the referrers itself.

```json
{"tool": "impact_of", "arguments": {"name": "ExampleConsumerSWC"}}
```

### 2. Remove it { #remove-it }

<!-- purpose: The one change that removes a component and every referrer, or another element -->

*In VS Code:* delete the component's file, the connector file, the member in `Root.yaml`, the
entry in `Task_10ms.yaml` and the step in the sequence — or the sequence's file, when that was its
only step.

*Through the assistant:* a component goes in one `remove` with `"with_dependents": true`. The
server adds the removal of every referrer, each row marked `"origin": "server"`. The `baseline` is
an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Remove ExampleConsumerSWC and everything that refers to it.",
  "complete": true,
  "operations": [
    {"operation": "remove", "address": {"layer": "swc", "element": "ExampleConsumerSWC"}, "with_dependents": true}
  ]}}
```

Any other element is a plain `remove`; its referrers, when it has any, are removed or repointed
by their own operations in the same change. `IDiagnosticService` has none:

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Remove the unused interface IDiagnosticService.",
  "operations": [
    {"operation": "remove", "address": {"layer": "interfaces", "element": "IDiagnosticService"}}
  ]}}
```

VS Code opens the review tab with every row, the server's included; **Approve** applies them all.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the change, and what the answer says -->

`ExampleConsumerSWC`, `Example_to_Consumer` and the sequence `task10ms_consumer_cycle` are gone,
and `Root` and `Task_10ms` no longer name the component. `ExampleSWC` and its port stay. An
approved change answers `"status": "committed"` with a new `baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming nothing still names the element and the model passes its checks -->

*On the command line:* `swcraft check-model --project swcraft.yaml`.

*Through the assistant:* `find` answers `"none"` for a name the model no longer holds.

```json
{"tool": "find", "arguments": {"names": ["ExampleConsumerSWC", "Example_to_Consumer"]}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals this change meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | something still names the element — a component removed without `with_dependents`, an interface a port still speaks — or the name does not resolve | add `with_dependents` for a component; remove or repoint the referrers the message names |
| [`schema-violation`](refusals.md#schema-violation) | `with_dependents` on anything but a whole component | drop it, and remove the referrers by their own operations |
| [`stale-baseline`](refusals.md#stale-baseline) | the model changed after the `baseline` was read | read a fresh `baseline` and send the change again |
