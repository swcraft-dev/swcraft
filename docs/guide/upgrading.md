<!-- draft: not yet reviewed by the product's author -->

# Upgrading

<!-- purpose: Before you upgrade SWCraft: what a new release changes and the order to do it in -->

SWCraft ships as one archive holding five things — four packages, the specimen generator among
them, and a VS Code extension — and they move together. This page is about what a new one changes, what an existing project has
to do about it, release by release, and the order to do it in.

## The one rule { #the-one-rule }

<!-- purpose: Why the packages and the extension always carry one version and arrive together -->

**The five artefacts carry a single version, and they arrive together.** They are one
archive — `swcraft-<version>-<platform>.zip` — so there is no way to take the packages and
leave the extension behind, which used to be the one combination that installed quietly and
then behaved oddly. Upgrading is installing the next archive.

One of the four packages is `swcraft-specimens`, the generator of synthetic projects; the archive of
a new release replaces it with its own, like the rest.

Which version this copy of the guide describes is on its own `version` page, at `/version/`
in the built site.

## What the release notes are for { #read-the-release-note-first }

<!-- purpose: The release notes: the history of each release, beside the steps this page gives -->

Every release has a note, and the notes travel with this guide — the *Release notes* section of its
navigation, **[the release notes](release-notes/index.md)** — so they are there wherever the guide
is: in the editor's *Open Guide*, under `swcraft docs` and in the guide archive, with no network.

A note is the history of one release: what it added, what it did *not* change, and what it measured.
What an existing project must **do** is not left to the notes. Every step a release asks of a
project — a file to write, a key to rename, a directory to delete, a script to change — is on this
page, in *Upgrading by version*, written for the release you install. A note may describe an
intermediate spelling that a later release changed again; this page never does.

## What upgrading touches, and what it does not { #what-upgrading-touches-and-what-it-does-not }

<!-- purpose: What an upgrade touches in your project and your installation, and what it leaves alone -->

**Your model files are yours, and a release that needs them changed is listed below with its
steps.** Most releases need nothing: a model written for an older SWCraft loads, checks, generates
and exports under the newer one. Where a rule has tightened it has usually been about a *generated*
name colliding with something SWCraft now emits, and the fix is a rename.

A release that reorganises a layer is the other case. It is rare, and *Upgrading by version* writes
its edits out as a recipe. The hardware layer, the root composition, the data types (three times),
the partition, the budget's unit and the trace link have each been one.

The commands, the manifest and the generated output are treated the same way: a change to any of
them that a project or a script has to follow is a step below, never a footnote.

## Upgrading by version { #upgrading-by-version }

<!-- purpose: The steps each release asks of an existing project, written for the release you install -->

Find the release your project was last used with, then take every subsection **after** it, oldest
first, up to the installed release; then do *The procedure* below. A release that is not listed
asks nothing of a project beyond *The procedure* — the archive, the pin, *Onboard Project* and the
check.

Each subsection says what an older project spells, what the installed release reads instead, what
to do, and how to check it. The names a release removed stay written here, so that the word an
error or an old script prints can be searched for. Where a later release changed a step again, the
step says what to write **today**.

### 0.6.0 — the hardware layer { #upgrading-0-6-0 }

<!-- purpose: Upgrading from before 0.6.0: resources/ and cpu_core become the hardware/ layer -->

**Before:** the budgets lived in `model/resources/`, as `kind: cpu_core` and `kind: memory` files,
and a task named its core by number (`core: 0`). **Now:** the processor, its cores and its memories
are elements of `model/hardware/`, and a task names the **partition** it runs in, which names the
core (*0.15.0* below). Nothing converts the old files; the loader refuses a `cpu_core` tag it does
not know.

1. **Write the processor**: one `kind: processor` file in `model/hardware/` — exactly one is
   required.
2. **Turn each `kind: cpu_core` file into a `kind: core` file** whose `name` is what partitions will
   name. `threshold` and `enforcement` carry over unchanged. The old `core:` number has no
   successor; the vendor's core number may go in the optional, purely informational `hw_id`.
3. **Turn each old memory file into a memory of the processor.** `size`, `threshold` and
   `enforcement` carry over; `type` (`RAM`, `ROM`, `EEPROM` or `Flash`) and `accessible` are
   required. `accessible` is checked: list every core whose tasks run a component that budgets the
   memory.

   ```yaml
   # model/hardware/RAM0.yaml
   name: RAM0
   kind: memory
   type: RAM
   size: 262144
   accessible:
   - CORE0
   threshold: 0.8
   enforcement: strict
   ```

4. **Keep each component's `memory:` map** — `{memory name: bytes}` — and make every key the name
   of a declared memory.
5. **Give each task a partition** on the core it ran on, as *0.15.0* describes.
6. **Delete `model/resources/`.** Nothing reads it and nothing reports a leftover one.

**Check:** `swcraft check-model` after each step, until it exits 0. The finding that can remain
when every name resolves is `hardware.unreachable-memory`: a memory's `accessible` list does not
name the core a component's task runs on. Add the core, or move the task.

*History:* [the 0.6.0 release note](release-notes/0.6.0.md).

### 0.7.0 — one installation per machine { #upgrading-0-7-0 }

<!-- purpose: Upgrading from before 0.7.0: no project bootstrap.py or requirements file; relative output roots -->

**Before:** each project built its own environment — a `.venv/`, a `bootstrap.py`, a
`requirements-swcraft.txt`. **Now:** SWCraft is installed once per machine and a project installs
nothing; the release a project is built with is its `toolchain_version` pin (*The procedure*,
step 3). The model itself does not change.

