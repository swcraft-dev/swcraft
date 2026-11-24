<!-- draft: not yet reviewed by the product's author -->

# Link requirements

<!-- purpose: Adding a requirement, linking an element to it, and confirming links after a new version -->

<!-- keywords: requirement, trace, traceability, link, satisfies, realise, realize, allocate, new requirement, new version, moved to version 2, confirm links, stale link, needs confirmation, trace link -->

An element realises a requirement when its `satisfies` list names it as `<id>@<version>`: the
requirement's `id` and the version the link was confirmed against. A component, a port, a
runnable, an interface, a composition, a task and a hardware element can each carry one.

## Purpose { #purpose }

<!-- purpose: What these changes do, and when you need each -->

Use it to add a requirement and say which element realises it, to link an element to a
requirement that exists, or — after a requirement moves to a new version — to confirm every link
to it at that version.

## Before you start { #before-you-start }

<!-- purpose: What must already be in the model before you link a requirement -->

- The element exists. A requirement you add needs an `id`, a `title`, a `description`, a
  `req_type`, a `version` and a `source`.
- Requirements maintained in another tool come in through
  [Importing requirements](../cli.md#importing-requirements); this page links them.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names these changes need, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| A new requirement | `REQ_EXAMPLE_002` (*example*) | an `id` you choose; `find` answers `"none"` for a free one |
| An existing requirement | `REQ_EXAMPLE_001`, version 1 | `find` with its `id`, or the files under `model/requirements/` |
| The component | `ExampleConsumerSWC` | `find` with the name as you know it |
| The port | `rIn` | the component's `ports:` list |

## Steps { #steps }

<!-- purpose: The three changes, by hand in the editor and through the assistant -->

### 1. Add a requirement and link a port { #add-a-requirement-and-link-a-port }

<!-- purpose: The one change that creates a requirement and links the port that realises it -->

*In VS Code:* write `model/requirements/REQ_EXAMPLE_002.yaml`, then add `REQ_EXAMPLE_002@1` to the
port's `satisfies:` list in the component's file.

*Through the assistant:* one `change_set`. The link is a member of the port's `satisfies` list,
addressed by its own value. The `baseline` is an *example*.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Add REQ_EXAMPLE_002 and link ExampleConsumerSWC's port rIn to it.",
  "operations": [
    {"operation": "create", "address": {"layer": "requirements", "element": "REQ_EXAMPLE_002"}, "value": {"id": "REQ_EXAMPLE_002", "title": "Example input check", "description": "The consumer shall check every value it receives.", "req_type": "req", "version": 1, "source": "architecture"}},
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleConsumerSWC", "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "satisfies"}, {"member": "REQ_EXAMPLE_002@1"}]}, "value": "REQ_EXAMPLE_002@1"}
  ]}}
```

### 2. Link an element to an existing requirement { #link-an-element-to-an-existing-requirement }

<!-- purpose: Adding one link to a requirement the model already holds -->

*In VS Code:* add `REQ_EXAMPLE_001@1` to the element's `satisfies:` list.

*Through the assistant:* one `create` of the member. A component's own list is
`[{"field": "satisfies"}, ...]`; a runnable's is reached through `runnables` as a port's through `ports`.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Link ExampleSWC's port pOut to REQ_EXAMPLE_001.",
  "operations": [
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}, {"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}]}, "value": "REQ_EXAMPLE_001@1"}
  ]}}
```

### 3. Confirm the links at a new version { #confirm-the-links-at-a-new-version }

<!-- purpose: Moving a requirement to its new version and every link to it in one change -->

*In VS Code:* set the requirement's `version:` to 2, then change `@1` to `@2` in every
`satisfies:` list that names it; `check-model` warns about each one you missed.

*Through the assistant:* one `update` of the version with `"confirm_links": true`. The server adds,
for every link at another version, its removal and the link at the new one, each row marked
`"origin": "server"`. When an import already raised the version, send the version it holds.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "REQ_EXAMPLE_001 moved to version 2; confirm every link to it.",
  "operations": [
    {"operation": "update", "address": {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"field": "version"}]}, "value": 2, "confirm_links": true}
  ]}}
```

Each change opens VS Code's review tab; **Approve** applies its rows, the server's included.

## What you get { #what-you-get }

<!-- purpose: What the model holds after each change, and what the answer says -->

`REQ_EXAMPLE_002` exists and `rIn` realises it; `pOut` realises `REQ_EXAMPLE_001`; after the third
change `ExampleSWC` and `ExampleConsumerSWC` both name `REQ_EXAMPLE_001@2`. An approved change
answers `"status": "committed"` with a new `baseline`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the links and finding the ones that still need confirmation -->

*On the command line:* `swcraft check-model --project swcraft.yaml` warns about a requirement
nothing realises and about a link at an old version.

*Through the assistant:* the links at a version other than the requirement's own.

```json
{"tool": "read", "arguments": {"address": {"layer": "requirements"}, "form": "index", "derived": ["stale_links"], "limit": 20}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals these changes meet most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | the link names a requirement the model does not declare; the message names the nearest | correct the `id`, or add the requirement in the same change |
| [`immutable-field`](refusals.md#immutable-field) | an `update` of a link: a link is its own identity | remove the old link and create the new one, or confirm with step 3 |
| [`schema-violation`](refusals.md#schema-violation) | `confirm_links` on anything but a requirement's `version`, or an `id` holding `@` or `:` | move the flag to the version; correct the `id` |
| [`identity-conflict`](refusals.md#identity-conflict) | a requirement with that `id` exists | link the one there, or choose another `id` |
