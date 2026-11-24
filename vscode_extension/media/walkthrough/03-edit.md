# Edit a layer file

The model is plain YAML: **one element per file**, named after the element's `name` (for
requirements, its `id`) — and when another element's file already holds that name, compared
without regard to case, the file takes a `~1` instead. The element is always the `name` inside
the file. The layers live under the manifest's `model_root`:

```
swc/{group}/  interfaces/  connectors/
data_types/application/  data_types/implementation/  data_types/base/
data_types/mapping_sets/  data_types/units/  data_types/compu_methods/  data_types/constraints/
requirements/  compositions/  tasks/  sequences/  hardware/
```

Open any file under one of them — say a file in `tasks/` — and start typing.

## The schemas are already wired up

SWCraft ships a generated JSON Schema per layer and associates each one by **path**, not by
model root: `**/tasks/*.yaml`, `**/interfaces/*.yaml`, `**/swc/**/*.yaml`,
`**/data_types/base/*.yaml` — a layer under `data_types/` by both of its directory levels — and so
on. A project that renamed `model_root` still gets them.

The schemas are read by the **Red Hat YAML** extension, which the SWCraft pack installs. Without
it the files open as plain YAML and nothing else changes: no completion, no hover, no diagnostics,
and every SWCraft command, the CLI and `swcraft validate` behave exactly the same.

## What you actually get

| Where | What appears |
|---|---|
| Hover on a key | the schema title, the field's description, and — for an enumerated field — its allowed values |
| Completing a key | the field name, its description, and its **default** pre-filled in the inserted text (`period: 0.01`) |
| Completing a value | the members of an enumeration |
| An empty file | every field of that layer, plus one whole-element snippet that fills in the required keys |
| A mistake | a diagnostic that names the offending property — an unknown key, a wrong type, a value outside an enumeration, a missing required key |

A field's `default` reaches you through **completion**, not hover; the description reaches both.

## One thing worth knowing

`tasks/*.yaml` also matches a pattern in the public JSON Schema Store — the *Ansible Tasks File*
schema. SWCraft's association wins over the catalogue, so a task file is checked against SWCraft's
schema and nothing else. You do not have to turn `yaml.schemaStore.enable` off, and onboarding
does not touch that setting.

## Save, then check it for real

The editor now tells you about the file in front of you. It cannot tell you about the model. That
is the next step.

When you run the model check — **Check Model**, or **Build** — its findings also appear in the
**Problems** panel at the line each names, and a requirement link that needs its version confirmed
or stated offers a quick fix. The fix changes the open file only when you pick it; you save.
