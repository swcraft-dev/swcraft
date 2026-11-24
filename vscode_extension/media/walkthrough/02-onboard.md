# Onboard a project

**Setup → Onboard Project** (or `SWCraft: Onboard Project` in the Command Palette) opens a folder
picker. Pick the folder you want to be a SWCraft project — an empty one, or an existing repository.

What it then does, in order:

1. **A preflight, before anything is written.** It runs the installed `swcraft` — the one every
   other command in this window spawns — and checks that it starts. If it does not, onboarding
   stops and says so rather than leaving a half-configured folder behind. The repair it names is
   **SWCraft: Install Toolchain**.
2. **`swcraft init`, but only if the folder has no `swcraft.yaml` yet.** An existing manifest is
   never overwritten.
3. **The `swcraft-architect` agent profile and the Copilot instructions** are copied in — the
   model project's own pair, the files `swcraft init` writes — and an existing copy of either is
   replaced with this release's. A project that has no profile is also offered it by a notice when
   the window opens, which writes the profile and adds the instructions only where there are none.

**Nothing configures the MCP server**, and there is nothing to configure: the extension registers
it with VS Code itself for any open folder whose root holds a `swcraft.yaml`. Onboarding is what
puts the *agent* in place; the server is already there.

## What `swcraft init` leaves behind

A `swcraft.yaml` at the root, and one directory under `model/` per declared model layer — fifteen,
seven of them nested under `data_types/`:

```
swc/  interfaces/  connectors/
data_types/application/  data_types/implementation/  data_types/base/
data_types/mapping_sets/  data_types/units/  data_types/compu_methods/  data_types/constraints/
requirements/  compositions/  tasks/  sequences/  hardware/
```

Every one of them may stay empty until you need it.

Three of those directories get a file. `hardware/CPU.yaml` is the one processor every project
declares and `hardware/CORE0.yaml` is one core with an advisory, uncapped budget;
`compositions/Root.yaml` is the empty root composition, the top of the hierarchy the exported ARXML
describes. They are there so a fresh project passes the `check-model` gate this walkthrough goes on
to run, and two of the three are what makes it pass: delete `CORE0.yaml` and the gate still exits 0,
delete `CPU.yaml` or `Root.yaml` and it does not. Replace the placeholder text with the real part
and the real budget, add a `kind: memory` element per memory you want components budgeted against,
and add members to the root composition as you add components. No memory is scaffolded: a memory's
`size` is a hardware fact that cannot be invented.

## What `swcraft.yaml` is

It is the project manifest: the one file that says where everything lives. Every path in it is
relative to the directory holding the file, and no path may escape the project root.

```yaml
name: swcraft-basic-example
schema_version: "1.0"
model_root: model            # where the YAML model layers live
dist_root: dist              # generated build outputs; cleaned by every build
views_root: views            # generated diagram pages, opened by Diagrams; cleaned by every build
outputs:
  arxml: dist/model.arxml
  needs: dist/needs.json
```

`schema_version` is the version of *this manifest format*. The AUTOSAR release the generated ARXML
targets is a separate field, `arxml_version`, and it defaults to `4.2.2` when absent.

`swcraft.yaml` is also how the extension recognises a project at all: it activates on a
`swcraft.yaml` at the workspace root, and the top line of the SWCraft view reports what it found.
It gets the same schema-backed completion and hover the model layers do — see the next step.
