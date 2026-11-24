<!-- draft: not yet reviewed by the product's author -->

# Move a runnable to another task

<!-- purpose: Moving a runnable's task entry to another task of its partition, with the sequence that names it -->

<!-- keywords: move runnable, reschedule, remap, task mapping, another task, change task, scheduling, task entry, partition, lower priority, sequence step -->

A task runs a runnable because its `runnables:` list has an entry naming it. Moving the runnable
is taking that entry out of one task and putting it into the other, in one change, together with
whatever else names the runnable on the old task — a sequence of that task, say.

## Purpose { #purpose }

<!-- purpose: What this change moves, and when you need it -->

Use it when a runnable should run in another task of the same partition — a lower priority, a
task with room in its budget. The runnable itself, its component and its events do not change.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model before you move a runnable -->

- The task you move it to exists, in the same partition, with a period the runnable's own
  timing period is a whole multiple of.
- You know whether a sequence names the runnable: a sequence belongs to one task, and each of its
  steps must be a runnable that task runs.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The component | `ExampleConsumerSWC` | `find` with the name as you know it |
| The runnable | `ExampleConsumerRun` | the component's `runnables:` list, or `read` of the component |
| The task it runs in now | `Task_10ms` | `read` of the task, or the files under `model/tasks/` |
| The task it moves to | `Task_10ms_Low` (*example*) | the same; it must share the partition |
| A sequence naming it | `task10ms_consumer_cycle` | `impact_of` the component, or the files under `model/sequences/` |

## Steps { #steps }

<!-- purpose: The two steps, by hand in the editor and through the assistant -->

### 1. Read the two tasks { #read-the-two-tasks }

<!-- purpose: Seeing both tasks' entries, partitions and periods, and taking the baseline -->

*In VS Code:* open the two task files, and **SWCraft: Impact of This Element** on the component
for the sequences that name it.

*Through the assistant:* one `find` with the names. Its answer carries the `baseline`.

```json
{"tool": "find", "arguments": {"names": ["ExampleConsumerSWC", "Task_10ms", "Task_10ms_Low", "task10ms_consumer_cycle"]}}
```

### 2. Move the entry { #move-the-entry }

<!-- purpose: The one change that removes the entry, adds it to the other task and moves the sequence -->

*In VS Code:* cut the entry `{swc: ExampleConsumerSWC, runnable: ExampleConsumerRun}` from
`Task_10ms.yaml`, paste it under `runnables:` in `Task_10ms_Low.yaml`, and set the sequence's
`task:` to `Task_10ms_Low`. A sequence with other steps keeps its task and loses this step instead.

*Through the assistant:* one `change_set`. An entry is addressed by its component and runnable,
`instance` `null` when it names the component by `swc`. The `baseline` is an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Move ExampleConsumerRun from Task_10ms to Task_10ms_Low.",
  "complete": true,
  "operations": [
    {"operation": "remove", "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "runnables"}, {"member": {"swc": "ExampleConsumerSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}},
    {"operation": "create", "address": {"layer": "tasks", "element": "Task_10ms_Low", "path": [{"field": "runnables"}, {"member": {"swc": "ExampleConsumerSWC", "instance": null, "runnable": "ExampleConsumerRun"}}]}, "value": {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}},
    {"operation": "update", "address": {"layer": "sequences", "element": "task10ms_consumer_cycle", "path": [{"field": "task"}]}, "value": "Task_10ms_Low"}
  ]}}
```

VS Code opens the review tab; **Approve** applies the three rows, **Reject** nothing.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the change, and what the answer says -->

`Task_10ms_Low` runs `ExampleConsumerRun`, `Task_10ms` no longer does, and the sequence belongs to
`Task_10ms_Low`. An approved change answers `"status": "committed"` with a new `baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the runnable runs where you meant and the model still passes its checks -->

*On the command line:* `swcraft check-model --project swcraft.yaml`, and
`swcraft resource-report --project swcraft.yaml` for each core's load and each task's share of it.

*Through the assistant:* read the task back.

```json
{"tool": "read", "arguments": {"address": {"layer": "tasks", "element": "Task_10ms_Low"}}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals this change meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | a sequence step names the runnable on a task that no longer runs it, or the target task's name does not resolve | move the sequence or drop its step in the same change; correct the name |
| [`consistency-violation`](refusals.md#consistency-violation) | the runnable is in both tasks (the remove is missing), or its period is not a multiple of the new task's | add the remove; pick a task whose period fits |
| [`identity-conflict`](refusals.md#identity-conflict) | the target task already runs that runnable | nothing to move; read the task |
| [`stale-baseline`](refusals.md#stale-baseline) | the model changed after the `baseline` was read | read a fresh `baseline` and send the change again |