1. **Delete what nothing reads any more**: `.venv/`, `bootstrap.py`, `requirements-swcraft.txt`,
   `clean_outputs.py`, and the `.vscode/mcp.json` the old template wrote, with the
   `.vscode/mcp.json.example` beside it — and the `.gitignore` entry that hid the first; `git rm --cached` whichever of them was committed. A
   leftover `.venv/` activated in a terminal shadows the installation for everything typed there.
2. **Write `dist_root` and `views_root` relative** to the project directory, if either is absolute:
   an absolute root is refused when the manifest is read, by every command and by the assistant.
3. **Drop the `python` pin from `.prototools`**, if there is one; the moon pin stays.
4. **Take the template's build graph** if you hand-edited `moon.yml`: every task runs
   `command: 'swcraft'`. Scaffolding into a copy with `swcraft init` and comparing the two is the
   quickest way to see the whole shape.

**Check:** `swcraft check-model` reads the project with no environment of its own.

### 0.8.0 — the names an initial value and an access point derive { #upgrading-0-8-0 }

<!-- purpose: Upgrading from before 0.8.0: derived constant and access point names that collide or overflow -->

The model's files do not change. What changes is that SWCraft derives names from two names you
wrote, and `swcraft check-model` reports a model whose derived names cannot be written:

- an interface name and one of its data element names together longer than 122 characters — the
  constant `{Interface}_{Element}_Init` would pass AUTOSAR's 128: shorten one of the two;
- two interface and element pairs deriving one constant name (`A_B` with `C`, `A` with `B_C`),
  `rule:interface.init-constant-name-collision`: rename one of them;
- a text `init` on a numeric type, `rule:interface.init-string-scalar`: write the number unquoted;
  an enumeration literal named `ON` keeps its quotes (`init: 'ON'`);
- a port name and a data element name a runnable reads or writes through it longer than 125
  characters together, or a port and an operation it calls longer than 122: shorten one;
- two entries of one runnable deriving one access point name, `rule:swc.access.name-collision`:
  rename one of the two.

`swcraft generate arxml` also refuses a model with a `rule:swc.access.*` finding that it used to
write anyway. **Check:** `swcraft check-model` until it exits 0.

### 0.9.0 — the root composition { #upgrading-0-9-0 }

<!-- purpose: Upgrading from before 0.9.0: containers/ go, one root composition holds the components -->

**Before:** components stood flat, an optional `model/containers/` grouped them, and the generated
ARXML invented a composition called `SwCraftComposition`. **Now:** one composition in
`model/compositions/` says `root: true` and names its members; a connector that names no
composition belongs to it.

1. **Write the root composition.** Its `name` is its `SHORT-NAME` in the ARXML, so it must be a legal
   AUTOSAR identifier and may be neither a component's name nor a component's generated
   `{component}_Implementation`. Every component goes in `members` as a `{name, type}` pair — both
   written, even when equal:

   ```yaml
   # model/compositions/Root.yaml
   name: Root
   title: Root composition
   root: true
   members:
     - name: ExampleSWC
       type: ExampleSWC
     - name: ExampleConsumerSWC
       type: ExampleConsumerSWC
   ```

2. **Make every connected component a member**: a connector's ends name component types, and the
   writer looks for their instances in the connector's composition — the root, when it names none.
3. **Delete `model/containers/`.** It is never read, and a leftover one is never reported. A script
   or a build task that ran `swcraft list containers` or `swcraft query containers` stops with a
   usage error, exit 2: the layer to name is `compositions`. A container that carried real grouping
   becomes a composition of its own, member of the root.
4. **Re-point anything downstream** — an RTE configuration, a hand-written reference, a script — that
   resolved a path through `/ComponentTypes/SwCraftComposition/…`: the path now runs through your
   root's name.

**Check:** `swcraft check-model` until it exits 0; a composition named like a component is
`rule:name.duplicate-component-type` (or `rule:name.composition-implementation-collision`). The
generated ARXML changes once: the composition's name and the paths built from it, and the members in
the order you declared them.

*History:* [the 0.9.0 release note](release-notes/0.9.0.md).

### 0.11.0 — base types, value types and `import model` { #upgrading-0-11-0 }

<!-- purpose: Upgrading from before 0.11.0: primitive and alias types, import rhapsody, old component files -->

**Before:** a data type was a `kind: primitive` or a `kind: alias`, and the whole-export import was
`swcraft import rhapsody`. **Now:** a `base_type` declares size, encoding and C type, a `value_type`
or a `reference_type` stands over it, and the import is `swcraft import model`. The files go into
the layer directories *0.14.0* introduced.

1. **Split each `kind: primitive` file in two.** The file you have keeps its `name` and its
   `description`, becomes `kind: value_type`, and moves to `model/data_types/implementation/`. A new
   `kind: base_type` file in `model/data_types/base/` carries `size`, `encoding` and
   `native_declaration` — always written: the primitive's own declaration, or, if it declared none,
   the primitive's name. Name the base type by the platform's lowercase spelling where the
   primitive's name is capitalised (`Float32` → `float32`), and otherwise append `_base`
   (`float32` → `float32_base`); point the value type at it with `base: float32_base`.
2. **Turn each `kind: alias` file into a `kind: value_type`** with its `base` re-pointed to the new
   base type — or into a `kind: reference_type` that keeps `base` on the implementation type it
   names, when you want a type reference rather than a value over the base type. A reference type
   carries no `unit`, `value_min` or `value_max`.
