<!-- draft: not yet reviewed by the product's author -->

# Ask about requirements

<!-- purpose: Who realises a requirement, what nothing realises, stale links, topics and candidate elements -->

<!-- keywords: requirement, requirements, trace, traceability, satisfies, satisfied by, realised by, realized by, realise, realize, allocated, unallocated, not covered, uncovered, orphan, stale link, outdated link, needs confirmation, newer version, topic, about, search requirements, candidates, could realise, suggest a link, coverage -->

These questions read the model and change nothing. An element realises a requirement when its
`satisfies` list names it as `<id>@<version>`; the answers below are read from those links. In the
calls below, `<REQUIREMENT>` stands for the requirement's `id` and `<TOPIC>` for the words you search
for; send yours in their place.

## Purpose { #purpose }

<!-- purpose: What these questions answer, and when you need them -->

Use it to see what realises a requirement, to find the requirements nothing realises and the links
that a newer version left unconfirmed, to find requirements by their words, and to ask where a
requirement could be linked. Making or confirming a link is
[Link requirements](link-requirements.md).

## Before you start { #before-you-start }

<!-- purpose: What the answers rely on, and how to page through a long one -->

- An empty answer is an answer: nothing matches. It is never a refusal.
- A page names its `total` and, when there are more rows, a `next`: send the same request again
  with `after` set to it.
- *On the command line*, a `read` call on this page runs as `swcraft query --project swcraft.yaml
  --request request.json`, with the call's `arguments` saved as `request.json` and the names filled
  in.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names these questions need, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The requirement, `<REQUIREMENT>` | `REQ_EXAMPLE_001` | step 3's search by its words, or the files under `model/requirements/` |
| The words, `<TOPIC>` | `example functionality` (*example*) | the words the requirement's title or description uses |

## Steps { #steps }

<!-- purpose: Four questions, each by hand and through the assistant -->

### 1. Who realises a requirement { #who-realises-a-requirement }

<!-- purpose: Every element and member whose satisfies names one requirement, with the version it states -->

<!-- recipe: Who realises a requirement -->

*On the command line:* the elements that name it themselves — `swcraft query` with
`--dependents REQ_EXAMPLE_001` and `--depth 1`:

```sh
swcraft query --project swcraft.yaml --dependents REQ_EXAMPLE_001 --depth 1
```

*Through the assistant:* every element and member whose `satisfies` names `<REQUIREMENT>`, one row
per link: its `layer`, `element`, `member` (a port or a runnable, else `null`), `kind` and the
version the link states (`stated`, `null` where it states none), a page of twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "requirements"}, "form": "index", "derived": ["realised_by"], "where": [{"field": "id", "equals": "<REQUIREMENT>"}], "limit": 20}}
```

No row means that nothing realises it; the next step asks that of every requirement at once.

### 2. Requirements nothing realises, and stale links { #requirements-nothing-realises-and-stale-links }

<!-- purpose: The requirements no element satisfies, and the links stated at an older version -->

<!-- recipe: Requirements nothing realises, and stale links -->

*On the command line:* the model check, `swcraft check-model`, warns about every link stated at an
older version:

```sh
swcraft check-model --project swcraft.yaml
```

*Through the assistant:* two requests: the requirements no element satisfies —
`derived.allocation.allocated` is `false`, and `null` for a stub, which no clause on `false` keeps — and every link stated at a
version other than the requirement's (`current`) or at none; each answer is a page of twenty.

```json
{"tool": "read", "arguments": {"address": {"layer": "requirements"}, "form": "index", "derived": ["allocation"], "where": [{"field": "derived.allocation.allocated", "equals": false}], "limit": 20}}
```

```json
{"tool": "read", "arguments": {"address": {"layer": "requirements"}, "form": "index", "derived": ["stale_links"], "limit": 20}}
```

A requirement with no stale link has no row in the second answer. The links are confirmed by an
`update` of the requirement's `version` with `"confirm_links": true`, as
[Link requirements](link-requirements.md) shows.

### 3. Requirements about a topic { #requirements-about-a-topic }

<!-- purpose: The requirements whose id, title and description hold every word you search for -->

<!-- recipe: Requirements about a topic -->

*In VS Code:* search the words in the files under `model/requirements/`.

*Through the assistant:* `find` answers the requirements whose `id`, `title` and `description` hold
every word of `<TOPIC>` — whole words or their prefixes, never a substring — up to sixteen in id
order, each with the words that matched; `more` counts the rest.

```json
{"tool": "find", "arguments": {"text": "<TOPIC>"}}
```

### 4. Elements that could realise a requirement { #elements-that-could-realise-a-requirement }

<!-- purpose: The trace link search's ranked places for one requirement, with the terms they share -->

<!-- recipe: Elements that could realise a requirement -->

*In VS Code:* **Trace Links** in the panel's Experiments section runs the search over every
requirement and shows its proposals in the **Trace Links** view.

*On the command line:* the same search, written as a report by `swcraft experiment trace`:

```sh
swcraft experiment trace --project swcraft.yaml
```

*Through the assistant:* `find` answers the trace link search's ranking for `<REQUIREMENT>`, best
first: each place is a group — a component with its ports and runnables, or one element — with its
`rows`, the `address` of each element or member that could carry the link, and the `terms` the
requirement shares with it.

```json
{"tool": "find", "arguments": {"for_requirement": "<REQUIREMENT>", "candidates": 3}}
```

The places come in the server's order, each with its terms; the assistant explains them and never
re-ranks them. A link you confirm is a change, made as
[Link requirements](link-requirements.md) shows.

## What you get { #what-you-get }

<!-- purpose: What the four answers say for the example -->

In `examples/basic`, `REQ_EXAMPLE_001` is realised by `ExampleSWC` and `ExampleConsumerSWC`, each
linking it at version 1, its current one; so no requirement is unrealised and no link is stale. The
words `example functionality` find `REQ_EXAMPLE_001`. The search for places answers `"places":
"none"` for it and lists the two links it already has under `existing_links`.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming an answer against the requirement and the element that links it -->

*In VS Code:* open the element a row named and read its `satisfies:` list.

*Through the assistant:* `read` the requirement; its `version` is the one a current link states.

```json
{"tool": "read", "arguments": {"address": {"layer": "requirements", "element": "REQ_EXAMPLE_001"}, "form": "summary"}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals these questions meet, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| [`broken-reference`](refusals.md#broken-reference) | the search for places, or a `read` of one requirement, names an `id` the model does not hold | use the `id` as the requirement's file spells it; the message names the nearest |
| [`schema-violation`](refusals.md#schema-violation) | `find` was sent an empty `text`, or two questions in one call | send one question, with at least one word |
| [`unknown-field`](refusals.md#unknown-field) | a `where` field or a `derived` name the model does not declare — a misspelling | use a name the message lists |
