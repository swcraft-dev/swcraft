# Build — the gate

**Model → Build** (or `SWCraft: Build`) runs, one after another, what `moon run :build` runs in a
scaffolded project: `swcraft clean-outputs`, `check-model`, `generate arxml`, `generate diagrams` and
`generate needs`, each with `--project swcraft.yaml` — and it ends with `swcraft validate`, run with `--report`. The
first step that fails ends the run. `swcraft validate` loads the whole model, generates the ARXML, and checks that
ARXML against the AUTOSAR XSD — or, when no licensed schema is configured on the machine,
structurally. Every step's own text goes verbatim into the **SWCraft** output channel — the model
check's findings go to the **Problems** panel instead, the channel ending its step with one summary
line — and the tree
item reports `built 12:39` or `failed 12:41`, with the failing step named in the warning.

Then Build shows you where to look. When `swcraft validate` ran — the ARXML passed its check or
failed it — the build report it wrote, `dist/build-report.md`, opens in the Markdown preview: the
verdict and which check gave it, what was built, the warnings, the cores and memories, and the ARXML
elements changed against the previous build's `dist/model_old.arxml`. Nothing opens, with one line
in the output channel saying so, when the step says the report was not written or that its path holds
a control character, when the path it prints lies outside the workspace folder or the file is no
longer on disk, or when the Markdown preview itself cannot open. When the model check stopped
the run with an error, no report opens and the **Problems** panel takes the focus instead. Warnings
alone leave the focus where it is. The report is a summary for you to read; the exit code of
`swcraft validate` stays the verdict.

## Read this part twice

**The editor's diagnostics — the red squiggles in a YAML file — are usability assistance.
`swcraft validate` is the authoritative gate.**

They are not two views of the same answer. A file with no squiggles in it can still be refused by
`swcraft validate`, and that is normal, not a bug in either one.

The entries marked `swcraft` in the **Problems** panel are different in kind: the SWCraft entries
are the model check's findings, placed — what the model check reported at the last **Check Model** or
**Build**, put at the line each names, red for an error and yellow for a warning. **Check Model**
takes you to the Problems panel when it finishes, and the status bar counts the same entries
(`SWCraft: 0 errors · 35 warnings`). They are still not the gate. They describe the saved model at that run, a save does not refresh them, and an empty
panel is not a passed `swcraft validate` — nor is `0 errors` in the status bar. A JSON Schema sees exactly one
document at a time; almost everything that makes an architecture model correct is a statement
about *more than one* document, or about a value rather than a type.

So the editor cannot see, and `swcraft validate` does:

- **cross-file references** — a port's `interface`, a data element's `implementation_type` or
  `application_type`, a value type's `base`, a task's `runnables[].swc`, a sequence's `task`, an
  element's `satisfies` and a requirement's `derived_from`;
- **name uniqueness**, both within a file (two ports of one name) and across files (two SWCs of
  one name, in one directory or across `swc/` groups);
- **the AUTOSAR Identifier rule** — `[a-zA-Z][a-zA-Z0-9_]*`, at most 128 characters — on every
  name that is actually emitted, *including* the generated forms (`_Implementation`,
  `_InternalBehavior`, `_CompuMethod`, `_Unit`, `_Compu`, `_Range`, `TMT_`, `IT_`, `OIT_`, the
  `{Interface}_{Element}_Init` constant behind a data element's initial value, and the `R_`, `W_`
  and `Call_` access points behind a runnable's `reads`, `writes` and `calls`) that lengthen a name
  you declared legally — five of them built from two names at once, so an interface and each of
  its data elements share 122 characters between them, a port and each member a runnable names
  through it share 125 for a read or a write and 122 for a call, and a port and each operation an
  operation event names share 123. A form is charged only where SWCraft generates it: where you
  state a component's `behavior_name`, an event's `name` or a read's, write's or call's `name`,
  that name is written instead and checked as a name you declared, on its own;
- **the writer's reserved names** — a declared name that collides with something another element's
  generation produces;
- **cross-layer consistency** — every port interface declared, every task `core` a declared
  `kind: core` hardware element, every SWC `memory` key a declared `kind: memory` one, and every
  SWC a consumer of every declared memory;
- **value rules** — `period > 0`, `size > 0`, a non-blank name, and the refusal of `.nan` / `.inf`
  in any float field;
- **resource budgets** — CPU utilisation against a core's threshold, consumption against a memory's;
- **model rules that span two files** — a `{swc, runnable}` pair mapped to at most one task, a
  sequence's `steps` an ordered subsequence of its task's `runnables`;
- **the manifest's path rules** — nothing escaping the project root, no generated path overlapping
  `model_root`.

## The red marker on the generated `.arxml`

Open `dist/model.arxml` with Red Hat XML installed and you may see
`cvc-elt.1.a: Cannot find the declaration of element 'AUTOSAR'`. **Nothing is wrong with the
file.** The XML server reads the document's own `xsi:schemaLocation`, looks for
`AUTOSAR_4-3-0.xsd` next to it, and reports what it could not find as an error on the document.
The AUTOSAR schemas are licensed and SWCraft ships none, so that file is not there.

It is the same rule as above, seen from the other side: an editor marker is not the gate, in
either direction. `swcraft init` writes a `.vscode/settings.json` that turns the check off for
`.arxml` and only for `.arxml`:

```json
"xml.validation.filters": [
  { "pattern": "**/*.arxml", "noGrammar": "ignore", "schema": { "enabled": "never" } }
]
```

An older project has no such file — add it, or delete the entry if you are licensed to hold the
schemas and would rather bind them yourself with `xml.fileAssociations`. Either way
**`swcraft validate` is the authoritative gate**, and it is the one that checks against the XSD
when a licensed schema is configured on the machine.

## Two neighbours that are not gates

- `swcraft check-model` answers the model half of the same question and **writes nothing** — it is
  what a build graph runs first, because `validate` would rewrite the ARXML the next build step is
  about to check.
- **Impact of This Element** and **Resource Report** always exit successfully on a run that completed, *even when
  the resource report is over budget*. They are reports: neither touches the model, and the one
  file either writes is **Resource Report**'s own, under `dist/`. Do not read a clean
  exit from them as an answer about model validity; run Build, or `swcraft validate`, for that.
  The same holds for the **Resources** view: its red and green rows are the report's statuses, not
  a check that passed or failed.

## And when it fails

A failed build leaves **nothing stale**. The ARXML writer writes nothing until the whole document
has been built and checked in memory, and every build's first step removes the previous artifacts —
so a `dist/model.arxml` from an earlier run can never survive a failed one and be mistaken for a
fresh one.

**Build** keeps that order: its first step removes the previous outputs, and the first step that
fails ends the run, so nothing after it is generated. `swcraft validate` is the one step that
checks a file after writing it, so when it is the step that fails, the ARXML it just generated is
still on disk — fresh, and refused — and so is the build report beside it, whose verdict says
*Validation failed*. Read the exit code, not the directory.