3. **Re-point nothing else.** Interfaces, records, arrays and `init` values keep the names they use,
   because the declared name stayed with the value type.
4. **Replace `swcraft import rhapsody` by `swcraft import model`** in every script, build task and CI
   step — the same flags, report and exit codes. The old word is gone: it stops with a usage error,
   exit 2.
5. **Export again any component file an earlier release wrote** before importing it: such a file
   carries the invented `{base type}_impl` twin and no data-types marker, and `swcraft import
   component` refuses it, naming the type. Export the component from the project that owns it with
   the installed release.
6. **On Linux, rename one of two files whose names differ only in case** in one layer directory:
   `rule:storage.filename-case-collision` reports the pair, and a checkout on macOS or Windows would
   keep only one of them.

**Check:** `swcraft check-model --project swcraft.yaml` until it exits 0. Every reference left
unfixed is named — a value type whose base is not a base type, a data element naming a base type, a
type that is not declared, a base type with no native declaration.

*History:* [the 0.11.0 release note](release-notes/0.11.0.md).

### 0.12.0 — the diagrams drawn by SWCraft { #upgrading-0-12-0 }

<!-- purpose: Upgrading from before 0.12.0: c4 output, generate c4, model/systems and the LikeC4 leftovers -->

**Before:** SWCraft wrote a LikeC4 document — `outputs.c4` in `swcraft.yaml`, `swcraft generate c4`,
`views/model.c4` and a `model/systems/` directory. **Now:** `swcraft generate diagrams` draws the
views as files in `views_root`.

1. **Delete the `c4:` line under `outputs:`** in `swcraft.yaml`. The model check reports a leftover
   one as `manifest.unknown-key`, and `check-model` and `validate` exit 1 until it is gone.
2. **Delete `model/systems/`.**
3. **Run `swcraft generate diagrams` wherever `swcraft generate c4` ran.** In a project with a build
   graph, the `generate-c4` task becomes `generate-diagrams`, with `views/` as its output, and the
   `build` task depends on it by that name. The `swcraft-generate-c4` command and the Python
   functions `generate_c4` and `generate_c4_cli` are gone; from Python, call
   `swcraft_tools.writer.generate_diagrams`.
4. **A script that matches the rule `rule:sequences.sequence.name.likec4-identifier`** matches
   `rule:sequences.sequence.name.identifier`.
5. **After a forced re-import**, delete `model/requirements/REQ-RHAPSODY-IMPORT.yaml` (and see
   *0.17.0* for the placeholder that replaced it).
6. **Delete `views/model.c4`**, and the `.likec4/` line of `.gitignore` if you like; neither is
   read.
7. **A script that checks `swcraft query --format json`'s `schemaVersion`** accepts the installed
   release's: `3` for a layer slice or a walk, `4` for a request's rows.

**Check:** `swcraft check-model`, then `swcraft generate diagrams`.

*History:* [the 0.12.0 release note](release-notes/0.12.0.md).

### 0.13.0 — physical types and mapping sets { #upgrading-0-13-0 }

<!-- purpose: Upgrading from before 0.13.0: an application type and a mapping set for composites and enums -->

**Before:** a record field or an array element could name an implementation type directly, and an
enumeration on a port needed no pairing; the exporter invented the physical view. **Now:** that
position takes an application type, paired with its implementation type in a mapping set that
every component speaking it names.

1. **Make sure `model/data_types/mapping_sets/` exists** — required, and it may stay empty
   (*0.14.0* creates it with the other layer directories).
2. **Give every composite member a physical type.** `swcraft check-model` names each record field
   or array element that names a value type or a reference type. For each one:
   1. declare an `application_type` in `model/data_types/application/` — a name is enough, and a
      `unit`, a `conversion` or a `range` you know reaches the generated file;
   2. point the field or the element at it (`type: Distance_M`, where it said `type: float32`);
   3. pair the two in a mapping set in `model/data_types/mapping_sets/`:

      ```yaml
      # model/data_types/mapping_sets/PhysicalTypes.yaml
      name: PhysicalTypes
      maps:
        - application: Distance_M
          implementation: float32
      ```

   4. name the set in `data_type_mappings` on every component whose ports speak the type.
3. **Pair every enumeration a port speaks** the same way — `application: GearPosition`,
   `implementation: uint8` — and name the set on every component that speaks it. The enumeration
   itself does not move.
4. **Rename by hand an element named for a device Windows reserves** — `CON`, `aux`, `Com3` and the
   others the check names: a new name inside its file, the file renamed to match, and every
   reference changed (`swcraft impact` lists them). The assistant refuses every change while such a
   file stands.
5. **A script that parses the import report** matches its headings and its counts, never whole
   lines.
6. **Saving in VS Code no longer regenerates the diagrams**: press **Diagrams**, or run `swcraft
   generate diagrams`.

**Check:** `swcraft check-model` until it is clean, then `swcraft validate`.

*History:* [the 0.13.0 release note](release-notes/0.13.0.md).

### 0.14.0 — the data type layers { #upgrading-0-14-0 }

<!-- purpose: Upgrading from before 0.14.0: seven data type directories, implementation_type, restart -->

**Before:** every data type was a file directly in `model/data_types/`, the mapping sets were in
`model/mapping_sets/`, and a data element or an argument named its type as `type:`. **Now:** the
data description lives in seven directories under `model/data_types/`, and a data element or an
argument says which side it names. Nothing converts a project on the old layout.

1. **Create the seven directories** (`swcraft init` scaffolds them for a new project):

   ```sh
   mkdir -p model/data_types/application model/data_types/implementation model/data_types/base \
            model/data_types/mapping_sets model/data_types/units model/data_types/compu_methods \
            model/data_types/constraints
   ```

