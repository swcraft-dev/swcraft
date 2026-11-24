# Differ goldens — the review-surface oracle

Two files, captured from `swcraft diff`'s two renderers **before the differ gained address paths
and member identities**, over the deep fixture pair `DEEP_OLD` / `DEEP_NEW` in
`packages/tools/tests/test_differ.py`:

| Golden | Produced by |
|---|---|
| `deep-pair.txt` | `format_diff(diff) + "\n"` — byte for byte what `swcraft diff` writes |
| `deep-pair.json` | `format_diff_json(diff)` — byte for byte what `swcraft diff --format json` writes |

That change carries an address path down the differ's existing walk and gives every collection
rule a member identity in the protocol's vocabulary. Both are **additive**: they add reachable
structure to `ModelDiff`, and they change nothing a human or a script is shown. These two files
are the statement of that, and
`test_differ.py::test_the_rendered_text_diff_is_byte_identical_to_the_golden` and
`::test_the_rendered_json_diff_is_byte_identical_to_the_golden` assert it — the differ is the
object a reviewer approves a change set on, so an extension of it that moved one rendered byte
would be changing what was reviewed, not what is reachable.

The pair is deliberately deep rather than minimal: it exercises a `.name`-keyed member, a
`{swc, runnable}` composite member, a `whole_value` list member (`events`, `links`), a mapping
entry added, removed and changed, a pure reorder, and two three-level nestings
(`runnables -> events`, `operations -> arguments`).

**The hardware layer moved exactly one byte-range in one of them, and the reason is recorded here rather
than in a commit message.** The change replaced the `resources` layer with `hardware`, so
`deep-pair.json`'s tenth layer key is `"hardware"` where it was `"resources"` (line 272, and
nothing else in either file). `deep-pair.txt` did **not** move at all: the fixture pair declares
no hardware element, so that layer's section renders nothing in the text form. The fixture pair
itself gained one field — `Task.core` became required and both tasks carry `core: "CORE0"`,
which is therefore unchanged between them and produces no delta. Both goldens were verified
byte-identical outside those two facts before the edit.

**The compositions layer moved exactly one byte-range as well, for the same kind of reason.** Contract `4.0.0`
retired the `containers` layer for `compositions`, so `deep-pair.json`'s **third** layer key is
`"compositions"` where it was `"containers"` (line 33, and nothing else in either file) — the
compositions layer took the display slot the retired one vacated, which is why the key moved in
place rather than to the end. `deep-pair.txt` did **not** move at all, for the reason it did not
move for the hardware layer: the fixture pair declares no composition, so that layer's section renders nothing in
the text form. The fixture pair itself did not change — the contract's added fields are all optional
(`swc.external` and `composition.root` are `omitAtDefault` booleans, the two `composition`
references default to `None`), so no element of the pair had to gain a key. Both goldens were
verified byte-identical outside that one line before the edit.

**Contract `7.0.0` made the third such edit, and it is an ADDITION rather than a
replacement.** The bump adds the `mapping_sets` layer with no layer retired for it, so
`deep-pair.json` gains an eleventh layer key — an empty `{added, removed, changed, rename_hints}`
section between `"data_types"` and `"connectors"`, where the layer's declared display order puts
it, because every declared layer is a key of the JSON form whether or not it changed. `deep-pair.txt` did **not** move, for the reason it did not move for the hardware and compositions layers: the
fixture pair declares no mapping set, so that layer's section renders nothing in the text form.
The fixture pair itself did not change — the bump's one field on an existing kind,
`swc.data_type_mappings`, is `omitAtDefault` with an empty default, so no element of the pair had
to gain a key. Both goldens were verified byte-identical outside that one added block before the
edit.

**Contract `8.0.0` (the first step of the data description split) made the fourth such edit, an
ADDITION again.** The `base_type` kind leaves `data_types` for a layer of its own, `base_types`, so
`deep-pair.json` gains one more layer key — an empty section between `"data_types"` and
`"mapping_sets"`, where the new layer's display order (6) puts it. `deep-pair.txt` did **not**
move: the fixture pair declares no base type, so that layer's section renders nothing in the text
form. The fixture pair itself did not change. Both goldens were verified byte-identical outside that
one added block before the edit.

**The same contract's second split step made the fifth such edit, an ADDITION again.** The four
application kinds leave `data_types` for a layer of their own, `application_types`, so
`deep-pair.json` gains one more layer key — an empty section between `"interfaces"` and
`"data_types"`, where the new layer's display order (4) puts it. `deep-pair.txt` did **not** move:
the fixture pair declares no application type, so that layer's section renders nothing in the text
form. The fixture pair itself did not change. Both goldens were verified byte-identical outside that
one added block before the edit.

**They are otherwise never hand-edited**, and they are not regenerated to make a diff go away — an
unexplained diff is the finding. Regenerate only when an intentional rendering change has been
accepted and recorded in the change's plan first, and only through the renderers:

