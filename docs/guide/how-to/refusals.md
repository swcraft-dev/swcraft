<!-- draft: not yet reviewed by the product's author -->

# What a refusal means

<!-- purpose: Each code a change or a read is refused with: what it means and what to do next -->

<!-- keywords: refused, refusal, rejected, error, error code, failed change, change refused, why refused, failure, what to do -->

When SWCraft refuses a change, nothing was applied: the model is byte-identical, and the answer
carries a **code**, a message naming the element and the reason, and the addresses involved. A
change is all or nothing, so there is never half a change to undo. Every violation found is
reported, not just the first. The code says what kind of problem it is; this page says what each
one means and what to do. The code is one of eleven.

## schema-violation { #schema-violation }

<!-- purpose: The request is malformed: a value missing, of the wrong kind, or a whole list replaced -->

<!-- explains: schema-violation -->

**What it means.** The request does not have the shape the model needs: a required value is
missing (a new component's `group`), a value has the wrong kind (a word where a number belongs),
a `form` is not one of `summary`, `full`, `index` or `count`, or an `update` sends a whole list
back with one more entry instead of adding the entry. A rule within one element is refused this
way too: a runnable's `wcet` without its `wcet_type`.

**What to do.** Correct the operation the message names. To add one entry to a list, `create` it
at its member address — `{"field": "ports"}, {"member": "pStatus"}` — never `update` the list.

## unknown-field { #unknown-field }

<!-- purpose: A layer, element kind or field the model does not declare -->

<!-- explains: unknown-field -->

**What it means.** An address or a value names a layer, an element kind or a field the model does
not have — a misspelling, or a field from another layer. The message lists the fields the
element does declare.

**What to do.** Use a field the message lists, or look it up on the layer's page under
[The model layers](../reference/model/index.md). An empty answer is never this code: an empty
answer means nothing matches.

## invalid-identifier { #invalid-identifier }

<!-- purpose: A name that is not legal in the generated AUTOSAR file -->

<!-- explains: invalid-identifier -->

**What it means.** A name that goes into the AUTOSAR file is not a legal AUTOSAR name: it must
start with a letter and hold only letters, digits and underscores, and stay within the length
limit once SWCraft has lengthened it. `N-1` is refused for its hyphen.

**What to do.** Rename it. [What a name has to be](../model.md#what-a-name-has-to-be) gives both
rules and the names SWCraft derives from yours.

## broken-reference { #broken-reference }

<!-- purpose: A name that points at nothing: a missing element, port or type -->

<!-- explains: broken-reference -->

**What it means.** Something names an element or a member that does not exist — a port typed by
an interface nobody declared, a connector naming a port the component does not have, an address
of an element that is not there. Where a near name exists the message names it:
*"ExampleSWC.ports holds no 'pOutt'; nearest: pOut."*

**What to do.** Correct the name, or create the missing element in the same change. When the
change removes an element, remove or repoint everything that names it in the same change.

## identity-conflict { #identity-conflict }

<!-- purpose: A name already taken, or one name removed and created again in one change -->

<!-- explains: identity-conflict -->

**What it means.** A `create` names an element, member or entry that already exists, or one
change removes and creates the same name. A `create` never quietly overwrites.

**What to do.** To change an element that exists, `update` it; to add a new one, choose a free
name. A rename removes the old name and creates the new one, so it never takes a name twice.

## immutable-field { #immutable-field }

<!-- purpose: A field that cannot be changed in place, such as a name or a component's group -->

<!-- explains: immutable-field -->

**What it means.** The `update` targets a field that never changes in place: an element's or a
member's name, a component's `group`, the `kind` of a hardware element, or a field that is part of a
list entry's identity.

**What to do.** A new name is a `remove` of the old element and a `create` of the new one in one
change, with every reference repointed in the same change. A changed identity field of a list entry
is the same: that entry removed and one created at the new identity, in one change.

**What it means, for one entry of a list.** The `update` is at one entry of a list — a runnable's
`read`, a port — and sends only the field that changes. An `update` at an entry's address replaces
the **whole** entry, so a payload without the entry's identity is refused.

**What to do.** Send the one field at its own address, `[…, {"member": …}, {"field": "<name>"}]` —
the refusal names that address — or send the entry whole, its identity included.

## stale-baseline { #stale-baseline }

<!-- purpose: The model changed after the change was prepared; read again and restate it -->

<!-- explains: stale-baseline -->

**What it means.** Every change carries a `baseline` token naming the state of the model it was
written against. The model has changed since that token was read — you approved an earlier
change, edited a file, switched branches, or another change landed — so the change describes a
model that no longer exists. SWCraft refuses it rather than apply it to a state nobody reviewed.
Nothing was applied.

**What to do.** Read a fresh `baseline`, check that the change still makes sense against what is
there now, and send it again; it is reviewed again. A committed change answers with its new
`baseline`, so the next change uses that one.

*Through the assistant:* the model map answers the fresh token.

```json
{"tool": "read", "arguments": {}}
```

## unauthorized-delta { #unauthorized-delta }

<!-- purpose: A change outside the scope the task was approved for -->

<!-- explains: unauthorized-delta -->

**What it means.** The task was bounded in advance to an approved scope, and the change reaches
outside it. The message names the addresses the scope does not cover.

**What to do.** Decide whether the wider change is wanted. If it is, approve a wider scope; a
retry of the same change is refused again.

## partial-change-set { #partial-change-set }

<!-- purpose: The operations of a change are in an order where one needs a later one -->

<!-- explains: partial-change-set -->

**What it means.** An operation addresses something a later operation of the same change creates
— a port of a new component, before the operation that creates the component. Nothing is
missing; the order is wrong, and SWCraft never reorders a change for you.

**What to do.** Put the creating operation first, or fold the part into its value.

## consistency-violation { #consistency-violation }

<!-- purpose: The model as a whole would break one of its rules, such as a mismatched connector -->

<!-- explains: consistency-violation -->

**What it means.** Every name resolves and every element is valid on its own, but the model the
change leaves breaks a rule between elements: a connector whose two ports speak different
interfaces, the same pair of ports connected twice, a component with no budget for a strictly
checked memory or with no `handle_termination_and_restart`, a component speaking an application
type none of its mapping sets maps. A key in a model file that no field declares
(`model.unknown-key`) refuses every change until it is renamed or deleted by hand.

**What to do.** Read the reason and change the model so the rule holds. One case the server
repairs itself when the change carries `complete: true`: naming the one mapping set that maps the
types a component speaks. [The checks](../reference/rules.md) lists every rule.

## over-budget { #over-budget }

<!-- purpose: An answer too large to send; the refusal names narrower questions -->

<!-- explains: over-budget -->

**What it means.** A read's answer would be larger than the server sends, so it is refused rather
than cut. The refusal says how large it would have been and lists `hints`: complete, narrower
requests, in narrowing order.

**What to do.** Send the first hint as it stands, and the next only if that one is refused too.
No `hints` means nothing narrower exists: report the count and what could not be read.