2. **Move each file by its `kind`**, keeping its name and its content: `enum`, `record`, `array`
   and `application_type` into `application/`; `value_type`, `reference_type`,
   `implementation_record`, `implementation_array`, `pointer` and `external_type` into
   `implementation/`; `base_type` into `base/`; every file of `model/mapping_sets/` into
   `data_types/mapping_sets/`.
3. **Say which side each data element and argument names**: in every interface, replace `type:` by
   `implementation_type:` where it names an implementation type and by `application_type:` where it
   names an application type — exactly one of the two.
4. **Give every component that is not external a `handle_termination_and_restart`** —
   `no_support`, `can_be_terminated` or `can_be_terminated_and_restarted`. SWCraft never picks it
   for you.
5. **Run `swcraft check-model`, and fix what it names:**
   - two names in one component, behaviour, interface, record or composition that are equal
     ignoring case: rename all but one;
   - two elements of one package whose names differ only in case: add `package: <a sub-package>` to
     the file of one of them;
   - an application type with a `conversion` and no `unit` or `unit_ref`: declare its unit;
   - a declared type named `{X}_Range` beside a value type `X` that states `value_min` or
     `value_max`: rename the declared type;
   - a description or another text holding a character XML cannot carry: remove it from the file;
   - a runnable named, ignoring case, like an event its component generates: rename the runnable or
     state the event's name;
   - an external component whose `external_ref` starts with `/DataTypes`, `/PortInterfaces` or
     `/ComponentTypes`: give the path of the component in the file that really declares it.
6. **Commit the move on its own.** A revision on the old layout is refused like the working tree,
   so `swcraft diff` across that commit has nothing to compare on the old side.

A model still on the old layout is refused on every read with one sentence that names the
directories:

```text
The model keeps its elements in a retired layout: 3 element files in 'data_types', … they belong
in 'data_types/application', 'data_types/implementation' or 'data_types/base'. Move each file
into the directory of its layer; nothing converts a model on the old layout.
```

**A project imported from an AUTOSAR export** is upgraded by importing the export afresh: it writes
the new layout, gives each type the name it has in the export (the `{name}_impl` names an earlier
import invented are gone) and creates the shared units, conversions and constraints. Edits made to
the earlier import are not carried over; the project's own files move by the steps above.

**Component files.** A component file written by an earlier release may be refused — one reaching
an array-typed argument, or stating an enumeration's or an application type's initial value the old
way: export the component again with the installed release from the project that owns it.

**Scripts around the assistant's server.** The model's layers are addressed as `application_types`,
`implementation_types`, `base_types`, `mapping_sets`, `units`, `compu_methods` and
`data_constraints`; a request naming `data_types` is refused as an unknown layer. In the default
short form a missing `required`, `immutable` or `optional` key means `false`, and a reference that is
not set is left out rather than sent as `null`. A script that read a large component's ports in one
call pages them. A script that matched the finding `mapping-set.unknown-application` for a record or
an array paired with a value type matches `mapping-set.incompatible-composite-map`.

**Check:** `swcraft check-model` until it exits 0. The first build after this step differs
throughout — every data type moves into its layer's package — so commit that build's output on its
own if you keep it under version control.

*History:* [the 0.14.0 release note](release-notes/0.14.0.md).

### 0.15.0 — the partition { #upgrading-0-15-0 }

<!-- purpose: Upgrading from before 0.15.0: a task names its partition, not its core -->

**Before:** a task said `core: <core>`. **Now:** a task names the **partition** it runs in, and the
partition names the core. A task file that still says `core:` does not load; the error says
`partition` is a required field.

1. **Write one partition per core your tasks name**, into `model/hardware/`, with the level the
   components its tasks run share, or no level when they share none:

   ```yaml
   # model/hardware/PARTITION0.yaml
   name: PARTITION0
   kind: partition
   core: CORE0
   ```

2. **In every task file, replace `core: <core>` by `partition: <the partition on that core>`.**
3. **Name an occurrence where a type is placed twice**: a task entry or a sequence step that says
   `swc: <type>` for a type the compositions place more than once is refused, and the message lists
   the paths. Replace it by `instance: <path>`, one entry per occurrence you want run.
4. **Follow the imported external elements' names**: an element an import creates is named
   `<name>_ext<N>`, and anything you wrote yourself that names one by its old name is renamed to
   match.
5. **A script that calls the Python writer** passes `partition` where `create_task` took `core`, and
   calls `set_task_partition` where it called `set_task_core`, which is gone.
6. **A script around the assistant's server** reads a tool result's first text block, parsed as
   JSON where the tool answers a mapping, where it read `structuredContent`.

**Check:** `swcraft check-model`. On a project whose cores declare an `asil_capability`, a partition
left without a level is compared with nothing — rate the partition to keep the check the core used
to make.

*History:* [the 0.15.0 release note](release-notes/0.15.0.md).

### 0.16.0 — the budget's unit { #upgrading-0-16-0 }

<!-- purpose: Upgrading from before 0.16.0: wcet_us becomes wcet with its unit -->

**Before:** a task's budget was `wcet_us: 400`. **Now:** it is `wcet: 400us`, the same number with
its unit. A file still spelling `wcet_us` does not load and is never converted:

```text
'wcet_us' was renamed 'wcet' by contract 10.0.0 and now carries its unit: write 'wcet: 400us' for 'wcet_us: 400'
```

1. **Rename each `wcet_us: N` to `wcet: Nus`.** A runnable's budget is declared with its
   `wcet_type`.