```python
from pathlib import Path

from swcraft_tools.differ import diff_snapshots, format_diff, format_diff_json

# DEEP_OLD / DEEP_NEW come from packages/tools/tests/test_differ.py
diff = diff_snapshots(DEEP_OLD, DEEP_NEW)
Path("deep-pair.txt").write_text(format_diff(diff) + "\n", encoding="utf-8")
Path("deep-pair.json").write_text(format_diff_json(diff), encoding="utf-8")
```

Editing `DEEP_OLD` / `DEEP_NEW` invalidates both goldens for the same reason: what they record
is one fixed pair's rendering, so a changed pair is a different record and no longer evidence
about the change that was made under it.

**The same contract's third split step made the sixth such edit, a RENAME.** The `data_types`
layer is renamed `implementation_types` (label *Implementation Types*), and a data element's and an
argument's `type` is renamed `implementation_type`. So `deep-pair.json`'s layer key `"data_types"`
reads `"implementation_types"` and one changed field reads `"implementation_type"`, and
`deep-pair.txt`'s section heading reads `Implementation Types:` and the same changed field
`implementation_type:`. Four lines, each the same fact under its new name; no section moved (the
layer keeps display order 5) and nothing was added or removed. The fixture pair's own snapshot
attribute was renamed with the layer. Both goldens were verified byte-identical outside those four
lines before the edit.

**The same contract's elements made the seventh such edit, an ADDITION again** (the data
description elements). Three layers are added with no layer retired for them --
`units`, `compu_methods` and `data_constraints`, the forms a type names by reference -- so
`deep-pair.json` gains three layer keys, each an empty section, between `"base_types"` and
`"mapping_sets"`, where the new layers' display orders (7, 8, 9) put them. `deep-pair.txt` did
**not** move: the fixture pair declares no element of the three, so their sections render nothing
in the text form. The fixture pair itself did not change: the fields the same contract adds to
existing kinds (the types' reference fields) default to `None`, so no element of the pair had to
gain a key. Both goldens were verified byte-identical outside that one added block before the edit.

**Contract `9.0.0`'s instance identity made the eighth such edit, a CHANGED KEY SHAPE**.
A task's runnable entry and a sequence step name their component by exactly one of
`swc` and `instance`, and the collections' composite identity becomes `(swc, instance, runnable)`
with the absent selector `null`. So `deep-pair.txt`'s two composite keys read
`(SpeedCtrlSWC, None, TorqueCalc_Run)` and `(SpeedCtrlSWC, None, SpeedCtrl_Run)` (`:40`, `:45`),
and `deep-pair.json` carries `"schema_version": 2` (`DIFF_SCHEMA_VERSION`, raised because the key
field changed type) and a `null` as the middle part of the same two keys. Five lines, each the same
fact in the new shape; no section moved and nothing else was added or removed. The fixture pair
itself did not change: neither side states an `instance`, and a dumped entry carries the absent
selector as `None`. Both goldens were regenerated through the renderers above and verified
byte-identical outside those five lines.

**Contract `11.0.0` made the ninth such edit, and the first that EDITS the pair** (2026-10-16).
The trace link moved into the architecture element: a requirement no
longer carries `links`, and a requirement file still carrying one is refused on load. The deep
pair's one whole-value string collection was `REQ-SPEED-001`'s `links` losing `ICtrl`, a shape the
model no longer has, so the pair was edited — the one reason this file admits — and both goldens
regenerated through the renderers above: `SpeedCtrlSWC` satisfies `REQ-SPEED-001@1` and
`REQ-SPEED-002@1` on the old side and `REQ-SPEED-001@1` alone on the new, and both requirements
stand unchanged on both sides. What moved, and nothing else: the `Requirements:` section of
`deep-pair.txt` is gone (no requirement changes) and `SpeedCtrlSWC` gains a `satisfies:` block naming
`REQ-SPEED-002@1 (removed)` after `memory:`; `deep-pair.json`'s `requirements.changed` is empty and
the component's `collections` gain the matching `satisfies` entry after `memory`. Every other line of
both files was verified byte-identical before the commit. The claim the pair carries — a whole-value
string member is reported by its own value — is the same claim on the new collection.

**The 0.18.0 diff fix made the tenth such edit, a CHANGED TEXT FORM** (2026-10-21).
A key of a composite collection rule -- a task's runnable entry, a sequence
step, a runnable's `reads`, `writes` and `calls` -- is rendered in the text form as the model writes
the reference: its set parts joined by `.`, the absent scheduling selector left out, where the
whole tuple was printed with the absent part as `None`. So `deep-pair.txt`'s two composite keys read
`SpeedCtrlSWC.TorqueCalc_Run` and `SpeedCtrlSWC.SpeedCtrl_Run` (`:37`, `:42`) where they read
`(SpeedCtrlSWC, None, TorqueCalc_Run)` and `(SpeedCtrlSWC, None, SpeedCtrl_Run)`. Two lines, each the
same fact in the readable form; `deep-pair.json` did **not** move (the document keeps every part, the
absent one as `null`). The fixture pair itself did not change. The text golden was regenerated through
the renderer above and verified byte-identical outside those two lines.
