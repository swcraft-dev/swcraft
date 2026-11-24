<!-- draft: not yet reviewed by the product's author -->

# Connect two components

<!-- purpose: Adding a sender port, a receiver port and the connector between them, in one change -->

<!-- keywords: connect, connection, connector, wire, wiring, hook up, sender, receiver, provide port, require port, connect two ports, connector between ports, data flow -->

Two components exchange data through a pair of ports on the same interface — a `provide` port on
the sender, a `require` port on the receiver — and a connector from one to the other. This page
adds all three in one change.

## Purpose { #purpose }

<!-- purpose: What this change adds to the model, and when you need it -->

Use it when one component has to receive what another sends and neither has a port for it yet.
When both ports already exist, the last step alone — the connector — is the change.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model before you connect two components -->

- Both components exist, and the interface the ports speak exists.
- You know which composition the connector is drawn in: one that names no `composition` is
  drawn in the root composition, where both components of `examples/basic` stand.
- In VS Code, the SWCraft extension is installed and the project's folder is open, so a proposal
  from the assistant opens its review tab.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The sender component | `ExampleSWC` | `find` with the name as you know it, or the component's file under `model/swc/` |
| The receiver component | `ExampleConsumerSWC` | the same `find` call |
| The interface | `IExample` | the same `find` call, or the files under `model/interfaces/` |
| The new sender port | `pStatus` (*example*) | a name you choose; a provide port often starts with `p` |
| The new receiver port | `rStatus` (*example*) | a name you choose; a require port often starts with `r` |
| The connector | `ExampleStatus` (*example*) | a name you choose, unique among the connectors |

## Steps { #steps }

<!-- purpose: The three steps, by hand in the editor and through the assistant -->

### 1. Resolve the names { #resolve-the-names }

<!-- purpose: Making sure the components and the interface are the ones you mean -->

*In VS Code:* open the two component files and the interface file you mean to use.

*Through the assistant:* one `find` call with every name the task gives. Its answer carries the
`baseline` token the change needs.

```json
{"tool": "find", "arguments": {"names": ["ExampleSWC", "ExampleConsumerSWC", "IExample"]}}
```

A name's `word` match splits it at spaces, `_`, `-`, `.` and the capitals inside a word, so "example
consumer" finds `ExampleConsumerSWC`; its `near` match finds a close spelling, "ExampelSWC" finding
`ExampleSWC`. `find` with `text` instead searches the requirements' words: "example value" finds the
requirements about the example value, not the elements named after it, without reading the
requirements layer. `find` with `for_requirement` answers the ranking `swcraft experiment trace`
gives: each place a component with its ports and runnables, or one element. When a requirement has
more than sixteen links, `existing_links_read` is the `read` that lists them all, page by page with
`after`.

### 2. Add the two ports and the connector { #add-the-two-ports-and-the-connector }

<!-- purpose: The one change that adds both ports and the connector together -->

*In VS Code:* add a port under `ports:` in each component file, then a connector file
`model/connectors/ExampleStatus.yaml`:

```yaml
name: ExampleStatus
from:
  swc: ExampleSWC
  port: pStatus
to:
  swc: ExampleConsumerSWC
  port: rStatus
```

The sender's port is `{name: pStatus, direction: provide, interface: IExample}`, the receiver's
`{name: rStatus, direction: require, interface: IExample}`.

*Through the assistant:* one `change_set` carrying all three operations, the ports before the
connector that names them. The `baseline` here is an *example*; send the one `find` answered.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Connect ExampleSWC to ExampleConsumerSWC through a new pair of IExample ports.",
  "complete": true,
  "operations": [
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pStatus"}]}, "value": {"name": "pStatus", "direction": "provide", "interface": "IExample"}},
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleConsumerSWC", "path": [{"field": "ports"}, {"member": "rStatus"}]}, "value": {"name": "rStatus", "direction": "require", "interface": "IExample"}},
    {"operation": "create", "address": {"layer": "connectors", "element": "ExampleStatus"}, "value": {"name": "ExampleStatus", "from": {"swc": "ExampleSWC", "port": "pStatus"}, "to": {"swc": "ExampleConsumerSWC", "port": "rStatus"}}}
  ]}}
```

### 3. Review and approve { #review-and-approve }

<!-- purpose: Approving the proposed change in VS Code's review tab -->

*In VS Code:* nothing to approve when you edited the files yourself; go on to the check.

*Through the assistant:* VS Code opens the review tab with the three additions. **Approve**
applies exactly them; **Reject** leaves the model as it was.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the change, and what the answer says -->

`ExampleSWC` holds the port `pStatus`, `ExampleConsumerSWC` the port `rStatus`, and the connector
`ExampleStatus` joins them. An approved change answers `"status": "committed"` with a new
`baseline`; the next change uses that one.

### What a change answers { #what-a-change-answers }

<!-- purpose: The answer to a proposed change, its delta rows, and what the assistant cannot do -->

The answer to a change set holds its delta, one row per changed field, member or map entry. A
removed element's `before` is its summary, marked `"before_form": "summary"`; the committed answer
carries that summary too, and once the removal is applied no tool answers the removed value, so the
assistant reads the element first when the whole value matters. There is no commit request, no
transaction handle and no force flag. A name that does not resolve is refused with the nearest name
where one exists ("swc holds no 'ExampelSWC'; nearest: ExampleSWC"); a task entry or a sequence step that
does not resolve is named without one.

A `remove` with `"with_dependents": true` and an `update` of a requirement's version with
`"confirm_links": true` need no reads first: the server finds the referrers or the links itself
([Remove an element](remove-an-element.md), [Link requirements](link-requirements.md)). For a
component, the referrers are its connectors, the composition members typed by it and their
delegations, and the task entries and sequence steps naming its runnables; a sequence left with no
step goes whole. With
`"complete": true` the server repairs one thing: when every finding is a component the change
creates or updates speaking an application type no mapping set it names maps, and one declared set
carries all of them without pairing a type the component maps with a second implementation type,
the server names that set in the component's `data_type_mappings` and stages the change again. A
component the change does not touch is never repaired, and a refusal it cannot repair names the set
or the candidates.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the connection is there and the model still passes its checks -->

*On the command line:* the model check, `swcraft check-model`:

```sh
swcraft check-model --project swcraft.yaml
```

*In VS Code:* **SWCraft: Check Model** from the panel.

*Through the assistant:* read the connector back.

```json
{"tool": "read", "arguments": {"address": {"layer": "connectors", "element": "ExampleStatus"}}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals this change meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | a component, port or interface name does not resolve; the message names the nearest one | correct the name and send the change again |
| [`consistency-violation`](refusals.md#consistency-violation) | the two ports do not fit — different interfaces, or the same pair connected twice | read the reason; pick the matching port or drop the duplicate |
| [`identity-conflict`](refusals.md#identity-conflict) | a port or connector of that name already exists | choose another name, or use the existing one |
| [`stale-baseline`](refusals.md#stale-baseline) | the model changed after the `baseline` was read | read a fresh `baseline` and send the change again |