2. **Commit the rename on its own**: `swcraft diff` refuses a revision committed before it, like any
   revision it cannot read.
3. **A request that relied on `exists` counting an empty description as a value** changes:
   `exists: true` on `description` no longer finds an element whose description is empty.
4. **Run *Onboard Project*** (*The procedure*, step 4) in a project the extension onboarded before
   this release: it replaces SWCraft's own development instructions in
   `.github/copilot-instructions.md` with the model project's, and writes the model guard. The guard
   runs the `swcraft` on your path, which must be the installed release.

A project that declares no budget needs nothing, and its ARXML is the same.

*History:* [the 0.16.0 release note](release-notes/0.16.0.md).

### 0.17.0 — the trace link in the element { #upgrading-0-17-0 }

<!-- purpose: Upgrading from before 0.17.0: a requirement's links move into satisfies on the elements -->

**Before:** a requirement listed the components that realize it under `links`, with a `status`.
**Now:** each element names what it satisfies, as `satisfies: [REQ-12@1]` — the requirement's id and
the version the link was confirmed for. A requirement file still carrying `links` or `status` does
not load and is never converted:

```text
'links' moved to the elements by contract 11.0.0: write 'satisfies: [REQ-12@N]' on the component, port, runnable, composition, interface, task or hardware element that realizes it
```

1. **For each requirement file**, add `<its id>@1` to the `satisfies` list of every element its
   `links` named; delete `links` and `status`; write `version: 1` and `source: architecture` — or
   `source: external` for a requirement a requirements tool owns, which the next import manages.
2. **Commit the move on its own**, as with every reorganisation.
3. **A project imported from an AUTOSAR export** deletes `model/requirements/REQ-ARXML-IMPORT.yaml`
   (or `REQ-RHAPSODY-IMPORT.yaml`), the placeholder the import used to write, and links its
   components to real requirements.
4. **The requirements output** is the file `outputs.needs` names — *From 0.21.0* has the step.

**Check:** `swcraft check-model`. Links to confirm and requirements nothing satisfies are warnings,
which build anyway; a pipeline that wants them to stop the build runs it with
`--warnings-as-errors`. A component that satisfies no requirement is not reported at all.

*History:* [the 0.17.0 release note](release-notes/0.17.0.md).

### 0.18.0 — trace decisions { #upgrading-0-18-0 }

<!-- purpose: Upgrading from before 0.18.0: the retired link warning and the trace decisions file -->

1. **A pipeline that counted the rule `cross-layer.swc-without-requirement-link`** stops counting it:
   the rule is gone, and `--warnings-as-errors` no longer stops a model whose only findings were
   components without a link. A requirement nothing satisfies is still a warning, and still refused
   under the flag.
2. **Once you apply trace links, commit `trace-decisions.yaml`** beside `swcraft.yaml` with the model
   files the apply changed: it records every decision, and a pair you rejected is not proposed again
   while the requirement stays at that version.

*History:* [the 0.18.0 release note](release-notes/0.18.0.md).

### 0.19.0 — the schema in the assistant's profile { #upgrading-0-19-0 }

<!-- purpose: Upgrading from before 0.19.0: onboard again, withdrawn checks, new default answer forms -->

1. **Run *Onboard Project* in every project.** The server's project-wide `discover` answers the
   `baseline` form by default, and the schema it no longer sends is in the profile: an old profile
   sends the agent to `discover` for a schema it does not get. A client of your own that reads the
   schema from `discover` asks for `form: "summary"`.
2. **A client that called `check_port_compatibility` or `check_connector_consistency`** meets an
   unknown-tool error: both tools are gone. Stage the connector instead (the refusal names both
   interfaces), or run `swcraft check-model` for the whole model.
3. **A client of your own that reads the answers** meets new default forms:
   - `model_diff` answers the summary; ask `form: "full"` for the text, which is refused rather than
     returned when it is over the answer budget;
   - `resource_report` answers structured data instead of the rendered text (`swcraft
     resource-report` is unchanged);
   - a `remove`'s `before` is the element's summary; `read` the element if you need all of it;
   - an `impact_of` `summary` with no `limit` over a walk of more than twenty rows answers a page
     and its `next` cursor; ask `form: "full"` for the whole walk;
   - `find` with more than three names answers sixteen candidates in all, and adds
     `existing_links_more` and `existing_links_read` when a requirement has more than sixteen
     links; `true` or `"2"` is no longer accepted as an integer for `read`'s `limit` or `find`'s
     `candidates`.

*History:* [the 0.19.0 release note](release-notes/0.19.0.md).

### 0.20.0 — Codex and the build report { #upgrading-0-20-0 }

<!-- purpose: Upgrading from before 0.20.0: Codex's hook and review, the fit verdicts, the build report task -->

1. **If you use Codex**, trust the model guard's hook once with `/hooks`, and set
   `SWCRAFT_REVIEW=panel` on the server you register for it, so its change sets reach the review tab
   ([The assistant](assistant.md) has the configuration). Make the edits of `swcraft.yaml` an upgrade
   asks for yourself: the guard does not let Codex edit it.
2. **A reader of `swcraft resource-report`'s JSON** that compared the headroom itself reads the two
   verdicts instead; one that read `fits` alone needs no change.
3. **A project with a build graph** takes the template's `validate-arxml` task to get the build
   report from `moon run :build`: it passes `--report`, depends on `generate-arxml`,
   `generate-diagrams` and `generate-needs`, lists `dist/build-report.md` among its outputs and runs
   with `cache: false`. A project that keeps its old task builds as before and writes no report.

