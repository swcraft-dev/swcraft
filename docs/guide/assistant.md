<!-- draft: not yet reviewed by the product's author -->

# The assistant

<!-- purpose: Before you let a chat assistant change the model: what it may do, and what you still decide -->

SWCraft can hand your architecture to a chat assistant — you describe the change you want in
a sentence, it works out which files that means and proposes them. What makes that usable on
a real project is not that the assistant can write YAML. Anything can write YAML. It is that
**the assistant cannot write your files.**

In VS Code the assistant is the **SWCraft Architect** agent, picked from Copilot's chat-mode
dropdown. It has no file editing, no file creation and no terminal; its one file tool, `view`,
only reads. Its instructions tell it to `view` nothing but an answer of SWCraft's that Copilot
saved to a file, and the project's model guard denies a `view` of the model directory, within the
limits listed under [The model guard](vscode.md#the-model-guard); a `view` of another project file
is not denied. Everything it does, it does by
asking SWCraft, and SWCraft answers questions freely, changes nothing without showing you
first, and refuses outright anything that would leave the architecture in a state the checks
reject. This page is about what that means when you are sitting in front of it.

[In VS Code](vscode.md) is where the buttons are — how to pick the assistant out of the
chat-mode dropdown, and what the permission prompts mean. This page is the part underneath.

## What it can reach { #what-it-can-reach }

<!-- purpose: Which tools and files the assistant can reach, and what keeps it off the model's files -->

The assistant is given SWCraft's tools, and `view` to read a large SWCraft answer Copilot saved
to a file — what it is told to use `view` for; the guard keeps it off the model directory only.
The complete list of SWCraft's tools is
[The assistant's tools](reference/tools.md) — generated from the running server, so it is the
real list rather than a summary of one — and it is worth two minutes of your time before you
let one loose on a model you care about. What it says, in short:

- **Most of the tools only answer questions.** What the model can express, what it currently
  holds and how many of something it holds, where a name you used is in the model, what depends
  on what, whether the budgets hold and whether a new component fits. And the requirement
  questions: which elements realise a requirement, which requirements nothing realises or have
  links waiting for confirmation, which requirements are about a topic, and which elements could
  be linked to one — the last is the trace link search's own ranking, which the assistant explains
  and does not re-rank.
  None of these touch a file. Every answer is bounded: an answer too large to send is refused
  with the smaller questions that would answer it — asking what the whole model can express *with
  every description* is one such, and the refusal points at the same question one layer at a
  time, which the assistant follows on its own.
- **One reads this guide.** It searches the guide your installation carries and reads a section
  of it, so it can tell you how something is done; it never reads your project's files for that.
  An answer drawn from the guide ends with a link that opens that section in a VS Code tab — the
  first time you click one, VS Code may ask whether to allow the extension to open the link.
- **A few run something you could have run yourself**: the checks, the build, a component
  export, the planning half of a component import. They write into the generated output
  directory, never into the model.
- **Exactly one can change the model, and it is the subject of the rest of this page.**

The same tools reach every one of the model's fifteen layers, and no layer has a tool of its own.
The data types — application, implementation and base types, and the mapping sets — and the units,
conversions and data constraints they name are read, proposed and removed exactly as components and
interfaces are. Removing one that something still names is refused, and the refusal points at the
field that names it — the type's `unit_ref`, say — not at the element that went. An external
interface's list of the operations or data elements its ports use is read with that interface:
asking for the operations or data elements of every interface answers the declared ones, and where
only external interfaces list a name, the answer is empty rather than a refusal.

There is no way round that. The assistant cannot open an editor, cannot run a shell, cannot
reach for a second server that would. If you ask it to "just quickly fix the file", it has
nothing to fix the file *with*.

That holds for the SWCraft Architect agent, and **not for Copilot's general Agent mode**, which
has the terminal and file editing. There, a question whose answer is too large for one reply can
end with the assistant offering to run a script that parses your model files itself — and a
script you approve runs whatever it was written to run, against your model, with nothing of
SWCraft in between. The project's Copilot instructions tell every mode to ask SWCraft and never
to read the files with a script, the terminal or a file tool. The project's model guard,
`.github/hooks/swcraft.json`, enforces the file-tool half in every mode, within the limits listed
on the page linked below: Copilot runs it before each tool call, and it denies a read or search by
the file tools it knows when that reaches the model directory. It cannot judge a terminal command — only the agent's tool list takes the terminal away. So ask in the SWCraft
Architect agent, and if you ask in the general Agent mode, set Copilot's **Auto** model choice to
optimize for intelligence: optimized for balance, it can route the question to a lighter model,
and one was seen answering a partition question wrong by reading the model's files. What the guard
does not reach is on [In VS Code](vscode.md#the-model-guard).

Codex, set up as [Codex and Claude Code in VS Code](vscode.md#codex-and-claude-code-in-vs-code)
says, has file editing and a shell of its own, and the project carries a second hook file for it,
`.codex/hooks.json`. Codex runs it
before each shell command and each file edit, once you have trusted it with `/hooks`, and it denies
an edit of a file in the model directory and a command that names the model directory, pointing
Codex at the SWCraft tools instead. It cannot judge a command that reaches the model without naming
it — a script, a variable, a tool run over the whole project — and Codex itself calls its hooks a
guardrail, not an enforcement boundary; the list is under
[The model guard under Codex](vscode.md#the-model-guard-under-codex). Claude Code is guarded the same
way by the hook in the project's `.claude/settings.json`, which also judges its reads and searches
and denies its `PowerShell` tool; its limits are under
[The model guard under Claude Code](vscode.md#the-model-guard-under-claude-code). Kiro, set up as
[In Kiro](kiro.md#kiro) says, runs its own hook, `.kiro/hooks/swcraft-guard.json`, before every tool
call, and the guard judges its reads, searches, edits and shell commands alike; a tool it does not
know is denied. Its limits are under [The model guard under Kiro](kiro.md#the-model-guard-under-kiro).

An answer larger than Copilot hands over whole — about 8 KB — is saved to a temporary file, and
the agent reads that file with `view` and answers from all of it. If a whole table is too large
even for that, the refusal names the command that prints it in full — `swcraft query --request
FILE --project PATH`, `PATH` being your project's `swcraft.yaml` — for you to run; a refused *what
does this change affect* names `swcraft impact NAME --project PATH` the same way, after offering
one level of depth and then the count of what it affects, which is never refused — and a long answer
to that question comes as its first twenty with the totals and a way to continue; a refused *what changed* names `swcraft diff
--project PATH` with the revisions you asked about; and a refused budget answer names `swcraft
resource-report --project PATH`.

The agent also carries recipes for the questions people ask most — who reads or writes a data
element, the sender-receiver table, the elements without a description, the components at a
safety level and how many there are, a component's partitions and the components a partition
runs, the connections that cross a partition, the components no partition runs, the ports nothing
connects and those of one component, the tasks on a core, whether a memory has room for a new component, the largest consumer
of a memory, and what a change affects (how many elements first, then which) — so it asks for the
exact answer instead of assembling one. A long answer comes as its first page — twenty rows, five
for the sender-receiver table — with the number of rows in all, and the rest when you ask for them;
the components at each safety level come as one number per level, and those of one level on
request. The one exception is the components no partition runs, which reads every component. Its answer is still a language model's: check one that matters against the
command line.

The assistant's instructions carry the model's schema, so its first call needs only the `baseline`
token, which `discover` answers alone when asked about the whole project. When it needs what a field
means, `discover` describes one layer or all of them: its `summary` follows SWCraft's schema, not
your model, so it is the same size on a two-element project and on a large one, and it leaves out
the descriptions, which make up about 60 % of the `full` form. Each answer names the form it was
given in.

Whether an amount fits a core or a memory has two answers, and the assistant is given both: within
the threshold — the budget, and the answer to *does it fit* — and within the size, the memory's
whole size or the whole core. It is told to lead with the first and to state the sentence SWCraft
gives for the pair, so a memory where the amount fits physically and breaks the budget reads *fits physically,
breaks the threshold by 2048 bytes* rather than a bare *no*. Where a consumer declares no budget, a
verdict the missing load could turn round is *unknown*, and the sentence says how many consumers
declare nothing. The same two verdicts are `swcraft resource-report --fit-bytes N` on
[The command line](cli.md#does-it-fit).

## A change is one proposal, not a series of edits { #a-change-is-one-proposal-not-a-series-of-edits }

<!-- purpose: Why one requested change arrives as one proposal of many files, never as a series of edits -->

Ask for something small — "add a diagnostics interface and wire it into the two components
that need it" — and it is already four files: an interface, two components gaining a port,
and a connector. Ask for something ordinary and it is a dozen.

The assistant does not make those edits one at a time. It states the **whole change as one
proposal**: every element it wants to add, alter or remove, in one submission, which SWCraft
treats as a single unit. That is the shape everything else follows from. A model that is
half-changed is a model whose checks fail for a reason nobody chose, and there is no moment
in this process at which yours is half-changed.

A proposal has exactly three possible endings:

| Ending | What happened | Your files |
|---|---|---|
| **refused** | SWCraft would not accept it, and says precisely why | untouched |
| **waiting** | it is sound, and you have not answered yet | untouched |
| **applied** | you approved it | changed, by exactly what you approved |

Note what is missing from that table: there is no partial ending, and there is no ending in
which your files changed and you were not asked.

## What happens before you are asked { #what-happens-before-you-are-asked }

<!-- purpose: What SWCraft has already checked by the time a proposal reaches you for a decision -->

You are not the first check. By the time a proposal reaches you, SWCraft has already done all
of this:

1. **Taken a private copy of your model.** Everything that follows happens on the copy. Your
   files are not open, not locked and not touched.
2. **Applied the proposal to the copy**, in the order it was stated.
3. **Run the model checks over the result** — the ones
   [`swcraft check-model`](reference/cli.md#swcraft-check-model) runs, against the model as it
   would be, not as it is. Those are the cross-layer checks: names, references, mappings,
   budgets. A check that is an error refuses the proposal; a check that is a warning — a
   requirement that no element satisfies — lets it through, and `swcraft validate` lists the
   warning afterwards. They are **not** the whole of `swcraft validate`, which additionally
   generates the AUTOSAR file and checks *that* against the schema — so a proposal can pass
   here and still have something to say for itself when you generate.
4. **Compared the copy with what you have**, and turned that comparison into the summary you
   are about to read: elements added, removed and changed, in the vocabulary you wrote them
   in.

There are three things SWCraft adds on its own, each only when the assistant asks for it. The
first is a repair. If
every error the checks in step 3 find is a component left with an application data type no
mapping set maps, the component is one the proposal itself creates or changes, and exactly
one of your mapping sets maps every such type and gives none of the component's types a
second implementation type, SWCraft adds that mapping set to the component and checks the
result again. Anything else — another error, a component the proposal never touched, no
such mapping set or several — is refused as it stands, and the refusal names the one set or
the candidates. What SWCraft added is shown to you with the rest of the proposal and marked
**[added by the server]**, or **[the server added to this]** where it extended something the
assistant proposed; you approve or decline it with everything else.

The second is removing a component with what refers to it. When the assistant removes a component
and asks for its dependents to go with it, SWCraft removes the connections with an end on the
component, the composition members of its type and the delegations naming them, and the task
entries and sequence steps that run it — a sequence left with no step goes whole. A requirement
naming the component is not touched. Each of these is shown marked **[added by the server]** and
approved or declined with the rest.

The third is confirming a requirement's links at its version. When the assistant changes a
requirement's `version` and asks for its links to be confirmed, SWCraft finds every `satisfies`
entry naming that requirement at another version, or at none. It removes the old entry and adds
`ID@<new>` in the same list; if `ID@<new>` is already there, it only removes the old one. An entry
whose version suffix cannot be read is left alone, and so is a requirement's `derived_from`. The
assistant may also confirm at the version the requirement already has, which is what a
requirements import leaves behind: the requirement itself does not change, only its links — and
where every link is already current, the proposal is empty. A proposal that sets the same
requirement's version twice and asks for its links to be confirmed is refused, since the links
would follow one version and the requirement end at another. Each row is shown marked
**[added by the server]** and approved or declined with the rest.

If anything still went wrong in steps 2 or 3, you are never asked. The proposal is refused, the
copy is thrown away, and your model is byte-for-byte what it was. Only a proposal that
already passes every check that is an error is ever put in front of a human — which means the question
you are answering is *"is this what I wanted?"* and, for everything those checks cover, no
longer *"is this valid?"*.

## The review, and the one guarantee worth knowing { #the-review-and-the-one-guarantee-worth-knowing }

<!-- purpose: Approving or rejecting a proposal in the review tab, and what nothing can apply without you -->

The question does not come in the chat. VS Code opens a **SWCraft review tab** by itself, on
the project the assistant is working on, with the change in it and **Approve** and **Reject**
under it; one click decides. It is the same tab whichever assistant made the proposal — Copilot,
another chat client or `swcraft agent` in a terminal — and it waits until you answer, the
assistant cancels, or the assistant's session ends; in those last two cases the proposal is
abandoned with your files untouched. With no VS Code window open on the project nobody can
answer, so the assistant goes on waiting. [In VS Code](vscode.md#review-in-vs-code) shows the tab and what it cannot guard against.

The tab shows you the change. Approve it and SWCraft applies **exactly that** — the thing
you were shown is the thing that lands, not a re-run of the assistant's request that might
compute something else the second time. Reject it and nothing happens at all.

**An approved change touches only the lines it needs.** When an element's file already exists,
SWCraft edits those lines and leaves the rest of the file as it stands, so `git diff` shows the
change: your layout and your comments stay where they are. The diff is small rather than the
smallest possible: a list item changed in several fields may show as one line block removed and
another added. What lies inside the lines that change goes with them: a comment inside a replaced
value, inside a key that is removed, or on a list item that is removed or moved. When a key at the
top of a list item is removed, the comment below it moves above the item. When one list item takes
the place of an unrelated one, the old item's comments stay, now on the new item.

Some files cannot be edited that way, a file with mixed line ends for one. SWCraft still makes the
change, and the file is **rewritten whole** in SWCraft's form. The review then says so before you
answer: a **WARNING** block above the change names every such file and why, and the closing question
asks you to commit the change *and* rewrite those files. The values still change only as the review
says; the layout of the rest of each file moves, and its comments are lost — and so is any key
SWCraft does not declare, which the warning line names. It is one question, and one answer covers
both. A file that is already exactly in SWCraft's form has
nothing to lose, so it raises no warning.

A new element is a new file. A **rename** keeps its file: the review shows a removal and an
addition, and the renamed element's new file is the old one with only its name line changed, so
your comments and layout stay. If the old file cannot be edited that way, the new one is written
whole and the **WARNING** block names it, *renamed from* the old name. **Rename in one change and
edit in the next**: a rename bundled with other changes to the same element writes the new file
whole, and the warning names the old file as *not a pure rename*. The `swcraft import` commands write
whole files.

**A key SWCraft does not declare stops every change.** A misspelt key — `derived___from:` for
`derived_from:` — is an error of the model check, and the assistant's checks run the whole model
check on every proposal. So while any file of your model holds such a key, every change the assistant
proposes is refused, and the refusal names the key. The assistant cannot fix it for you, because the
key is no field it can address: rename or delete the key by hand, and the next proposal goes
through. A rename keeps such a key as you wrote it, so the renamed element is refused the same way.

Occasionally you get a **second, separate question**. It appears when the proposal would
weaken a check rather than change the design: raising a core's or a memory's threshold,
enlarging a memory, switching either of them from `strict` to `advisory`, deleting a budgeted
core or memory outright, or admitting a core to a memory's `accessible` list. Those five are
about budgets, and they quietly reduce what SWCraft will ever catch again, for every
consumer, without looking alarming in a list of edits — so they are asked about on their own,
and approving the change does not approve the weakening.

**Three more are about safety integrity levels**, and they work the same way. Declaring a
measure on a port, or writing a task's or a [partition's](safety.md#partitions) coexistence
argument, makes a [safety finding](safety.md) stop being reported — that is what those fields are
for, and it is exactly why writing one is its own question — and it is asked whether you write the
argument into something that already exists or create the port, the component, the task or the
partition with the argument already inside it. And so does taking a level away: lowering or
clearing the level a requirement allocates, removing a requirement from the `satisfies` of a
component or of one of its ports or runnables, lowering a level a signal, a component or a partition declares, or deleting the thing that carried
it — the requirement, the rated signal, the interface that declared one, the port that carried the
argument, a component that bears a level, a rated partition, or a task or a partition whose
coexistence argument answered components at different levels. Each of those makes a check stop refusing and
leaves nothing in its place. So does re-pointing a rated component's `require` port from an
interface that declares a rated signal to an [external interface](model.md), which declares no
level for any signal: the check that held the component to the signal's level has nothing left to compare,
so you are asked, and the question names the port. The same holds when such a component is turned
into an [external component](model.md), whose ports name no interface: the question names the port
and reads `none` for its new interface. You are asked the same question when a connection into
such a port — a connector, or a composition's delegation to it — is moved in one change onto
another port of the same component that uses an external interface, or that names none on an
external component, whether that port is new or already existed. The question names the port the
connection left, and a note below the list names the connections that moved and the port each now
ends on, saying their consumer interface can no longer be checked. The note names as many
connections as fit its length and counts the rest ("and 4 more connections moved off it"); with
names so long that not even one fits, the question is cut short and says how much it left out. The question's line reads as an
interface change even where the port kept its interface, because the connection, not the port,
is what moved: the note is what says so, and it is repeated, without cutting, in the reason you
are given when you decline.

**One more is about where a level may run.** Raising a core's or a memory's
`asil_capability` — or clearing one that stood below `D` — makes the checks that hold
components to the [hardware](safety.md) stop reporting what is placed on it, so it is asked
about on its own. Moving what is placed is not: a partition moved to another core, a task moved
to another partition, or a budget moved to another memory or set to `0`, can close the same
findings without a second question — even onto a partition, a core or a memory that declares
nothing, where nothing is compared at all. You are asked when the hardware's own statement
changes, and a move leaves that statement where it was.

**An argument you wrote earlier can become live without you writing anything**, and that is
asked about too. A measure written on a port ahead of the rating it answers goes live the
moment the signal it names is given a level; a coexistence argument on a task or a partition
whose components happen to be rated alike goes live the moment one of them is rated
differently — or, for a partition, the moment a task moves in that brings a component of
another level. In each case the edit in front of you does not touch the argument — but the
standing argument now closes a finding nobody answered in this change set, so you are asked
about the argument, and the question names the port, the task or the partition it sits on.
The same happens when a port is flipped to `require`, or re-pointed at an interface that
declares the signal its measure names.

Their inverses are not asked about, because they can only make a check stricter: lowering a
threshold, shrinking a memory, `advisory` back to `strict`, withdrawing a core from an
`accessible` list, removing a measure, blanking a coexistence argument, rewording one,
raising a requirement's, a component's, a signal's or a partition's level on its own, and lowering a
capability, declaring one where none stood or clearing a `D` capability. Neither is reordering an `accessible` list — the same cores are
still admitted. And **renaming** anything is not a weakening: a renamed component still
declares the level it declared and a renamed port still carries the map it carried, so nothing
was taken away, however many files the rename touches. A port *replaced* by one of another name
that uses an external interface is not a rename, and where a connection moves onto it with it,
you are asked, as above.

Three things about the level questions are worth knowing, and they all say the same thing:
you are asked when something really dropped, and not otherwise. A requirement that allocates
**no** level allocates nothing, so unlinking or deleting one is not a weakening and you are
not asked — and neither is unlinking one where another requirement holds the same level up, or
where the component still names the same requirement on another of its ports or runnables.
Confirming a link — replacing `@3` with `@4` once you have read the new version — removes nothing
from the component, so it is not asked about either. A requirement's `version`, `source` and
`derived_from` are not levels: changing them is ordinary model content.
A component that stops declaring a level takes the highest of the requirements allocated to
it, so if they hold the same level up, nothing dropped. And on a project that has not
adopted safety levels at all, deleting a component, an interface, a task or a requirement
asks nothing: there was no level, no measure and no argument to take away.

When you **delete** a component that requirements allocate levels to, the question is about
the component — its level, and each port that carried a measure — and not one question per
requirement it named. The allocation those requirements made has nothing left to
protect, and a list of them would bury the one line you can act on.

Everything else is ordinary model content and rides on the single question. Lowering a task's
execution budget, for instance, is not a second question: it is a number sitting in the
change you are reading, and [`swcraft resource-report`](reference/cli.md#swcraft-resource-report)
will show you what it did.

## If the assistant reports changes you never see { #if-the-assistant-reports-changes-you-never-see }

<!-- purpose: When the assistant says it changed the model and the files show nothing: the cause and the fix -->

This is the one configuration issue worth putting on a page, because it looks like a bug and
is not one.

SWCraft's server **fails closed**. If it is started without being told how to reach a human,
it never applies anything: the assistant can do all the work above, and report it perfectly
accurately, and your files stay exactly as they were. Nothing is lost and nothing is
half-done — but nothing is written either.

In VS Code this normally never comes up: the SWCraft extension registers the server itself
and always tells it how to reach you. When it does come up, it is a chat configuration you
wrote by hand — that one carries whatever you gave it.

Starting the server yourself from a terminal is the case where nothing writes it for you, and
you have to say so — and keep VS Code open on the project, where the review tab opens:

```sh
export SWCRAFT_REVIEW=panel
swcraft mcp-serve --project swcraft.yaml
```

## Why a refusal is the product working { #why-a-refusal-is-the-product-working }

<!-- purpose: When the assistant reports a refusal: what it means, and why it is the product working -->

A refusal is the most useful thing the assistant does, and the easiest to misread as a
failure. Every one of them means the same thing: *this would have left the architecture in a
state SWCraft is not willing to write*, and the alternative was writing it.

The kinds you will meet:

- **The result would not pass the checks.** A port typed by an interface nobody declared, two
  elements claiming one name, a component nothing requires, a name that is not legal in the
  generated AUTOSAR file once SWCraft has lengthened it. This is the same answer
  `swcraft validate` would have given you afterwards — you are simply getting it before the
  files move instead of after. One of these checks asks every component whether its behaviour can
  be stopped and restarted (`handle_termination_and_restart`): the assistant is instructed to give
  a component it creates `no_support` unless you say otherwise, and to tell you which value it
  chose, and while any component of your project lacks the value, every proposal is refused naming
  all of them until they have one.
- **The model moved underneath it.** If you edited a file, or another change landed, while
  the assistant was composing, its proposal describes a model that no longer exists. SWCraft
  refuses it rather than merging it blind onto a state its author never saw. The assistant
  re-reads and restates; this costs a few seconds and is the whole reason a stale proposal
  can never overwrite your edit.
- **The proposal is internally inconsistent.** It refers to something it never creates, or
  creates something twice, or states its steps in an order where one needs a thing that comes
  later.
- **It reaches outside what the task was allowed to touch**, in the mode where you agreed to
  that in advance (below).

In every case the refusal names the code, the element and the reason — and reports *every*
violation it found, not just the first, so a restatement does not have to be a guessing game.
Your model is byte-identical throughout. There is nothing to roll back, because nothing was
ever rolled forward.

## The record { #the-record }

<!-- purpose: Finding what was proposed, asked and answered afterwards: the audit record of each proposal -->

Every proposal leaves a trace, whether it was refused, left waiting or applied: a line per
event in `dist/audit.jsonl`, including what you were asked and what you answered. A
proposal's record lists the assistant's changes and, apart from them, any repair, removal or
link confirmation SWCraft added, so the record never credits the assistant with a change it did not state. It is
generated-output territory, but the clean step that empties that directory before a build
spares this file on purpose (and the previous ARXML it moved aside as `model_old.arxml`) — it is
history rather than an artefact, and a build that wiped it would destroy the only record of who
agreed to what.

## Outside the editor { #outside-the-editor }

<!-- purpose: Running the same assistant in a terminal, over SSH or in a scripted rehearsal -->

The same assistant runs in a terminal, which is useful over SSH and for scripted rehearsals:

```sh
swcraft agent chat --project swcraft.yaml
```

It needs a language-model endpoint configured in the environment, which the editor route does
not — there, the editor supplies the model. Review works identically: this host always asks
before anything is applied, and the question opens in the same SWCraft review tab in VS Code, not
in the terminal. So `swcraft agent` in a terminal needs VS Code open on the project with the
SWCraft extension; without it nobody can answer and the proposal waits. Ctrl-C ends the agent and
withdraws the proposal, with your files untouched. A scripted
rehearsal is the one exception: its answers are written in the script, and nobody is asked.

Codex and Claude Code started in a terminal in an onboarded project read the same files as their
VS Code chats, so their proposals wait for the same review tab too; [Codex and Claude Code in VS
Code](vscode.md#codex-and-claude-code-in-vs-code) says how to review elsewhere for yourself alone.
So does `kiro-cli` ([`kiro-cli` in a terminal](kiro.md#kiro-cli-in-a-terminal)).

One flag is worth knowing about:

```sh
swcraft agent chat --project swcraft.yaml --scoped
```

`--scoped` adds an approval *before* the work starts, asked in the terminal. You agree up front
to the boundaries of the task — which parts of the model this turn is allowed to touch — and a
proposal that reaches outside them is refused even if you would have been happy with the change
itself. It does not replace the per-change review in the tab; both happen.

## The standing rule { #the-standing-rule }

<!-- purpose: Why neither the assistant nor its green answer is the gate, and which command is -->

The assistant is not the gate, and neither is a green answer from it.

```sh
swcraft validate --project swcraft.yaml
```

is what decides whether the architecture is allowed out, before and after anything the
assistant does. Run it yourself after a change lands. The assistant ran the *model* checks on
a copy a moment earlier, which is why a proposal you approved rarely surprises you here — but
it is a smaller set than this command's: `validate` also generates the AUTOSAR file and holds
it to the schema, and nothing the assistant did tells you how that went.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next about the assistant: the editor, its tools and the checks it runs -->

| If you want to | Read |
|---|---|
| find the chat-mode dropdown and the permission prompts | [In VS Code](vscode.md) |
| see every tool the assistant can call | [The assistant's tools](reference/tools.md) |
| know what the checks it runs actually require | [The checks](reference/rules.md) |
| do a common task step by step | [How-to](how-to/index.md) |
| do the same work by hand | [Command line](cli.md) |
