<!-- draft: not yet reviewed by the product's author -->

# Add a component

<!-- purpose: A new software component with a port and a runnable, placed in a composition and a task -->

<!-- keywords: new component, create component, new swc, software component, add swc, runnable, schedule, place in task, composition member, memory budget -->

A component on its own is a file. To take part in the architecture it also needs a place in a
composition, a task that runs its runnable, a budget on each memory the project checks, and a
connector to whoever feeds it. This page adds all of them in one change.

## Purpose { #purpose }

<!-- purpose: What this change adds to the model, and when you need it -->

Use it when a new piece of software joins the architecture. To add only a port to a component
that exists, [Connect two components](connect-components.md) is the shorter page.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model before you add a component -->

- The interface its port speaks exists, and so does the task that will run its runnable.
- You know the component's memory budget for every memory the project checks strictly: such a
  memory refuses a component that declares no budget for it.
- You know whether the component can be stopped and restarted. When you do not,
  `handle_termination_and_restart: no_support` is the value to start from.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The new component | `ExampleLoggerSWC` (*example*) | a name you choose; `find` answers `"none"` for a free one |
| Its group | `example` | the directory under `model/swc/` it goes into; a new one is fine |
| The interface of its port | `IExample` | `find`, or the files under `model/interfaces/` |
| The composition | `Root` | `read` with no address lists the compositions |
| The task | `Task_10ms` | the same `read`, or the files under `model/tasks/` |
| The memory | `RAM0` | `resource_report` with no argument lists the memories and their budgets |
| The component that feeds it | `ExampleSWC`, port `pOut` | `find` with both names |

## Steps { #steps }

<!-- purpose: The three steps, by hand in the editor and through the assistant -->

### 1. Read the model map { #read-the-model-map }

<!-- purpose: Seeing which compositions, tasks and memories exist, and taking the baseline -->

*In VS Code:* look under `model/compositions/`, `model/tasks/` and `model/hardware/`.

*Through the assistant:* one `read` with no address answers the model map — what each layer
holds — and the `baseline` token the change needs.

```json
{"tool": "read", "arguments": {}}
```

### 2. Add the component and place it { #add-the-component-and-place-it }

<!-- purpose: The one change that creates the component and places it in a composition and a task -->

*In VS Code:* write `model/swc/example/ExampleLoggerSWC.yaml`, then add a member to
`model/compositions/Root.yaml`, an entry to `runnables:` in `model/tasks/Task_10ms.yaml` and a
connector file under `model/connectors/`.

```yaml
name: ExampleLoggerSWC
type: application
ports:
  - name: rIn
    direction: require
    interface: IExample
runnables:
  - name: LogRun
    events:
      - type: timing
        period: 0.01
    reads:
      - port: rIn
        element: value
memory:
  RAM0: 512
handle_termination_and_restart: no_support
```

*Through the assistant:* one `change_set` with four operations: the component, its composition
member, the connector that feeds it and its task entry. The `baseline` is an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Add a logger component that reads IExample every 10 ms.",
  "complete": true,
  "operations": [
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleLoggerSWC"}, "value": {"name": "ExampleLoggerSWC", "group": "example", "type": "application", "ports": [{"name": "rIn", "direction": "require", "interface": "IExample"}], "runnables": [{"name": "LogRun", "events": [{"type": "timing", "period": 0.01}], "reads": [{"port": "rIn", "element": "value"}]}], "memory": {"RAM0": 512}, "handle_termination_and_restart": "no_support"}},
    {"operation": "create", "address": {"layer": "compositions", "element": "Root", "path": [{"field": "members"}, {"member": "ExampleLoggerSWC"}]}, "value": {"name": "ExampleLoggerSWC", "type": "ExampleLoggerSWC"}},
    {"operation": "create", "address": {"layer": "connectors", "element": "Example_to_Logger"}, "value": {"name": "Example_to_Logger", "from": {"swc": "ExampleSWC", "port": "pOut"}, "to": {"swc": "ExampleLoggerSWC", "port": "rIn"}}},
    {"operation": "create", "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "runnables"}, {"member": {"swc": "ExampleLoggerSWC", "instance": null, "runnable": "LogRun"}}]}, "value": {"swc": "ExampleLoggerSWC", "runnable": "LogRun"}}
  ]}}
```

A requirement the component realises travels in the same change: a `satisfies` list in the
component's value, such as `"satisfies": ["REQ_EXAMPLE_001@1"]`.

### 3. Review and approve { #review-and-approve }

<!-- purpose: Approving the proposed change in VS Code's review tab -->

*In VS Code:* nothing to approve when you edited the files yourself; go on to the check.

*Through the assistant:* VS Code opens the review tab with the four additions. **Approve**
applies exactly them; **Reject** leaves the model as it was.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the change, and what the answer says -->

`ExampleLoggerSWC` exists in its group, stands in `Root`, receives `IExample` from `ExampleSWC`
and runs `LogRun` in `Task_10ms`. An approved change answers `"status": "committed"` with a new
`baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the component is placed and the budgets still hold -->

*On the command line:* the model check, `swcraft check-model`:

```sh
swcraft check-model --project swcraft.yaml
```

*In VS Code:* **SWCraft: Check Model**, and **SWCraft: Resource Report** for the memory and the
core the component now takes.

*Through the assistant:* read the component back in its summary form.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc", "element": "ExampleLoggerSWC"}, "form": "summary"}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals this change meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`schema-violation`](refusals.md#schema-violation) | a required value is missing — the component's `group`, say | add it and send the change again |
| [`consistency-violation`](refusals.md#consistency-violation) | a rule over the whole model fails — no budget for a strictly checked memory, or no `handle_termination_and_restart` | add what the message names |
| [`broken-reference`](refusals.md#broken-reference) | the interface, task, composition or feeding port does not resolve | correct the name the message points at |
| [`identity-conflict`](refusals.md#identity-conflict) | a component of that name already exists | choose another name, or change the existing one with an update |
| [`partial-change-set`](refusals.md#partial-change-set) | an operation addresses a part of the new component — a port, say — before the operation that creates it | create the component first, or put the part into its value |