*History:* [the 0.20.0 release note](release-notes/0.20.0.md).

### 0.21.0 — the review waits for its answer { #upgrading-0-21-0 }

<!-- purpose: Upgrading from before 0.21.0: Codex and Claude Code timeouts, the one export tool -->

1. **If you use Codex**, set `tool_timeout_sec` in its SWCraft stanza to `86400` (it was `660`;
   [The assistant](assistant.md) has the stanza), and on its first prompt for a SWCraft tool choose
   **Always allow** or **Allow for this session**.
2. **If you use Claude Code**, add `"env": {"CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0"}` to
   `$HOME/.claude/settings.json` or the project's `.claude/settings.local.json`, so the review's
   call stays in the foreground.
3. **A client or a script that called `export_all_components`** — which is gone — calls
   `export_component` without `swc_name` to export every component.
4. **Keep the extension and the archive on one release**, which installing the archive does: the
   review record's format moved, and an extension and a server of different releases refuse each
   other's questions.

*History:* [the 0.21.0 release note](release-notes/0.21.0.md).

### 0.22.0 — `needs.json` and the unknown key { #upgrading-from-0-21-0 }

<!-- purpose: Upgrading from before 0.22.0: outputs.needs, generate needs, unknown keys -->

**Before:** `swcraft generate rst` wrote Sphinx sources into the folder `outputs.rst` named, and an
undeclared key in a model file or in `swcraft.yaml` was dropped without a word. **Now:**
`swcraft generate needs` writes one `needs.json`, and an undeclared key is an error.

1. **Rename `outputs.rst`** in `swcraft.yaml`: under `outputs:`, write `needs: dist/needs.json`
   where it said `rst: …` (any older value, a `.rst` file or the `dist/needs` folder), or delete the
   line to take that default. A leftover `outputs.rst` is a `check-model` and `validate` error,
   `manifest.unknown-key`.
2. **Replace `swcraft generate rst` by `swcraft generate needs`** in every script; the old command
   stops with a usage error, and the `swcraft-generate-rst` command is gone. In a project with a
   build graph, replace the `generate-rst` task of `moon.yml` by the template's `generate-needs`
   task (command `swcraft generate needs`, output `dist/needs.json`), and name `generate-needs`
   wherever `generate-rst` appears in a `deps` list — the `validate-arxml` and `build` tasks — or the
   build fails at that task.
3. **Fix each new error `swcraft check-model` names:** a `model.unknown-key` — a key no field of its
   kind declares, such as `derived___from:` — is renamed to the field it was meant to be, or
   deleted; a `manifest.unknown-key` in `swcraft.yaml` is renamed or deleted (a leftover `c4:` line
   under `outputs:` included); a `safety.measure-blank` — a `measures` entry with no text — gets the
   mechanism written in, or the entry is deleted and the finding it answered comes back. A pipeline
   under `--warnings-as-errors` also gives every variable a runnable reads an `init`.
