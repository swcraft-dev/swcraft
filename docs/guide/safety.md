<!-- draft: not yet reviewed by the product's author -->

# Safety integrity levels

<!-- purpose: Working with ASILs: where a level originates, where you write it and what the checks compare -->

<!-- keywords: freedom from interference -->

An ASIL — automotive safety integrity level — is one of `A`, `B`, `C` or `D`, and it says how
much rigour a piece of the system needs. It is not a property a component has because of what it
does. It **originates on a safety requirement**, and it reaches a component because that
requirement was **allocated** to it.

SWCraft models exactly that, and nothing more. Four fields carry a level:

- a **requirement** declares the level allocated to it;
- a **component** may declare a level of its own;
- a **data element** — one signal on one sender-receiver interface — may declare the level of the
  information it carries;
- a **partition** — the group of tasks that run together on one core — may declare the level it
  *is*. [Partitions](#partitions) below has it.

A **core** and a **memory** may declare something related but different — the highest level they
are qualified to *host* — and that has a section of its own below.

Everything else about safety — the hazard analysis, the safety goals, the argument that the
architecture is adequate — lives outside this tool. What SWCraft does is hold these declarations
and tell you when they contradict each other.

## Writing a level { #writing-a-level }

<!-- purpose: Writing an ASIL on a requirement, a component, a signal or the hardware: the eighteen values -->

All four fields are called `asil` and all four take the same eighteen values: the five levels and
thirteen decomposed ones, both below.

```yaml
# model/requirements/REQ_BRAKE_001.yaml
id: REQ_BRAKE_001
title: Torque request is limited
description: The powertrain shall limit the requested torque to the arbitrated maximum.
req_type: req
version: 1
source: architecture
asil: D
```

```yaml
# model/swc/powertrain/TorqueLimiter.yaml
name: TorqueLimiter
type: application
description: Limits the requested torque
asil: D
handle_termination_and_restart: no_support
satisfies:
  - REQ_BRAKE_001@1
```

The requirement is **allocated** to the component by the component's `satisfies`: the requirement
names no element that realizes it, and the element names the requirement, with the version it was
written against (a derived requirement names only its parent, in `derived_from`).

The five levels are `QM`, `A`, `B`, `C` and `D`, written in capitals — that is how the standard
spells them, and SWCraft does not accept another spelling. Any value outside the eighteen is a
**broken file**: the model will not load at all, and you get the file's path, the field and the
value rather than a finding at the end of a run.

The field is **optional and has no default**. A model that says nothing about safety is written
exactly as it is written today: the key is absent from every file, and nothing adds it.

## A decomposed level: `X(Y)` { #a-decomposed-level-xy }

<!-- purpose: Writing a decomposed level such as B(D), and what the checks read from it -->

A requirement that came out of an ASIL decomposition is written with the level it now carries
first and, in parentheses, the level it was decomposed from: `B(D)` is a `B` requirement that is one
half of a decomposed `D` one. The thirteen decomposed values are the ones AUTOSAR's Safety
Extensions list, exactly as spelled there:

`QM(A)`, `QM(B)`, `QM(C)`, `QM(D)`, `A(B)`, `A(C)`, `A(D)`, `B(B)`, `B(C)`, `B(D)`, `C(C)`,
`C(D)`, `D(D)`.

`A(A)` is not among them — the standard's list leaves it out, so SWCraft refuses it like any other
spelling it does not know. A space before the parenthesis (`B (D)`) or a lower-case letter is
refused too.

Any of the four fields — a requirement, a component, a data element, a partition — takes a
decomposed value. A core's or a memory's capability does not: what a core can host is not
decomposed, so those two keep the five levels.

**Every check compares by the level in front.** `B(D)` is compared as `B`: a `B(D)` component under
a `B` requirement is not below it, and a task running a `B` and a `B(D)` component runs one level,
not two. The value in parentheses is kept and shown — a finding names `B(D)` where your file says
`B(D)` — and one check compares it, with the requirement it was derived from (below). When a
component takes the highest level of its requirements
and two of them carry the same level in front, the plain one wins over a decomposed one (`B` beside
`B(D)` gives `B`), and between two decomposed ones the higher value in parentheses wins (`B(C)`
beside `B(D)` gives `B(D)`).

**`QM(Y)` declares no ASIL**, exactly as `QM` does: it is compared as `QM`, and nothing prints it as
a level — no `asil` in the published `needs.json` and no label in the deployment view.

**What this does not reach:**

- **One check reads the value in parentheses, and only against the parent.** A requirement written
  `B(D)` that names another requirement in its `derived_from` is reported when that parent's level
  before decomposition is not `D` — the parent's own value in parentheses when it is decomposed
  itself, its level otherwise — so a `B(D)` may come from a `D` or from a `C(D)`. The finding names
  both values and the two repairs: write the parent's level in the parentheses, or point
  `derived_from` at the requirement it was really decomposed from. A requirement derived from an
  element rather than a requirement, from a parent that declares no level, or that is not decomposed
  at all is not compared.
- **Nothing else checks the decomposition.** That two `B(D)` requirements are independent of each
  other, and that together they realise one `D` requirement, is shown by evidence outside the model
  — the dependent failure analysis. SWCraft stores the notation, compares by the level in front and
  checks the parent's level as above; it does not judge whether the decomposition is valid.
- **A safety goal may not carry a decomposed value, and nothing checks that.** The standard says a
  safety goal takes one of the five levels only, but a requirement here is `req` or `spec`: the
  model has no safety-goal category to restrict.
- **`swcraft query --where asil=B` matches the value as written.** It finds `B` and not `B(D)`: a
  query compares the field's text and knows no ASIL order.
- **A Sphinx-Needs filter on `asil` sees the value as written** too, so a filter for `B` does not
  match an architecture requirement published with `B(D)`.
- **Changing only the value in parentheses is not asked about.** The assistant compares the level
  in front, so moving a requirement from `B(D)` to `B(B)` or to `B` is neither a raise nor a
  lowering, and nothing records that its decomposition context changed.

## `QM` means no ASIL is allocated — it is not the lowest level { #qm-means-no-asil-is-allocated-it-is-not-the-lowest-level }

<!-- purpose: Why QM means no ASIL is allocated, and is not the lowest level -->

This is the one thing to read twice. `QM` stands for *quality management*, and writing it says
**no ASIL is allocated here**; the item is handled by the ordinary quality process. ISO 26262 is
explicit that QM is *not* an ASIL, so `QM` is not "ASIL zero" and not a rung below `A`.

Because of that, **writing `QM` and writing nothing are different statements**, and SWCraft reads
them differently:

| What the file says | What it means | What the check does |
|---|---|---|
| `asil: QM` | you declare that no level is allocated here | takes you at your word, and reports the component if a requirement allocated to it carries a level |
| no `asil` key at all | you have not said | works the level out from the requirements allocated to the component |

So `asil: QM` on a component under an ASIL-D requirement **is reported**. That is the case worth
having: it is exactly the sentence "this one is QM" written down where a reviewer can see it
disagree with the requirement.

## A component that says nothing takes the highest level of its requirements { #a-component-that-says-nothing-takes-the-highest-level-of-its-requirements }

<!-- purpose: The level a component takes when it declares none: the highest of its requirements -->

If a component declares no level, its level is the **highest** of the requirements it satisfies —
the ones named in its own `satisfies`, in its ports' and in its runnables'. A link on a port or a
runnable allocates the requirement to the component that owns it. A component under one `B`
requirement and one `D` requirement is at `D`. A component under requirements that declare no level
at all is at no level. The version after the `@` plays no part here: a link that needs confirmation
still allocates.

If a component *does* declare a level, it may declare it **equal to or above** that highest one and
be right. Declaring a component `D` when its requirements are `B` is a normal thing to do — you
have decided to develop it to a higher rigour than any single requirement demands, and nothing
objects. Only **below** is a contradiction.

## What the check reports { #what-the-check-reports }

<!-- purpose: Reading a safety finding: the component, its declared level and the requirements above it -->

`swcraft check-model` (and `swcraft validate`, which runs the same checks) reports one finding per
component whose **declared** level is below the highest level of the requirements allocated to it.
The finding names the component, the level it declares, and every requirement above it with its
level:

```text
SWC 'TorqueLimiter' declares ASIL B, below the ASIL D of the requirement(s)
allocated to it: 'REQ_BRAKE_001' (ASIL D).
```

It names two places to fix, and does not choose between them: raise the component, or lower the
requirements above it. Which one is right is a question about your system, not about your model.

The assistant is held to the same finding. A change that lowers a component below its requirements,
or raises a requirement above a component that has declared its own level, is **refused** — the
model that would result does not pass, so it is not written. Lowering a level — by the level in
front, so `B(D)` to `B(B)` is no lowering — is also one of the changes the assistant asks you about
separately, for a different reason: other checks read a
component's level and simply stop reporting when it drops — the one about what it consumes, the
one about the components it shares a task with, and the one about the partition it runs in. The two
are not alternatives — you are asked, and a change that would leave a finding standing is refused
whatever you answer.

## Where the level shows up { #where-the-level-shows-up }

<!-- purpose: Where a safety level shows up: the checks and the needs file, and why the ARXML carries none -->

- `swcraft check-model` and `swcraft validate` read all four fields, and the three arguments that
  answer a finding — a port's `measures`, a task's `coexistence` and a partition's `coexistence` —
  with them.
  [The checks](reference/rules.md) lists every safety rule with the rest.
- `swcraft generate needs` writes an architecture requirement's level as the `asil` field of its
  published need, as written (`B(D)` stays `B(D)`) — for a declared level other than `QM` or
  `QM(Y)`, so a requirement that allocates nothing carries no `asil`. `asil` is not a field
  Sphinx-Needs knows by itself; the requirements team declares it in its `conf.py`
  ([Publishing the architecture to the requirements
  team](cli.md#publishing-the-architecture-to-the-requirements-team)). A requirement the team
  delivered is never published, so its level is not written back. An element's own level is not
  exported: compare it in the model, or let `check-model` do it.
- **The generated ARXML carries nothing.** A model that declares a level everywhere exports byte
  for byte the same file as the same model with the fields removed. That is deliberate: AUTOSAR's
  component template has no ASIL attribute, and the one spelling that ever existed for it is
  obsolete and has no successor. The level lives in the model, where your review reads it, and it
  is not smuggled into an export that has no place to put it.

The full key list for these files is on the [Requirements](reference/model/requirements.md) and
[SWCs](reference/model/swc.md) reference pages.

## The level on a signal { #the-level-on-a-signal }

<!-- purpose: Writing a safety level on a data element, and what the checks compare it with -->

A level belongs to *information* as much as to the thing that computes it, so a data element takes
one too. It goes on the element and not on the interface, because one interface often carries a
rated signal and an unrated one side by side, and not on the
type, because the same `uint8` is used everywhere.

```yaml
name: ISafetySignals
kind: sender_receiver
data_elements:
  - name: torque
    implementation_type: uint8
    asil: D
  - name: speed
    implementation_type: uint8
    asil: B
  - name: raw          # no level: unrated, and nothing is compared against it
    implementation_type: uint8
```

Once a signal is rated, two questions can be answered from the model alone.

- **May this component produce it?** A component that provides an element rated *above* its own
  level is reported. There is no way to argue that one away, and that is deliberate: a producer
  below what it produces is a mis-declaration, not something a mitigation repairs.
- **May this component consume it?** A component that requires an element rated *below* its own
  level is reported too — but this one *can* be argued, and the next section is how.

An element that declares no level is **unrated**, which is not the same as `QM`. Nothing is compared
against it in either direction.

## Two arguments that close a finding { #two-arguments-that-close-a-finding }

<!-- purpose: Closing a safety finding with an argument: a port's measures, or coexistence on a task -->

Some findings are not mistakes. Taking a lower-rated signal into a higher-rated component is
ordinary engineering when there is a mechanism that makes it sound, and running components of
different levels on one task is ordinary when there is an argument that they cannot interfere.
SWCraft's answer is that you **write the argument down**, in the model, next to the thing it is
about. There is no switch that turns a check off.

**A measure, per port and per signal.** On the port that consumes the signal:

```yaml
ports:
  - name: rStatus
    direction: require
    interface: IPlainSignals
    measures:
      status: End-to-end protection profile 4 on the received value.
```

That closes the consumer finding for `status` on that port, and for nothing else. The text must say
something: an entry whose text is empty or only spaces, `status: ""`, is an **error** of its own
(`safety.measure-blank`), on the entry's line in VS Code, because an empty text names no mechanism.
Write the mechanism, or delete the entry and answer the finding that comes back.

**A coexistence argument, per task.** On the task that runs the components:

```yaml
name: Task_Control
period: 0.01
priority: 10
partition: P_Control
coexistence: >
  The lower-rated runnable cannot delay the higher-rated one past its deadline:
  timing protection bounds it; the argument is recorded in the item's safety case.
```

That closes the mixed-levels finding for that task. A task's argument is about **time** — sharing
one task's execution. Sharing one partition's memory is a different question with an argument of
its own, on the partition; [Partitions](#partitions) below has it.

Two things about both of them are worth being blunt about.

- **They are declarations, and nothing verifies them.** No part of SWCraft reads the text, grades
  it, or checks that the mechanism you named exists; the one thing checked is that a measure's text
  is not empty. A measure whose text is `TODO` closes the finding exactly as a real one does. What you get from writing it is that the argument is *in the
  model*, next to the port or the task it is about, where a review will find it.
- **Writing one is asked about separately.** Because declaring an argument makes a check stop
  reporting, the assistant asks you about it as its own question, after the ordinary "is this the
  change you meant?" — approving the edit is not approving the argument.

Two more findings exist purely to stop an argument being *lost*: on a **require** port, a measure
naming a signal the port's interface does not declare (usually a typo), and, on a `provide` port, a
`measures` map at all — no check will ever look at one there. The two divide by direction on
purpose: a map on a provide port has one repair, which is to remove it, and that answers the
spelling question too, so you get one finding and not two. A third catches an argument that says
nothing: on a require port, a measure whose text is empty or only spaces. All three are reported
rather than silently ignored, and none stops the file loading.

## The level the hardware is qualified to host { #the-level-the-hardware-is-qualified-to-host }

<!-- purpose: Writing the level a core or a memory is qualified to host, and what the checks compare -->

A requirement, a component and a signal each carry the level they *need*. A core and a memory carry
something else: the highest level they are **able to host**. The field is called `asil_capability`
rather than `asil` for exactly that reason — it is a different quantity, and the two checks below
compare the two on purpose.

```yaml
# model/hardware/CORE0.yaml
name: CORE0
kind: core
threshold: 0.7
asil_capability: B
```

```yaml
# model/hardware/RAM0.yaml
name: RAM0
kind: memory
type: RAM
size: 4096
accessible:
  - CORE0
threshold: 0.5
asil_capability: B
```

It takes the five levels — never a decomposed value — and it is optional with no default. A core or a memory that declares
nothing is **unrated**, and nothing is compared against it. One that declares `QM` says it hosts no
ASIL at all, so every rated element placed on it is reported. The processor takes no capability:
nothing is placed on a processor directly — partitions sit on its cores, and budgets are charged to
its memories.

**What the value is a statement about.** Not the silicon on its own. Whether a core can host an
ASIL D component depends on how it is configured and on what its safety manual assumes. The same
part can carry a different claim when its cores run split rather than locked in step; the memories
of one chip are not always rated alike, and the ones that belong to a lockstep core can be rated
higher than the rest; two domains of one device can be rated differently. So what you write is
your project's statement about the core or the memory **as you have configured it**, and nothing in
SWCraft checks it against a datasheet.

Two questions become answerable from the model:

- **May this partition sit on this core?** A rated partition is reported when its core's capability
  is below the partition's level — one finding for each partition, **whether or not any task runs in
  it**, because the partition's level is your statement about what it will host.
- **May this component use this memory?** A component is reported when it charges a memory **more
  than nothing** and the memory's capability is below the component's level — one finding for each
  component and memory. The level compared is the component's own, or the highest of its
  requirements when it declares none.

```text
Partition 'P_Control' (ASIL D) is on core 'CORE0' (ASIL capability B): a partition placed on a
core not declared able to host its level.
```

Each finding names every place a repair could go and does not choose between them: lower the
partition, raise the capability or move the partition to another core; for a memory, raise its
capability, lower the component — its own level, or the requirements it takes its level from — or
set the component's budget for it to `0`.

**A budget of `0` is not a use**, here exactly as it is for a memory's `accessible` list. Under the
default `enforcement: strict` every component has to write a budget for every memory, so `0` is how a
component says it uses none of that one — and it is the only way an ASIL D component can name a `QM`
memory without a finding.

**A component no task runs** is never checked against a partition or a core, because it is not in
one. Its memory budgets are still checked: that question is asked of the component's own budgets,
not of a task.

Raising a capability is one of the changes the assistant asks you about **separately**, because it
makes the checks stop reporting what is placed on that core or memory — and so is clearing
one that stood below `D`, because an element that declares nothing is compared against nothing.
Declaring a capability where there was none, or lowering one, makes the checks report more and is not
asked about. **Moving what is placed is not asked about either, wherever it moves**: a partition
moved to another core, a task moved to another partition, or a budget moved to another memory, set
to `0` or removed, can make a finding disappear without a second question — even when the core,
the partition or the memory it moves to declares nothing at all. You are asked when a core's or a
memory's own statement changes; a move changes what is placed on it, and the core you moved away
from still refuses what it refused.
`swcraft resource-report` shows neither finding: it reports budgets, and a level is not a budget.
`swcraft check-model` does.

## Partitions { #partitions }

<!-- purpose: Writing partitions: tasks on one core under one memory protection, and their safety checks -->

A **partition** is the group of tasks that run together on one core under one memory protection —
AUTOSAR's OS-Application. Every task names the partition it runs in, and takes its core from it;
the partition names the core.

```yaml
# model/hardware/P_Control.yaml
name: P_Control
kind: partition
core: CORE0
asil: D
coexistence: >
  The QM monitor's data is in its own memory region; the memory protection unit
  denies it write access to the ASIL D data. Recorded in the item's safety case.
```

```yaml
# model/tasks/Task_Control.yaml
name: Task_Control
period: 0.01
priority: 10
partition: P_Control
runnables:
  - swc: TorqueLimiter
    runnable: Run
```

`asil` is the level the partition **is**, written like a component's — not the level it is able to
host, which is what a core's and a memory's `asil_capability` says. It is optional with no default:
a partition that declares none is **unrated**, and nothing is compared against it — except that an
unrated `trusted` partition is reported beside a partition rated above `QM` (*Whether a partition
is trusted*, below). `coexistence` is
the partition's argument for sharing its memory between components of different levels, as free
text, and like every argument nothing reads it.

A project on a single core without memory protection still has every task name a partition; it
declares one, without a level, on its core. That is a convention of SWCraft rather than of AUTOSAR,
which configures no OS-Application at all there, and it is what a new project starts with:
`swcraft init` writes `PARTITION0` on `CORE0`, unrated. A partition no task names is legal, and is
checked like any other.

An unrated partition is compared with nothing — save the `trusted` one just named — and that
includes the core under it: a component
rated above a core's `asil_capability` is reported through the partition it runs in, and only when
that partition is rated. A project whose cores declare a capability rates its partitions to keep
that check.

Once a partition is rated, four questions are answered from the model, and one more about where a
component's tasks are:

- **Is a component above its partition?** A component whose level — its own, or the highest of its
  requirements — is above the partition's is reported, once per partition and component. No
  argument closes it: a partition cannot host more than it is.

  ```text
  SWC 'TorqueLimiter' (ASIL D) is run by partition 'P_Body' (ASIL B): a component rated above the
  partition that runs it.
  ```

  The repairs: raise the partition, or lower the component — its own level, or the requirements it
  takes its level from.

- **Is a component below its partition?** A component rated below the partition's level, `QM`
  included, shares the partition's memory protection with the higher-rated ones. It is reported
  once per partition and component unless the partition carries a `coexistence` argument.

  ```text
  SWC 'Monitor' (ASIL QM) is run by partition 'P_Control' (ASIL D), which declares no coexistence
  argument: a component rated below the partition that runs it, sharing its memory protection.
  ```

  The repairs: write the partition's argument, lower the partition, or raise the component.

- **May this partition sit on this core?** The question in the section above: a rated partition on
  a core declared able to host only less, empty or not.

- **May this partition's components use this memory?** A rated partition is reported once per
  memory when a component it runs charges that memory more than nothing and the memory's
  capability is below the **partition's** level — however many of its components charge it.

  ```text
  Partition 'P_Control' (ASIL D) runs 'Monitor', 'TorqueLimiter', budgeting memory 'RAM0' (ASIL
  capability B): a partition whose components charge a memory not declared able to host its level.
  ```

  The repairs: raise the memory's capability, lower the partition, or set a component's budget for
  that memory to `0`.

- **Does one component instance run in two partitions?** An instance whose runnables are run by
  tasks of two or more partitions is reported — its one instance would run in more than one
  partition. It is judged **per instance**: a task entry names its component by `swc` while the
  type is placed once and by `instance`, its path below the root composition, when it is placed more
  than once ([The model](model.md#what-it-runs-on)), so two occurrences of one type in two
  partitions are two instances and no finding. A type placed nowhere below the root is judged as
  one instance.

  ```text
  SWC 'TorqueLimiter' is placed once below the root composition, at 'TorqueLimiter', and its
  runnables are run by tasks 'Task_Control', 'Task_Monitor' of partitions 'P_Control', 'P_Monitor':
  one instance would run in more than one partition.
  ```

  The repairs: move a task to the partition the others run in, or move the runnable to a task that
  runs there. **It is not judged** when the model has no root composition, or more than one, or
  compositions below the root that contain one another in a cycle; those are reported by their own
  checks.

**Whether a partition is trusted.** The operating system gives every partition one of three
classes, and `trust` says which:

- `non_trusted`, the default — memory protection confines it;
- `trusted_with_protection` — trusted, but confined for memory like a partition that is not;
- `trusted` — not restricted for memory at all.

```yaml
# model/hardware/P_Gateway.yaml
name: P_Gateway
kind: partition
core: CORE0
asil: B
trust: trusted
```

A partition that writes no `trust` is `non_trusted`, and the key is written back only when it says
something else. The operating system protects a partition's data from being written by the
partitions that are **not** trusted, and only from them, so a trusted partition can write into the
memory of every other partition on the processor. Two checks follow, and both judge `trusted`
alone — a `trusted_with_protection` or `non_trusted` partition is confined and never reported by
either:

- **Is a trusted partition below the highest level on the processor?** A trusted partition rated
  below the highest level any partition of the processor declares is reported, naming the partition
  that declares it; a written `QM` counts as a level. A trusted partition at that level is not.

  ```text
  Partition 'P_Gateway' (trusted, ASIL B) shares the processor with partition 'P_Control' (ASIL D):
  a trusted partition, unrestricted for memory, rated below a partition whose memory it may write.
  ```

  The repairs: confine the partition, raise it to the highest level, or lower the higher partition.

- **Is a trusted partition unrated beside a rated one?** A trusted partition that declares no level
  is reported when the highest level on the processor is above `QM`: nothing says how far it can be
  relied on, and it can write the memory of a partition that carries an ASIL. Beside `QM` partitions
  only, it is not reported — they claim no ASIL to protect.

  ```text
  Partition 'P_Gateway' (trusted, unrated) shares the processor with partition 'P_Control' (ASIL D):
  a trusted partition of unknown assurance, unrestricted for memory, beside a partition rated above QM.
  ```

  The repairs: confine the partition, or declare its level.

A partition on a core the model does not declare, and a second file declaring a partition of the
same name, are judged by neither check — their own checks report them. The deployment diagram shows
`trusted` after the partition's level ([Diagrams](diagrams.md)).

**What you are asked about.** Lowering a partition's level, clearing it, or removing a rated
partition is asked about separately, because the checks above stop reporting what that level would
have refused. So is writing a partition's `coexistence`, and so is a change that makes an argument
that already stood start answering — a task moved into an argued partition, making it mixed.
**Raising a partition's level is not asked about**: a level is a rating, and raising a rating is
never asked about anywhere — raising a signal's level works the same way. The limit that leaves is
worth knowing: raising a partition above a component can close a finding that said the component was
above it, with nothing new reported in exchange where the core and the memories declare no
capability. A task moved to an unrated partition, or a partition moved to an unrated core, is a move
and is not asked about either.

Making a partition `trusted`, or creating one that is, is asked about separately too. It does not
make a check stop reporting — it widens what the partition may write, and the check above already
refuses a trusted partition below the highest level, so you are asked where the change is allowed.
So is **confining** a trusted partition — `trusted` to `trusted_with_protection` or `non_trusted` —
because that makes both trust checks stop reporting it, and nothing checks that the operating system
really confines it. Moving between `non_trusted` and `trusted_with_protection` is not asked about.
Removing a trusted partition is asked about only as any rated partition's removal is, so removing
one that declares no level closes its finding without a second question. The same holds when you
remove a trusted partition and create a confined one under another name in one change set: the
assistant compares partitions by name, and a partition whose content changed under a new name is a
removal and a creation, not a rename, so the confinement question is not asked.

## What the checks do not see { #what-the-checks-do-not-see }

<!-- purpose: What the safety checks do not see, so a quiet check is not read as agreement -->

Stated plainly, because a check that is quiet is easy to mistake for a check that agrees with you.

- **Only a component, its ports and its runnables allocate a level.** A requirement may also be
  written in the `satisfies` of a composition, an interface, a task or a hardware element, and
  SWCraft checks that it names a requirement that exists — but such a link allocates the
  requirement's level to nothing: a component using the interface, the components a composition
  places and the tasks on a core are not judged by it. A partition declares its own level, and a
  link on it does not change that. A signal — one data element — cannot carry a link at all.
- **An absent level is not a `QM`.** There is no way to write "QM, and I know about the ASIL-D
  requirement". If you want that recorded, it belongs in the requirement's own text for now.
- **A component the importer could not resolve is judged like any other.** Such a component is an
  external component with no runnables and ports that name no interface, but its level is something
  *you* wrote, so the check reads it. A signal crossing one of its ports has no level either: the
  producer-and-consumer checks read a port's declared interface, and such a port declares none. When
  [the assistant](assistant.md) proposes turning a rated component that consumes a rated signal
  into an external component — clearing its port's interface — you are asked first, whatever the
  port's `observed_interface` says, and so you are when the conversion replaces that port with one
  of another name and moves a connection into it onto the new one; a component whose rated ports
  only provide is not asked about.
- **A signal through an external interface has no level.** An interface declared outside the project
  may list the names of the data elements its ports use, but a name is all it holds — no type and no
  `asil` — so the producer-and-consumer checks have nothing to compare a port on it against: a
  component above the level of what it consumes is not reported when the interface it consumes is
  external. A `measures` entry on such a port is accepted when the external interface lists the
  element it names, and no level check reads it; an entry naming an element the external interface
  does not list is still reported, because it names a signal nothing in the model declares. When [the assistant](assistant.md)
  proposes re-pointing a rated consumer's port from an interface carrying a rated signal to an
  external one, you are asked first. You are asked the same question when a connection into that
  port — a connector, or a composition's delegation to it — is moved in one change onto another
  port of the same component that uses an external interface, whether that port is new (the old one
  replaced by one of another name) or already existed. The question then names the connections that
  moved and the port each now ends on, as many as fit its length, and counts the rest ("and 4 more
  connections moved off it"); with names so long that not even one fits, the question is cut short
  and says how much it left out. It says that the consumer interface can no longer be checked — not
  that the signal is the same, which nothing in the model shows; if you decline, the reason you are
  given repeats the same note without cutting it. A providing port re-pointed the same way and a port that already names an
  external interface are not asked about at all, and neither is a port replaced by one of another
  name with no connection moved along with it, a connection whose providing end moves in the same
  change, a connection moved onto another component, a connection renamed in the same change it is
  moved, one whose composition changes, one through a composition nested inside another, or a move
  split across two changes. Whether the other file rates the signal is for
  whoever puts the delivery together.
- **It compares two of your own declarations.** It says they disagree. It says nothing about
  whether the allocation is right, whether the requirements are the right requirements, or whether
  anything is missing from the model entirely.
- **No check follows the wire.** The producer-and-consumer checks read one component's own ports.
  A component that produces a signal and one that consumes it in a different part of the hierarchy
  are not compared, and in a large model that can be most crossings. Nor is a connector moved from a
  rated consumer onto another, external component's port: nothing asks about it, because no check
  reads the connector. The one move of a connection you are asked about is the one above, where it
  keeps both components and only its consumer end moves onto an external interface.
- **A partition is a declaration, not a configuration.** The model says which tasks share a
  partition and what level it is; it has no memory protection unit setting, no linker script and no
  timing protection budget, and it cannot put two tasks of one partition under different protection.
  The mixed-levels checks say the components scheduled or partitioned together are rated
  differently. They cannot say interference is possible, and they certainly cannot say it is
  prevented. **SWCraft can report that mixed criticality exists and can never report that it is
  handled.**
- **`trust` is what you declare, not what the operating system is configured to do.** Nothing in
  the model reads the operating system's configuration, so a partition you leave `non_trusted`
  while the operating system trusts it is never reported, and a partition you declare confined is
  believed. Functions a trusted partition offers to others, access rights between partitions and
  timing protection are operating system configuration, and not in the model.
- **A budget belongs to the component type.** Every instance of one type charges the same memories,
  so the partition's memory check cannot tell two instances of one type, in two partitions, apart:
  both are charged to the same memory, and each partition that runs the type is judged by it.
- **Client-server operations carry no level.** An operation is not rated, so a call across levels
  produces no finding at all.
- **An answered finding disappears.** Once you write the measure or the argument, the finding stops
  being reported — there is no "reported but excused" state. What remains is the argument itself, in
  the file, which is where a review should be reading it anyway.
- **A blank argument is not an argument.** A measure whose text is empty or only spaces is an error
  of its own, so the model does not pass with one, even though the signal's name in the map still
  answers the consumer finding. A task's or a partition's `coexistence` is a single field that starts
  out empty, so an empty one is simply no argument, and the mixed-levels finding stands. A text that
  says nothing in words, such as `-` or `tbd`, counts as written in both places.
- **A component no task runs is not placed anywhere.** The partition checks ask about the
  components a partition's tasks run, so a component nothing schedules is never compared against a
  partition or a core, however high its level. Its memory budgets are still checked.
- **A capability that was always too generous stays that way.** The hardware checks compare what a
  core or a memory declares with what is placed on it. Nothing reads a datasheet, a memory
  protection configuration or a safety manual, so a core or a memory declared able to host more than
  it can passes every check here, and nothing ever asks again.
- **An unrated partition, core or memory checks nothing.** A task in a partition that declares no
  level, a partition on a core that declares no capability, or a budget on a memory that declares
  none, is never compared, however high the component's level — the component is still placed there,
  and nothing checks it. Moving a task, a partition or a budget onto one makes a finding go away
  without a second question. `swcraft query hardware --exists asil_capability` lists the cores and
  memories that do declare a capability, and `swcraft query hardware --exists asil` the partitions
  that declare a level; everything else on the hardware is unrated.
- **A misspelt `asil_capability` is an error, and counts as absent until it is fixed.** A core
  written with `asil_capabilty: B`, or a capability written on the processor, carries a key its kind
  does not declare: the editor underlines it and `swcraft check-model` reports it as an error
  (`model.unknown-key`), naming the nearest declared key. Until you fix it, the core is unrated and
  nothing is compared against it.

The key lists are on the [Interfaces](reference/model/interfaces.md),
[SWCs](reference/model/swc.md), [Tasks](reference/model/tasks.md) and
[Hardware](reference/model/hardware.md) reference pages — `asil` on a data element, `measures` on a
port, `coexistence` on a task, `asil`, `coexistence` and `trust` on a partition, and `asil_capability` on a
core and a memory.

## The limit worth stating out loud { #the-limit-worth-stating-out-loud }

<!-- purpose: Why a model that passes the safety checks is not thereby safe -->

**A model that passes these checks is not thereby safe.** They compare declarations in your own
files. They are not a safety analysis, they do not evaluate an argument — least of all the
arguments you write into `measures` and `coexistence`, which nothing reads beyond checking that a
measure is not empty — and they cannot tell
you that a level is correct, or that a partition, a core or a memory can really host the level you
declared for it. They can tell you that two things you wrote do not agree.

No page of this guide is evidence for a qualification or an assessment, and this one least of all.
`swcraft validate` is the authoritative gate for the model and the generated ARXML, and what a
safety case needs beyond that comes from your process, not from this tool.
