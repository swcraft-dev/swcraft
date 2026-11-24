<!-- draft: not yet reviewed by the product's author -->

# Import a component

<!-- purpose: Importing a component from an ARXML export and wiring it in: budget, composition, connector -->

<!-- keywords: import, arxml, autosar file, export, bring in, component from another tool, import component, swc description, supplier, take over, hand over, software component, merge, wire in -->

A component exported as ARXML — by SWCraft or by another tool — comes into the model as a new
component, reusing the interfaces and types the project already has. The file carries no memory
budget and no place in the architecture, so the same change gives it both.

## Purpose { #purpose }

<!-- purpose: What the import adds, and when you need it -->

Use it when a component is designed elsewhere and handed over as a component-scoped ARXML file.
A whole model from an empty project is a different import:
[Exchanging with other tools](../cli.md#exchanging-with-other-tools).

## Before you start { #before-you-start }

<!-- purpose: What must be true before you import a component -->

- The file holds one component and what it depends on: its interfaces and data types.
- Each interface or type it shares with the project is the same in both; one that differs stops
  the import, and nothing is written.
- You know its budget for every memory the project checks strictly, and where it goes.

### The export it reads { #the-export-it-reads }

<!-- purpose: What a component export holds, and what the assistant's export_component answers -->

A component export, made by `swcraft generate arxml --component` or the assistant's
`export_component`, holds the component, the interfaces its ports reference and the data types they
reach — never a composition, a connector, a task, a sequence or a requirement — at the ARXML
version the manifest names, the same one the whole-model build writes. An unchanged model always
exports byte-identical files. The file names follow
[Exchanging with other tools](../cli.md#exchanging-with-other-tools). Through the assistant, the
answer's `output` is the export report, one per component and then the summary file, followed by
the AUTOSAR version; `export-summary.json` says which components were exported, and each file says
its own version. A component that cannot be exported — an unknown or ambiguous name, a reference
its closure cannot resolve, a failed generation — leaves no partial file; exporting every component, each
failure is one error entry, the others are still exported, the summary lists every component, and
the answer's `success` is false, as the command's exit code is non-zero.

## Names to resolve { #names-to-resolve }

<!-- purpose: The names this change needs, with an example of each and how to find yours -->

| Name | Example | How to find yours |
|---|---|---|
| The file | `imports/ExampleLoggerSWC.arxml` (*example*) | its path inside the project folder |
| The component it holds | `ExampleLoggerSWC` (*example*), port `rIn` | the import's report names it |
| The memory and the budget | `RAM0`, 512 bytes | `resource_report` lists the memories |
| The composition | `Root` | `read` with no address lists them |
| The port that feeds it | `pOut` of `ExampleSWC` | `find` with the component's name |

## Steps { #steps }

<!-- purpose: The three steps, by hand in the editor and through the assistant -->

### 1. Plan the import { #plan-the-import }

<!-- purpose: Reading the file and seeing what the import would create, reuse and skip -->

*In VS Code:* **Model → Import Component**, pick the file; its dry run's report opens with
**Write** and **Cancel**.

*On the command line:* the same dry run, which writes nothing: `swcraft import component` with
`--source` naming the file and `--dry-run`.

```sh
swcraft import component --source imports/ExampleLoggerSWC.arxml --project swcraft.yaml --dry-run
```

*Through the assistant:* take a `baseline` (`read` with no address answers one), then
`import_component`. It writes nothing: it answers the report and a ready `change_set` request.
The `baseline` is an *example*.

```json
{"tool": "import_component", "arguments": {"source": "imports/ExampleLoggerSWC.arxml", "baseline": "rev1:example-baseline"}}
```

### 2. Import it and wire it in { #import-it-and-wire-it-in }

<!-- purpose: The one change that creates the component with its budget, member and connector -->

*In VS Code:* **Write** — on the command line, the same command without `--dry-run`. Then add
`memory: {RAM0: 512}` to the new file under `model/swc/`, a member to `Root.yaml` and a connector
file; `check-model` reports the missing budget until you do.

*Through the assistant:* send the request's `baseline`, `intent` and `operations`, then the
companions after them: the budget, the member, the connector. Below, the component's value is
shortened to what this example's file holds; send the one `import_component` answered.

```json
{"tool": "change_set", "arguments": {
  "baseline": "rev1:example-baseline",
  "intent": "Import component 'ExampleLoggerSWC' from an ARXML export",
  "complete": true,
  "operations": [
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleLoggerSWC"}, "value": {"name": "ExampleLoggerSWC", "group": "components", "type": "application", "description": "Logs every value it receives.", "ports": [{"name": "rIn", "direction": "require", "interface": "IExample"}], "runnables": [{"name": "LogRun", "events": [{"type": "timing", "period": 0.01}], "reads": [{"port": "rIn", "element": "value"}]}], "handle_termination_and_restart": "no_support"}},
    {"operation": "create", "address": {"layer": "swc", "element": "ExampleLoggerSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]}, "value": 512},
    {"operation": "create", "address": {"layer": "compositions", "element": "Root", "path": [{"field": "members"}, {"member": "ExampleLoggerSWC"}]}, "value": {"name": "ExampleLoggerSWC", "type": "ExampleLoggerSWC"}},
    {"operation": "create", "address": {"layer": "connectors", "element": "Example_to_Logger"}, "value": {"name": "Example_to_Logger", "from": {"swc": "ExampleSWC", "port": "pOut"}, "to": {"swc": "ExampleLoggerSWC", "port": "rIn"}}}
  ]}}
```

A requirement the component realises travels in the same change, as a `create` of a member of its
`satisfies`: [Link requirements](link-requirements.md).

### 3. Review and approve { #review-and-approve }

<!-- purpose: Approving the proposed import in VS Code's review tab -->

*In VS Code:* nothing more to approve after **Write**; go on to the check.

*Through the assistant:* VS Code opens the review tab with the component, its member and its
connector. **Approve** applies them; **Reject** leaves the model as it was.

## What you get { #what-you-get }

<!-- purpose: What the model holds after the import, and what the answer says -->

`ExampleLoggerSWC` exists with a budget of 512 bytes on `RAM0`, stands in `Root` and receives
`IExample` from `ExampleSWC`; `IExample` and its types were reused, not copied. An approved change
answers `"status": "committed"` with a new `baseline`.

### What the plan decides { #what-the-plan-decides }

<!-- purpose: What the import reuses, replaces, keeps or refuses, and why its report says so -->

The command line's dry run, **Import Component**'s report and `import_component` decide the same
way; the assistant's plan writes nothing until its `change_set` is approved, and it never answers
with an empty change set, since a change set holds at least one operation.

**Shared definitions.** An identical shared interface or data type is reused silently. Equality
ignores its descriptions, which a reused definition never takes, and the fields the exported ARXML
cannot carry: a value type's unit, port order and the component's memory budget; a value type's
limits are written and compared. The component's own description, its ports' and its runnables'
are compared, an empty one in the file keeping the project's. A divergent shared definition always
stops the import, with no override — except a shared mapping set, which is extended with the maps
the file carries and the project's set lacks, and stops the import only on a map pairing one
application type with two implementation types.

**The component.** It is created when absent, a no-op when identical, and replaced only by an
update (`update: true`, `--update`) when divergent. An update carries the project's memory budget
and its requirement links over: `satisfies` on the component and on each port and runnable the
file keeps.

**References the project does not have.** A reference the component's behaviour makes is answered
first by the file's own declaration at that exact path, then by the project's interface of the kind
the reference names, or its mapping set, of the same name. When neither answers one, the plan still
writes the component's ports, description and resolved mapping sets, but without its `runnables`
and without each `data_type_mappings` entry found nowhere, and the report lists each missing
reference under *References the project does not have*. A port whose interface neither declares is
put on an external interface stub — the project's own stub for the same source path and kind when
it has one, else a new one — marked `(external interface stub)` in the report, and every
behavioural reference through such a port is a missing reference. A type only a mapping set found
nowhere would pair is reported by `check-model` until the set is declared. An update of a component
whose behaviour is withheld this way keeps the project's own runnables, with their events and
accesses, and its mapping sets, and writes the file's ports and description over them; it is
refused with a conflict naming the reference when the new ports or interfaces would break one the
kept behaviour makes.

**Read, write and call lists.** An update keeps the component's existing runnable read, write and
call lists whenever the file does not say whether it can express them: an ARXML with no access
point is written both by a tool that means *none* and by one that cannot say. A file written by a
current SWCraft says which it is — an `ACCESS-POINTS` marker reading `explicit`; any other value is
read as no marker at all, and the report names the value it found — and its lists are taken. The
word `explicit` says that the file can state which accesses a runnable has; it does not claim that
every access in the file is an explicit one, and a component whose runnables read and write
implicitly carries the same marker. For
any other file, `replace_access_lists: true` (`--replace-access-lists`) with the update takes them
anyway; without an update it is ignored entirely, and it decides what an update does with the
lists, never whether the update is allowed. The report names whichever decided it.

**Operation events and `concurrent`.** A file written by a current SWCraft carries an
`OPERATION-EVENTS` marker, and an update takes its operation events and concurrency, removing what
it does not declare. A file without the marker cannot say *none*: an update keeps the component's
own operation events and `concurrent` for each runnable the file says nothing about, while a
runnable that itself declares an operation event or `concurrent: true` is compared and replaced
like the rest of it. A marker with any other value is refused, nothing planned, and
`replace_access_lists` has no effect on any of this. The report names the regime and what was kept.

**Inter-runnable variables and exclusive areas.** A component's inter-runnable variables with its
runnables' variable accesses, and its exclusive areas with its runnables' `can_enter` and
`runs_inside`, are each decided for the whole component, again with no keyword. A file written by
a current SWCraft carries an `INTER-RUNNABLE-VARIABLES` and an `EXCLUSIVE-AREAS` marker, and an
update takes what it declares, removing what it does not. A file without the marker that states a
variable or an access (an area or an area reference) is the truth for all of them and replaces them
the same way; one that states none keeps the component's own, each runnable's lists by runnable
name. A marker with any other value is refused, nothing planned. Each regime and what was kept are
named in the rendered report.

**Data-received events.** A runnable's data-received events are decided for the whole component
too. A file written by a current SWCraft carries a `DATA-EVENTS` marker, and an update takes the
data-received events it declares, removing the ones it does not. A file without the marker that
states a data-received event on any runnable is the truth for every runnable's; one that states
none keeps the component's own, each in its place on the runnable of the same name. A marker with
any other value is refused, nothing planned, and the regime and what was kept are named in the
rendered report.

**Implicit access and asynchronous calls.** The implicit form of reads and writes (`by: implicit`),
and a call's synchrony (`asynchronous`, with its `poll`, `result` and `timeout`) together with the
runnables' `call_returned` events, are decided for the whole component on the same terms, by an
`IMPLICIT-ACCESS` and an `ASYNCHRONOUS-CALLS` marker. A file without the marker that states neither
keeps the component's own: `by: implicit` on each read or write the file lists with no form, the
synchrony on each call it still makes, and each `call_returned` event in its place on the runnable
of the same name. A marker with any other value is refused, nothing planned.

**Through the assistant.** `import_component` takes the `baseline` it is given into its request
without checking it; `change_set` refuses a stale one before anything is applied. A component the
import creates satisfies no requirement and needs no link to pass the checks; a link the user names
travels in the same change, and the change commits whole or not at all.

## How to check it { #how-to-check-it }

<!-- purpose: Confirming the component is wired in and the model passes its checks -->

*On the command line:* `swcraft check-model --project swcraft.yaml`.

*Through the assistant:* read the component back in its summary form.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc", "element": "ExampleLoggerSWC"}, "form": "summary"}}
```

## When it is refused { #when-it-is-refused }

<!-- purpose: The refusals an import meets most often, and what to do about each -->

| Code | What it means here | What to do |
|---|---|---|
| *the import's own* | the file is not there; a shared interface or type differs; the component exists and differs | correct the path; settle the difference first; `update: true` replaces the component, keeping its budget and links |
| [`consistency-violation`](refusals.md#consistency-violation) | the component has no budget for a strictly checked memory | add the budget's `create` |
| [`partial-change-set`](refusals.md#partial-change-set) | the budget or member comes before the component's `create` | put the companions after the request's operations |
| [`broken-reference`](refusals.md#broken-reference) | the composition, the feeding port or the memory does not resolve | correct the name the message points at |
| [`stale-baseline`](refusals.md#stale-baseline) | the model changed after the `baseline` was taken | plan the import again with a fresh one |