4. **The requirements import** reads the team's own `needs.json`. Pass `--baseline` when the file
   names no `current_version`; when the team's file uses need types or field names of its own, write
   a mapping file and pass it with `--mapping` (VS Code asks for it). A delivery is the complete
   set: a requirement the new delivery no longer holds is removed, and the links that named it are
   reported. The requirements team's side — its `conf.py` lines and the keys its import ignores — is
   in [Publishing the architecture to the requirements
   team](cli.md#publishing-the-architecture-to-the-requirements-team). A link to an id holding `.`
   or `[` is refused.
5. **A pending trace-link proposals file**: run the search again before applying it.
6. **Exchange component files only between projects on the installed release**: an earlier release
   refuses every component file this one writes, so upgrade both sides. A model imported earlier
   gains its variables, exclusive areas and `data` events when it is imported again.
7. **Scripts:** one that reads `swcraft import component`'s exit status treats `3` as the refusal
   `--update` resolves; one that matched the cohesion reason text matches `one port or variable:
   every runnable that uses one uses only it`; and an experiment configuration whose `output` is
   refused — a directory holding a report no earlier run wrote, or a path inside `.git` — points at
   another directory.

*History:* [its release note](release-notes/0.22.0.md).

### 0.23.0 — Codex, Claude Code and Kiro { #upgrading-from-0-22-x }

<!-- purpose: Upgrading from before 0.23.0: Codex, Claude Code and Kiro files, guard files, outputs.needs -->

**Before:** Codex and Claude Code were given SWCraft's server by hand, with the project's absolute
path, Claude Code ran with no model guard, and Kiro had no files at all. **Now:** the project
carries their files, and all three are guarded.

1. **Run Onboard Project again and choose the clients you use** — Copilot, Codex, Claude Code, Kiro,
   or several (or, for a project made with `swcraft init --integration moon`, copy the chosen
   clients' files from a fresh scaffold made with `--client`). Only the chosen clients' files are
   written: for Codex `.codex/config.toml` and `AGENTS.md` (its `.codex/hooks.json` you have
   already), for Claude Code `.mcp.json`, `.claude/settings.json`, `AGENTS.md` and `CLAUDE.md`, for
   Kiro `.kiro/settings/mcp.json`, `.kiro/steering/swcraft.md` and `.kiro/hooks/swcraft-guard.json`
   ([In Kiro](kiro.md#onboard-project-in-kiro)), each written where the project has none and
   replaced where it is still SWCraft's own. Run it even for a
   client whose files you already have: every client's instructions — Copilot's assistant profile
   included — now tell the assistant never to propose switching off, editing or getting round the
   model guard, and the opening lines of `AGENTS.md` name Kiro's guarded folders. A file of that name you wrote yourself is kept and named in the onboarding
   output: add SWCraft's part to it by hand, as [Codex and Claude Code in VS
   Code](vscode.md#codex-and-claude-code-in-vs-code) says. With Copilot and one of the other two,
   the project's `.vscode/settings.json` needs `"chat.useAgentsMdFile": false` and
   `"chat.useClaudeMdFile": false`; an existing settings file is never edited, so the onboarding
   output names the two — add them by hand ([Turning a folder into a
   project](vscode.md#turning-a-folder-into-a-project) says why). Commit the new files with the
   project.
2. **Your `.codex/hooks.json` stays.** A project from 0.22.x has Codex's model guard,
   `.codex/hooks.json`, whether you use Codex or not; nothing removes it, and choosing the clients
   does not either. It only guards Codex, so if you do not use Codex, delete `.codex/hooks.json` —
   and the `.codex` folder, if nothing else is in it — and commit the change. If you do, keep it.
3. **If you gave Codex or Claude Code SWCraft's server by hand**, remove that line: for Codex a
   `[mcp_servers.swcraft]` table in `$HOME/.codex/config.toml`, for Claude Code a `swcraft` server
   you added at its local scope, which would win over the project's file. A user-scope one does not,
   but it is left over all the same.
4. **Trust the project once in each client** you use: Claude Code asks to trust the folder; Codex
   asks for the project and lists the hook under **Review hooks**; in Kiro choose **Trust**, then
   run **Developer: Reload Window** once ([In
   Kiro](kiro.md#trust-the-folder-then-reload-the-window-once)).
5. **Put this release's `swcraft` first on your path**, then quit VS Code and start it again from
   the Dock or Finder, so that the chats and the guard hooks find it; an older one blocks every file,
   search and shell tool through Claude Code's hook, every shell command and edit through Codex's,
   and every file and shell tool through Kiro's. Do the same for Kiro if you use it.
6. **Edit `.claude/`, `.mcp.json`, `.github/hooks/`, `.kiro/hooks/`, `.kiro/settings/` and
   `.kiro/agents/` yourself.** Codex's guard now also denies its edits of these, as Claude Code's and
   Kiro's do — and of the `.github` and `.kiro` folders that hold some of them, so a command removing
   `.github` is denied too — so no assistant can switch off another's guard.
7. **A trace-link proposals file with decisions not yet applied**: the search now compares an
   element without its fields left at their defaults, so every proposal you decided and did not
   apply reports itself stale once, *changed since the search*. Run the search again and decide
   those proposals anew. Undecided proposals, confirmed links and applied ones are not affected.
8. **If `outputs.needs` points at or below `dist/test-tables/`** (in your manifest's `dist_root`),
   move it: `swcraft generate test-tables` writes its tables into that folder, so every command that
   reads the project refuses such an `outputs.needs`. Write another name under `outputs:`, or delete
   the line to take the default, `dist/needs.json`. Check it with `swcraft check-model`: the
   project is read without that refusal ([Test tables](test-tables.md) has the folder).

*History:* [its release note](release-notes/0.23.0.md).

### From 0.23.0 — the `.env` file and the scripts { #upgrading-from-0-23-0 }

<!-- purpose: Upgrading a project from 0.23.0: .env search scope and .gitignore, Kiro steering, scripts -->

**Before:** `swcraft` read a `.env` from the folder it ran in and from every folder above it, then
from SWCraft's installation folder and every folder above that, an installed SWCraft also looked
for the AUTOSAR schema in a `schemas` folder of its installation, and a new project's `.gitignore`
did not ignore `.env`. **Now:** a `.env` is read only from the folder you run `swcraft` from and the
folders between it and your project's folder, the one holding `swcraft.yaml`, and a new project
ignores `.env`.

1. **Add a `.env` line to your project's `.gitignore`**, if it has none, and commit it, so a path on
   your machine or a key in your `.env` stays out of the repository.
2. **If a setting lived in a `.env` above your project's folder, or in or above SWCraft's
   installation folder** — `SWCRAFT_AUTOSAR_XSD_PATH`, or a key — it is no longer read. Set it in
   your environment, or move a setting that is not a secret into a `.env` in your project's folder.
   Check it with `swcraft validate`: while the schema is not found, the run says *XSD validation is
   skipped*.
3. **If you kept the AUTOSAR schema in a `schemas` folder of SWCraft's installation** and did not
   set `SWCRAFT_AUTOSAR_XSD_PATH`, set it now to the schema file, in your environment or in a `.env`
   in your project's folder: SWCraft no longer looks in its installation for a schema, so without
   the setting `swcraft validate` checks the ARXML by its structure alone and says *XSD validation is
   skipped*.
4. **If you use Kiro, run Onboard Project again**, so that `.kiro/steering/swcraft.md` sends a
   question about SWCraft to SWCraft's `guide` first ([In Kiro](kiro.md#onboard-project-in-kiro)).
   Commit the rewritten file. A steering file whose first line no longer says Onboard Project wrote
   it is yours: Onboard Project leaves it and names it, so add the sentence by hand, from the table
   on the *In Kiro* page.
5. **Scripts:** one that passed `swcraft query` `--form json` to mean `--format json`, or a shorter
   abbreviation (`--f`, `--fo`, `--for`), spells `--format` in full, since `--form` is an option of
   its own now and those spellings stop with a usage error; one that matched the end of a
   `hardware.missing-budget` message matches the new sentence, which names the field, the value's
   form and an example. The code, severity and address are unchanged. One that matched the end of a
   refusal or a finding naming a reference to SWCraft's own design records in parentheses matches
   the message without it. One that read the model folder from the first line `swcraft
   resource-report` prints reads the project's name there now. One that ran `carchetype`, the
   specimen generator, runs `swcraft-specimens`, the same command under its new name.

*History:* [its release note](release-notes/0.24.0.md).

## The procedure { #the-procedure }

<!-- purpose: Upgrading step by step: steps by version, installer, the toolchain_version pin, editor assets, check -->

1. **Take the steps of every release after yours** in *Upgrading by version* above, oldest first,
   up to the release you install. A release with no subsection asks nothing more than the steps
   below.
2. **Run the new archive's installer** — the same file, the same double-click as the first
   install — and open a new terminal. The new release is installed beside the one you had, the
   `current` link moves to it, the extension is handed to VS Code, and only then is the old
   release removed. Nothing is deleted before the replacement works, which is what makes an
   interrupted upgrade leave you with a working SWCraft rather than none. The `PATH` entry itself
   does not change between releases — the `current` link does — so a shell profile line added once
   keeps working.

   Close VS Code first if you can. On Windows a directory the editor still has open cannot be
   removed, and the installer says so in one sentence and carries on: the new release is
   already the current one, and the leftover directory goes on the next run. It is untidiness,
   not a failed upgrade.
3. **Raise the project's pin, if it has one.** A project may name the release it is built with,
   as `toolchain_version` in its `swcraft.yaml`. When that name and the installed release
   disagree, every `swcraft` command that writes or gates refuses and says both numbers; the read-only
   ones report it and answer anyway. Editing that one line is the whole of the upgrade on the
   project's side for most releases — and, in a project with a build graph, it is what makes the
   next build regenerate instead of handing you the previous release's outputs from its cache.

   A project without the field pins nothing and is upgraded by upgrading the machine.
4. **Refresh the project's editor assets.** Run **SWCraft: Onboard Project**
   (`swcraft.onboardProject`) once: it brings the assistant profile, the Copilot instructions
   and the model guard (`.github/hooks/swcraft.json`) up to date. It overwrites all three, so
   keep a copy of anything you changed in them. It also writes Codex's model guard,
   `.codex/hooks.json`, which a project set up earlier does not have: running this command is how
   it gets one. A `.codex/hooks.json` you already have that is not SWCraft's is left as it is, and
   the command says so — add the SWCraft guard's entry to it by hand. If you use Codex, trust the
   new hook once with `/hooks`, and make the edits of `swcraft.yaml` this upgrade asks for yourself:
   the guard does not let Codex edit it
   ([The model guard under Codex](vscode.md#the-model-guard-under-codex)). The chat configuration itself needs nothing — the extension registers SWCraft's
   server with the editor rather than writing a file into your project. If an assistant
   reports changes which never appear in your files, [The assistant](assistant.md) explains
   why that is fail-closed rather than broken.
5. **Check the model** with `swcraft check-model` before regenerating anything, then **run the
   gate**, `swcraft validate`, and compare the output with what you had:

```sh
swcraft check-model --project swcraft.yaml
swcraft validate --project swcraft.yaml
```

Unless a step above says the generated ARXML changes, it should not have. A generated file
that is byte-identical after an upgrade is the strongest confirmation available that the
upgrade did what it said.

## If the last step fails { #if-the-last-step-fails }

<!-- purpose: When the last step of an upgrade fails: what to try before assuming the worst -->

The message names the element and the reason, which is usually enough on its own. Two things
to try before assuming the worst:

- **Look up the finding.** [The checks](reference/rules.md) lists every check and what it
  requires. A check that has tightened is a check with an entry there.
- **Search this page for the finding's name or the key it names.** A rule that changed
  deliberately is in *Upgrading by version* with the fix beside it; a rule that changed by accident
  is a bug worth reporting.

A run that fails a model check writes nothing of its own, so there is nothing new to clean up —
the worst you are left with is whatever the previous version generated, still sitting where it
was. A run that gets as far as the AUTOSAR file and fails the schema check leaves that file
behind instead; it is the refused one, not a usable output. Either way: fix the model, or
install the previous version back, and try again.

## Going back { #going-back }

<!-- purpose: Going back (a downgrade) to the previous release by running its archive's installer -->

**Rollback is the previous archive.** Keep the one you came from and run its installer: it
installs that release, points `current` back at it and hands VS Code its own extension, exactly
as the newer one did. There is no rollback command and no version to name on a command line —
the archive *is* the version. Lower the project's `toolchain_version` back with it, if it has
one.

Whether the *model* comes back with it is the question to ask first. Where the release needed
no model change, the files you fixed forward are still readable by the version you came from;
where it reorganised a layer, they are not, and going back means going back to the model you
had as well — across the release that moved the data types under `model/data_types/`, that means
the files back in their old directories and every `implementation_type:` back to `type:`, which
is what committing the move on its own makes a single revert. *Upgrading by version* tells you
which of the two you are in: a release whose subsection changes a model file is one you cannot
go back across without the model. The pin works forward only — a release older than the one
that introduced it ignores the field — so go back to a commit, not to a regenerated output.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next: the checks, the review behaviour and a fresh project -->

| If you want to | Read |
|---|---|
| know what the checks require | [The checks](reference/rules.md) |
| understand the fail-closed review behaviour | [The assistant](assistant.md) |
| set a fresh project up instead | [Getting started](getting-started.md) |
| read the history of a release | [The release notes](release-notes/index.md) |
