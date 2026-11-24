<!-- draft: not yet reviewed by the product's author -->

# Add an interface

<!-- purpose: A new sender-receiver interface on a new application type, its mapping, and a port on it -->

<!-- keywords: new interface, create interface, sender-receiver, data element, signal, application type, physical type, percent, range, mapping set, implementation type -->

An interface says what travels between two ports. When what travels is a physical value — a
percentage, a temperature — the interface's data element speaks an **application type**, and a
**mapping set** pairs that type with the implementation type that stores it. This page adds the
type, its mapping, the interface and a port on it in one change.

## Purpose { #purpose }

<!-- purpose: What this change adds to the model, and when you need it -->

Use it when a component has to send a value no interface carries yet. When the value needs no
physical meaning, a data element can name an implementation type directly, as `IExample`'s
`value` names `float32`, and the type and the mapping drop out.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model before you add an interface -->

- The implementation type that stores the value exists — `uint8` in `examples/basic`.
- You know whether the project has a mapping set. `examples/basic` has none, so the change
  creates one; a project that has one adds the pair to it.
- The component that will send the value exists.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The new application type | `ExampleLevel_Pct` (*example*) | a name you choose |
| The implementation type | `uint8` | `read` with no address lists the implementation types |
| The mapping set | `ExampleMappings` (*example*) | the same `read` lists the mapping sets; the component's `data_type_mappings` names the ones it uses |
| The new interface | `IExampleLevel` (*example*) | a name you choose |
| Its data element | `level` (*example*) | a name you choose |
| The sending component | `ExampleSWC` | `find` with the name as you know it |
| The new port | `pLevel` (*example*) | a name you choose |

## Steps { #steps }

<!-- purpose: The three steps, by hand in the editor and through the assistant -->

### 1. Read the model map { #read-the-model-map }

<!-- purpose: Seeing which types and mapping sets exist, and taking the baseline -->

*In VS Code:* look under `model/data_types/implementation/` and `model/data_types/mapping_sets/`.

*Through the assistant:* one `read` with no address answers the model map and the `baseline`.

```json
{"tool": "read", "arguments": {}}
```

### 2. Add the type, the mapping, the interface and the port { #add-the-type-the-mapping-the-interface-and-the-port }

<!-- purpose: The one change that adds the type, its mapping, the interface and the port together -->

*In VS Code:* write the type under `model/data_types/application/`, the interface under
`model/interfaces/`, add the pair to the mapping set, and the port to the component:

```yaml
name: ExampleLevel_Pct
kind: application_type
unit: percent
range:
  min: 0
  max: 100
```

The interface's data element is `{name: level, application_type: ExampleLevel_Pct}`, the mapping
pair `{application: ExampleLevel_Pct, implementation: uint8}`. The component also names the
mapping set under `data_type_mappings`.

*Through the assistant:* one `change_set`. With `complete: true` the server adds the mapping set
to the component's `data_type_mappings` when exactly one set maps the type, and marks that row
`"origin": "server"` in the review. The `baseline` is an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Add a fill-level interface on a new percent type, mapped to uint8, with a sender port on ExampleSWC.",
  "complete": true,
  "operations": [
    {"operation": "create", "address": {"layer": "application_types", "element": "ExampleLevel_Pct"}, "value": {"name": "ExampleLevel_Pct", "kind": "application_type", "unit": "percent", "range": {"min": 0, "max": 100}}},
    {"operation": "create", "address": {"layer": "mapping_sets", "element": "ExampleMappings"}, "value": {"name": "ExampleMappings", "maps": [{"application": "ExampleLevel_Pct", "implementation": "uint8"}]}},
    {"operation": "create", "address": {"layer": "interfaces", "element": "IExampleLevel"}, "value": {"name": "IExampleLevel", "kind": "sender_receiver", "data_elements": [{"name": "level", "application_type": "ExampleLevel_Pct"}]}},
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pLevel"}]}, "value": {"name": "pLevel", "direction": "provide", "interface": "IExampleLevel"}}
  ]}}
```

In a project that already has a mapping set, the second operation adds one pair to it instead:

```json
{"tool": "change_set", "arguments": {"baseline": "rev1:example-baseline", "complete": true, "operations": [
  {"operation": "create", "address": {"layer": "mapping_sets", "element": "ExampleMappings", "path": [{"field": "maps"}, {"member": "ExampleLevel_Pct"}]}, "value": {"application": "ExampleLevel_Pct", "implementation": "uint8"}}
]}}
```

That operation takes the place of the set's `create`, in the same change as the other three: a
component that speaks a type none of its mapping sets maps is refused.

### 3. Review and approve { #review-and-approve }

<!-- purpose: Approving the proposed change in VS Code's review tab -->

*In VS Code:* nothing to approve when you edited the files yourself; go on to the check.

*Through the assistant:* VS Code opens the review tab with the additions, the server's row
marked. **Approve** applies exactly them; **Reject** leaves the model as it was.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the change, and what the answer says -->

`ExampleLevel_Pct` is an application type in percent from 0 to 100, stored as `uint8` through
`ExampleMappings`; `IExampleLevel` carries it as `level`; `ExampleSWC` sends it on `pLevel` and
names `ExampleMappings`. An approved change answers `"status": "committed"` with a new `baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the interface is there and the model still passes its checks -->

*On the command line:* the model check, `swcraft check-model`:

```sh
swcraft check-model --project swcraft.yaml
```

*In VS Code:* **SWCraft: Check Model** from the panel.

*Through the assistant:* read the interface and the component's mappings back in one call.

```json
{"tool": "read", "arguments": {"addresses": [{"layer": "interfaces", "element": "IExampleLevel"}, {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "data_type_mappings"}]}], "form": "full"}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals this change meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`consistency-violation`](refusals.md#consistency-violation) | the component speaks a type none of its mapping sets maps | send the change with `complete: true`, or name the set in `data_type_mappings` |
| [`invalid-identifier`](refusals.md#invalid-identifier) | a new name is not a legal AUTOSAR name — a hyphen, a leading digit | rename it: a letter first, then letters, digits and underscores |
| [`unknown-field`](refusals.md#unknown-field) | a field the layer does not declare — a misspelt `application_type` | use the field the reference page names |
| [`identity-conflict`](refusals.md#identity-conflict) | the type, interface or pair already exists | use the existing one, or choose another name |
