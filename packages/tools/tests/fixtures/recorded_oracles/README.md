# The frozen oracles of the generic model kernel

Six values recorded from the **un-restructured** code, before the generic model kernel replaced
it. They are the kernel's evidence baseline: the tests that grade it **read** them and never record. **A frozen value is never regenerated from
the new code** — a fixture that disagrees with the rewritten kernel is a defect in the kernel, and
the recorder that produced these was deliberately not committed so no one can re-run it.

**The one admissible exception, and it has been taken twenty-three times**: when the frozen value's own
*declared input* or *model* moves, an oracle of the old input can only ever prove the new state
absent. Every such re-recording is a numbered section below, each one naming the comparison against
the old state that was made **before** writing and the rows that have no before-state at all. A row
with no before-state is recorded from the code it would grade and is therefore **not** an oracle for
it; where that matters, the section says which suite is.

**Recorded at**: parent commit `341c87f` (`feat: the production contract loader`),
in the commit that also corrects the three shipped field descriptions. The correction
comes **first** on purpose: `registry_tables.json` holds the corrected text, so nothing downstream
freezes the defect.

**One re-recording, and what it proves.** `registry_tables.json`'s two prose tables —
`KIND_DESCRIPTIONS` and `FIELD_ANNOTATIONS` — were re-recorded on 2026-08-23 at contract `1.4.1`, which authored a `description` for every kind and field that lacked one. That
is not the case the rule above guards against: the kernel did not move, its *declared input* did,
and an oracle of the old input could only ever prove the new prose absent. The re-recording was
checked against the frozen text before it was written: every frozen kind description survives
verbatim and in order, every frozen annotation row keeps its `unit`, `reference` and `immutable`
with its `description` either unchanged or filled in from `null`, and every *new* row carries a
`description` and nothing else. The four structural tables are untouched.

A second, smaller re-recording followed on the same day and under the same contract version: six descriptions were reworded for the editor's hover reader — the
`data_types` layer, `enum_literal`, `timing_event`, `timing_event.period`,
`runnable.min_start_interval` and `runnable.events`. That touched `KIND_DESCRIPTIONS` and
`FIELD_ANNOTATIONS` again and, for the first time, one row of `LAYER_SPECS`, whose `description`
member is the layer's prose; the three remaining structural tables are untouched, and
`packages/tools/tests/test_model_registry.py` compares every table against the live projection, so its
passing is the proof that the frozen text is the engine's.

A third re-recording, still under `1.4.1`, followed on the same day:
twenty-five descriptions reworded for the hover reader (five `LAYER_SPECS` rows, four
`KIND_DESCRIPTIONS` entries, sixteen `FIELD_ANNOTATIONS` rows). Checked the same way before writing:
every table is identical to its previous form with `description` stripped, and
`KIND_DESCRIPTIONS` keeps its keys and their order.

A fourth re-recording, on 2026-08-24 at contract `1.4.2`, touches **one row of
one table**: `PLACEMENT_FIELDS`' `swc` entry, whose `description` is the string contract `1.4.2`
rewords so that it states the rule instead of citing a decision record — the descriptions are
rendered into a user-facing reference page from that contract on, and a reader of that page has no
decision records. Same argument as the three re-recordings above: the kernel did not move, its
declared input did. Checked the same way before writing — every other row of every table, this
one's `kind`, `field` and `type` members included, is byte-identical, and the reworded string keeps
its first sentence verbatim.

A fifth re-recording, at contract `1.4.3`, touches **one row of one table** as
well: `LAYER_SPECS`' `resources` entry, whose `description` reported observed project state
("Present here, so the resource rules are on…") and is now state-neutral, because the guide's
generated reference renders it on a page that observes no project. `absentDescription` is
untouched. Same argument again: the kernel did not move, its declared input did, and every other
row of every table is byte-identical.

**A sixth re-recording, and the first that is not prose.** On 2026-08-28, at contract `2.0.0`, `impact_edges.json` was re-recorded because the model it was recorded over
changed shape: `layer:resources` was replaced by the mandatory `layer:hardware`, so
`impact_model/resources/` became `impact_model/hardware/` (`CPU`, `CORE0`, `CORE1`, `RAM0`) and
`Core0Mirror` was **deleted** — two hardware elements cannot share a name, and the "two `cpu_core`s
sharing a `core`" shape it existed for cannot exist under identity resolution. This is the case the
rule at the top of this file guards against — the oracle's *model* moved — so re-recording is only
admissible because a comparison with the old state is still possible for everything that did not
move, and that comparison was **made before writing**, by the script that wrote it:

- every graph node whose layer token is not the replaced one (`resource` before, `hardware` after),
  and every dependent of it, in order, is byte-identical;
- `impact_of` and `format_impact` for `SpeedSensor` and for `Shared` are byte-identical;
- `RAM` and `RAM0` are the same element renamed: their dependent lists are equal entry for entry,
  and the only difference is the layer token they are grouped under.

Two things are new rather than moved, and neither has a before-state: the `hardware:CORE0 ->
hardware:RAM0` edge (`field:hardware.memory.accessible`, the one relation whose source and target
are the same layer) and a **fourth** `impact_of`/`format_impact` start, `CORE0`, added so that edge
is rendered by the byte oracle rather than only present in the graph. The three original starts are
unchanged but for `RAM` -> `RAM0`.

**A seventh re-recording, back to the prose-and-declaration case.** On 2026-09-01, at contract
`3.1.0`, `registry_tables.json`'s two prose tables move again, because the
contract declares one new field: `field:interfaces.data_element.init`. `KIND_DESCRIPTIONS`'
`data_element` entry is reworded — it enumerated the kind's fields ("a name and the declared data
type it carries") and there is now a third — and `FIELD_ANNOTATIONS` gains one row,
`data_element.init`, carrying its `description` with `unit`, `reference` and `immutable` all
`null`. The same argument as the four prose re-recordings above: the kernel did not move, its
declared input did, and an oracle of the old input could only prove the new declaration absent.

Checked before writing, by the script that wrote it, and the checks are the ones the suite itself
grades by — so a row the suite hides could not have moved unnoticed either:

- `KIND_DESCRIPTIONS`' graded keys and their ORDER are unchanged, and exactly one value differs;
- `FIELD_ANNOTATIONS` gains exactly one graded row and loses none, and the remaining graded rows
  are equal ENTRY FOR ENTRY AND IN ORDER;
- the four structural tables — `KIND_NAMES`, `LAYER_SPECS`, `PLACEMENT_FIELDS`,
  `COLLECTION_SPECS` — are unchanged under the same grading. No collection, kind, layer or
  placement field is added: `init` declares none of those things.

The edit is **surgical and not a regeneration**: the file still records the pre-`2.0.0` `resources`
kinds, which is the state `test_the_frozen_exclusion_is_exactly_the_replaced_layer` derives
`UNGRADED_KINDS` from, and writing the live projection wholesale would have erased that derivation
instead of grading against it.
**A seventh re-recording, prose again, and beside it the first exclusion that is an addition.** On
2026-09-03, at contract `3.2.0`, `registry_tables.json`'s `KIND_DESCRIPTIONS`
`runnable` entry was re-recorded: the runnable gained three access lists (`reads`, `writes`,
`calls`) and its description now names them. The fourth and fifth re-recordings' argument applies
unchanged — the kernel did not move, its declared input did — and the check was made by the script
that wrote it, before writing: the four structural tables and `LAYER_SPECS` byte-identical,
`KIND_DESCRIPTIONS` keeping its keys and their order with exactly one value changed, one line of the
file different. What is **not** re-recorded, because it has no before-state to be checked against:
the three member kinds (`data_read`, `data_write`, `operation_call`), their seven field rows and the
three collection rows. Those are excluded from the frozen comparison by name —
`packages/tools/tests/test_model_registry.py`'s `ADDED_KINDS_3_2` / `ADDED_FIELDS_3_2`, the fourth
exclusion category after the hardware layer's three and the first that hides rows the fixture *never
carried* rather than rows it disagrees about — and graded positively by
`test_the_access_additions_are_exactly_the_added_rows`, which asserts what they project and that
they sit in one contiguous run after `init_event` with nothing else moved.

**A ninth re-recording, prose again — and the ordinal is one past the file's own count.** Two
blocks above are both headed *A seventh re-recording* (contract `3.1.0` and contract `3.2.0`); the
second of them is the eighth, and this one is the ninth. On 2026-09-06, at contract `4.0.0`,
`registry_tables.json`'s two prose tables move for three strings, all of them because
`layer:containers` was retired for `layer:compositions`:

- `KIND_DESCRIPTIONS`' `port` gains a sentence pointing at `kind:compositions.composition_port`,
  which declares the same four fields for a composition's boundary port;
- `KIND_DESCRIPTIONS`' `connector_endpoint` is rewritten, because the endpoint now names a
  component **or** a composition type and the writer resolves that type inside the connector's own
  composition;
- `FIELD_ANNOTATIONS`' `connector_endpoint.swc` gains a sentence saying that exactly one of `swc`
  and `composition` is set.

The fourth-through-eighth re-recordings' argument applies unchanged: the kernel did not move, its
declared input did, and an oracle of the old input could only ever prove the new declaration absent.
The checks were made before writing, by the script that wrote it, and they are the ones the suite
grades by:

- `KIND_DESCRIPTIONS` keeps its keys AND their order, with exactly two values changed;
- `FIELD_ANNOTATIONS` gains no row and loses none, every row keeps its `kind`, `field`, `unit`,
  `reference` and `immutable`, and exactly one `description` differs — as a pure **extension**, the
  frozen sentence surviving as its prefix;
- the four structural tables — `KIND_NAMES`, `LAYER_SPECS`, `PLACEMENT_FIELDS`,
  `COLLECTION_SPECS` — are byte-identical. Three lines of the file differ in total.

What is **not** re-recorded, for want of a before-state: the retired `container` kind and the four
new `compositions` kinds (`composition`, `composition_port`, `composition_member`, `delegation`),
their field and collection rows, and the five fields contract `4.0.0` added to kinds the fixture does carry
(`swc.external`, `swc.external_ref`, `swc.external_dest`, `connector.composition`,
`connector_endpoint.composition`). Those are excluded from the frozen comparison by name in
`packages/tools/tests/test_model_registry.py` — `UNGRADED_LAYERS` / `UNGRADED_KINDS` for the replaced layer,
the fifth exclusion category `ADDED_FIELDS_4_0` for the five added fields — and
`test_the_frozen_exclusion_is_exactly_the_replaced_layer` derives every one of those sets from the
two documents rather than trusting them.

**A tenth re-recording, and the second whose MODEL moved.** On 2026-09-06, at contract `4.0.0`, `impact_edges.json` was re-recorded because `impact_model/` had to change shape:
`layer:containers` was retired for `layer:compositions`, which declares **five** references where
the retired layer declared one, so `impact_model/containers/` became `impact_model/compositions/`
(`Powertrain`, `VehicleTop`), the connectors layer gained `Powertrain_to_Shared` — an assembly whose
`from` endpoint names a composition TYPE rather than a component — `Sensor_to_Control` gained the
`composition` it is drawn inside, and `swc/shared/Shared.yaml` gained the one port that endpoint
connects to. This is the sixth re-recording's case, not the prose ones': the oracle's *model* moved,
so re-recording is admissible only because a comparison with the old state is still possible for
everything that did not move, and that comparison was **made before writing**, by the script that
wrote it:

- the surviving graph nodes appear in the **same order**, and each one's dependents survive as an
  ordered SUBSEQUENCE of its new list. A subsequence and not a prefix, deliberately: the three
  `compositions` rows sit between the `connectors` rows and the `requirements` row in the emission
  plan, so a new dependent lands in the MIDDLE of an existing list rather than after it, which is
  exactly what `test_the_emission_plan_groups_consecutive_rows_of_one_source_layer` predicts of a
  row inserted among the listed ones;
- the nodes that disappeared are exactly the two `systems` elements, and every node that appeared is
  a `composition`;
- each of the four `impact_of` starts keeps its **layer-grouping order** and, within each group, its
  old rows in order.

One claim of the model is **retired** rather than moved, and it is named here because nothing else
would say so: `systems` is now a **leaf**. `field:containers.container.system` was the only declared
edge that ever targeted that layer, and contract `4.0.0` declares no replacement, so
`impact_of("Shared")` reports the `system` group EMPTY where it used to report `SharedContainer`.
The two groups are still reported separately, which is what the shared name is in the fixture for,
and `test_one_name_carried_by_two_layers_reports_each_match_separately` asserts the empty group
directly rather than leaving it to the frozen file.

Two things are new rather than moved, and neither has a before-state: the four `compositions` edges
the model now exercises (`composition_port.interface`, `composition_member.type`,
`composition_member.composition`, and `connector_endpoint.composition` beside
`connector.composition`), and the `swc ISpeed -> Shared` edge the new port draws.
`writer_paths.json` and `writer_refusals.json` were re-recorded in the same pass, for the same
reason: their subject is the set of public `create_*` functions, and contract `4.0.0` replaced one of them.

**An eleventh re-recording, prose again and the smallest of them.** On 2026-09-10, at contract
`4.5.1`, `registry_tables.json`'s `KIND_DESCRIPTIONS`
`data_element` entry was re-recorded: contract `4.5.0` declared
`field:interfaces.data_element.asil` on that kind and left the kind's own description enumerating
the three fields it had before, so the patch adds the `asil` to the enumeration. The
fourth-through-ninth re-recordings' argument applies unchanged — the kernel did not move, its
declared input did, and an oracle of the old input could only ever prove the new prose absent — and
the checks were made by the script that wrote it, before writing, and reported:

- the five other tables — `KIND_NAMES`, `LAYER_SPECS`, `PLACEMENT_FIELDS`, `FIELD_ANNOTATIONS`,
  `COLLECTION_SPECS` — byte-identical, `FIELD_ANNOTATIONS` included, because the `asil` row it
  already carries is the field's own description and only the *kind*'s moved;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly one value changed;
- the frozen sentence survives as a **prefix** of the new one, as it did at `4.0.0`. One line of the
  file differs.

Nothing is new without a before-state here, so this re-recording adds no exclusion:
`field:interfaces.data_element.asil` itself arrived at `4.5.0` and is graded already.

**A twelfth re-recording, prose again, and the widest prose one since `1.4.1`.** On 2026-09-11, at
contract `4.7.1` (a prose-only patch), **twelve** `FIELD_ANNOTATIONS` rows and **one**
`KIND_DESCRIPTIONS` entry were re-recorded. Contract `4.7.0` made a file name *derived from* an
element's identity rather than equal to it — a case-insensitive collision gives the file a `~N`
suffix — and the identity fields' descriptions still read *"the X's identity and its filename:
`dir/{name}.yaml`"*, and the requirement kind's *"its file is `{id}.yaml`"*. The twelve rows are the
graded identity fields: `swc.name`, `sender_receiver.name`, `client_server.name`, `connector.name`,
the five data type kinds' `name`, `requirement.id`, `system.name` and `task.name`; the entry is
`requirement`. The fourth-through-ninth re-recordings' argument applies unchanged, and the checks
were made by the script that wrote it, before writing, and reported:

- the four other tables — `KIND_NAMES`, `LAYER_SPECS`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS` —
  byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, and `FIELD_ANNOTATIONS` its `(kind, field)`
  rows and their order; each re-recorded row differs from its frozen row in `description` alone,
  and each new value is the shipped contract's string;
- the three rows still carrying the old wording are left as they were recorded, because their kinds
  are ungraded here: `container.name` and `cpu_core.name` describe kinds the contract no longer
  declares, and the frozen `memory.name` is the retired `resources` layer's kind, not `hardware`'s,
  so writing today's hardware string into it would record a description of a different kind. The
  file keeps its line count; 13 lines differ.

Nothing is new without a before-state here either, so this re-recording adds no exclusion.

**A thirteenth re-recording, and the third whose MODEL moved.** On 2026-09-13, at contract `5.0.0`,
`impact_edges.json` was re-recorded because `impact_model/data_types/` was migrated onto the three
kinds that replace `primitive` and `alias`: each of the three primitives (`float32`, `uint16`,
`uint8`) kept its name as a `value_type` over a new `base_type` named `{name}_base`, the alias
`Speed` became a `value_type` whose `base` names `float32_base`, and one file was added,
`SpeedRef` — a `reference_type` over `Speed` — so that `field:data_types.reference_type.base`, a
reference the contract now declares, is exercised by the fixture as every declared reference is.
The oracle's *model* moved, so the comparison with the old state was **made before writing**, by
the script that wrote it, exactly as in the sixth and tenth re-recordings:

- the surviving graph nodes appear in the **same order**, and each one's dependents survive as an
  ordered subsequence of its new list; the one surviving node whose list changed is `Speed`, which
  gains `SpeedRef` after its three old dependents;
- the one node that disappeared is `data_type float32`: its only dependent was `Speed` through the
  alias' `base`, and that reference now names `float32_base`, so `float32` has no dependent and is
  not a node; the three nodes that appeared are the three base types, `float32_base` (dependents
  `Speed` and `float32`), `uint16_base` (`uint16`) and `uint8_base` (`uint8`), emitted in the
  `data_types` pass, after `Speed` and before the first `composition` node, because
  `packages/tools/src/swcraft_tools/query.py`'s `_EDGE_DETAILS` row for the retired `alias.base` became the
  rows for `value_type.base` and `reference_type.base` in the same position;
- all four `impact_of` starts and all four `format_impact` texts are **byte-identical** to the old
  record — none of the four starts reaches a data type.

Nothing is new without a before-state except the `reference_type.base` edge `SpeedRef` draws and the
three `value_type.base` edges that name a base type, and neither `writer_paths.json` nor
`writer_refusals.json` is re-recorded in this pass.

**A fourteenth re-recording, and the first that RENAMES a kind in place.** On 2026-09-13, at contract
`5.0.0`, four files moved because the contract replaced the data type kinds
`primitive` and `alias` with `base_type` and `value_type` and added `reference_type`. The edit is
**surgical, not a regeneration**: a script loaded each file, replaced only the rows below, and made
every check before writing anything, aborting with no file touched on the first failure. Each file
re-serializes byte-identically to its committed form before the edit, so the diff is exactly the rows.

- `registry_tables.json`. The two `KIND_NAMES` rows are **renamed** in place,
  `PrimitiveDataType`/`primitive` to `BaseDataType`/`base_type` and `AliasDataType`/`alias` to
  `ValueDataType`/`value_type`. The two `KIND_DESCRIPTIONS` keys are renamed **in their positions**.
  Their thirteen `FIELD_ANNOTATIONS` rows, six and seven, are renamed with the fields in the same order
  and every row's `unit`, `reference` and `immutable` equal to its predecessor's. `LAYER_SPECS`'
  `data_types` row keeps every member but `annotation.members` and `description`. `PLACEMENT_FIELDS`
  and `COLLECTION_SPECS` are byte-identical.
  - Checked before writing: after the rename, every graded row of all four moved tables equals the
    live projection, in order.
- `authorization_vocabulary.json`: `_LAYER_MODELS`' `data_types` members, the same two class renames
  in place. `LAYER_FIELDS`' `data_types` row did not move, because the new kinds declare no field name
  the retired two did not.
- `writer_paths.json`: `create_data_type`'s two targets pass `kind: base_type` where they passed
  `kind: primitive`. Nothing else in either row moved.
- `writer_refusals.json`: the two `collisions` seeds of `create_data_type` declare `kind: base_type`.
  - Checked before writing: each seed is still exactly what the translated baseline call writes
    (measured: `name`, `kind`, `size`, `encoding`).
  - Not one verdict, exception type, message prefix, path or `reason` moved. No row of `functions`
    names a kind.

**Every change, classified.** A *rename* keeps the verdict or fact and changes a token; a *change of
meaning* states something different about what the model or the writer does.

| Row | Class |
|---|---|
| both `KIND_NAMES` rows; both `_LAYER_MODELS` class names; the two `writer_paths.json` arguments and the two `writer_refusals.json` seeds (**no verdict moved**) | rename |
| `FIELD_ANNOTATIONS` `kind` ×2 (`always base_type`/`value_type`); `value_type.name` (*all five* → *every* data type kind); `value_type.base` (*must name a declared `primitive`: alias-over-alias is rejected* → *must name a declared `base_type`*: the same refusal); `value_type.unit`, `.value_min`, `.value_max` (*aliased value* → *value*) | rename (prose reworded) |
| `base_type.size`, `.encoding`, `.description`; `value_type.description` | rename (text byte-identical) |
| `LAYER_SPECS` `data_types.description` (*Application data types. Five element kinds* → *Data types. Six element kinds*) | rename plus the added kind; the dropped *Application* corrects a term, since none of the kinds is an AUTOSAR application data type |
| `KIND_DESCRIPTIONS` `base_type` | **change of meaning**: a base type is never what a data prototype is typed by, where a primitive was |
| `KIND_DESCRIPTIONS` `value_type` | **change of meaning**: the emitted `IMPLEMENTATION-DATA-TYPE` references its `base` through `BASE-TYPE-REF` |
| `FIELD_ANNOTATIONS` `base_type.name` | **change of meaning**: the sentence *It is suffixed `_impl` for the generated implementation type* is gone with the twin |
| `FIELD_ANNOTATIONS` `base_type.native_declaration` | **change of meaning**: *when empty, the writer falls back to the type's own name* became *never derived; required by check-model* |

The four changes of meaning are the contract's own decided prose. The first three are
`5.0.0`'s kinds, the fourth the rule that a native declaration is never derived. Each was already
graded by `test_model_contract_conformance.py` and the discover vectors before this file moved, and
none is a verdict of the writer. They are recorded here because this file now carries them, not
because re-recording decided them.

**No before-state, and therefore not an oracle here:**

- `ReferenceDataType`, the third member written into both `annotation.members` lists. The whole-row
  comparisons cannot omit it. Its oracle is
  `test_model_registry.py::test_a_multi_kind_layer_publishes_its_top_level_kinds`, which writes the six
  kinds out by hand.
- The `reference_type` kind's own rows. They are **not** written into `registry_tables.json`: they are
  excluded by name, `ADDED_KINDS_5_0`, the seventh exclusion category. They are graded positively by
  `test_the_reference_type_addition_is_exactly_the_added_rows`.

**A fifteenth re-recording, prose again.** On 2026-09-14, at contract `5.0.0`,
**ten** rows of `registry_tables.json` were re-recorded, because the contract reworded ten
descriptions inside `5.0.0` that still described the five retired data type kinds or did not yet
state the six: `LAYER_SPECS`' `data_types` row (the split between a base type and the kinds a data
prototype names), `KIND_DESCRIPTIONS`' `enum` entry, and eight `FIELD_ANNOTATIONS` rows —
`enum.name`, `record.name` and `array.name` (*all five data type kinds* → *every data type kind*),
`data_element.init` (*a primitive or an alias* → *a value type or a reference type*), and
`data_element.type`, `argument.type`, `record_field.type` and `array.element_type`. The
fourth-through-ninth re-recordings' argument applies unchanged — the kernel did not move, its
declared input did — and the checks were made by the script that wrote it, before writing, and
reported:

- the file re-serializes byte-identically through `json.dumps(indent=2, ensure_ascii=False)` before
  the edit;
- `KIND_NAMES`, `PLACEMENT_FIELDS` and `COLLECTION_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, `LAYER_SPECS` and `FIELD_ANNOTATIONS` their
  rows and their order, and each re-recorded row differs from its frozen row in `description` alone,
  each new value being the shipped contract's string. The file keeps its 1 385 lines; 10 differ.

| Row | Class |
|---|---|
| `enum.name`, `record.name`, `array.name`; `data_element.init` | rename (prose reworded): the count and the two kind names the fourteenth re-recording retired |
| `LAYER_SPECS` `data_types.description`; `data_element.type`, `argument.type`, `record_field.type`, `array.element_type` | **change of meaning**: each now states the reference-role rule contract `5.0.0` declares — a base type is never what a data element, an argument, a record field or an array element names |
| `KIND_DESCRIPTIONS` `enum` | *primitive-compatible* dropped (rename), and **a correction**: the entry said an enum is emitted as an `IMPLEMENTATION-DATA-TYPE`, which the writer has never done — it emits an `APPLICATION-PRIMITIVE-DATA-TYPE` of category `TEXTTABLE` with its `COMPU-METHOD`, before contract `5.0.0` as after it (until contract `7.5.1`, which writes the type `VALUE` and keeps `TEXTTABLE` on the method — the twenty-fifth re-recording) |

None of the ten is a verdict of the writer, and nothing is new without a before-state, so this
re-recording adds no exclusion.

**A sixteenth re-recording, prose again.** On 2026-09-16, at contract `5.1.0`,
**five** rows were re-recorded, because the contract reworded the six descriptions that still
required a connector's two ports — and a delegation's — to name **one** interface, which the bump
directly beneath them stopped requiring: `LAYER_SPECS`' `connectors` row, `KIND_DESCRIPTIONS`'
`connector` entry, and three `FIELD_ANNOTATIONS` rows — `port.interface`, `connector.from` and
`connector.to`. The **sixth** reworded string, `composition_port.interface`, has no frozen row to
re-record at all: `composition_port` is one of `UNGRADED_KINDS`, this fixture having been frozen
before `layer:compositions` existed. The fourth-through-ninth re-recordings' argument applies
unchanged — the kernel did not move, its declared input did — and the checks were made by the
script that wrote it, before writing, and reported:

- `KIND_NAMES`, `PLACEMENT_FIELDS` and `COLLECTION_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`connector`) — checked as "exactly one", not by stripping a `description` key, because that
  table is a flat `{kind: description}` map and has no such key to strip;
- `LAYER_SPECS` and `FIELD_ANNOTATIONS` keep their rows and their order, and each re-recorded row
  differs from its frozen row in `description` alone, each new value being the shipped contract's
  string.

| Row | Class |
|---|---|
| `LAYER_SPECS` `connectors.description`; `KIND_DESCRIPTIONS` `connector`; `port.interface`, `connector.from`, `connector.to` | **change of meaning**: each said the two ends of a connector name the same interface, which contract `5.1.0` replaces with the standard's condition — the required port's interface must be *satisfied by* the provided port's, which the same interface always satisfies |

Nothing is new without a before-state, so this re-recording adds no exclusion.

**A seventeenth re-recording, prose again, beside the first exclusion that is a REMOVAL.** On
2026-09-16, at contract `6.0.0`, **four** rows were re-recorded, because the
contract reworded the descriptions that still said the model is drawn by LikeC4: `LAYER_SPECS`'
`sequences` row, `KIND_DESCRIPTIONS`' `sequence` entry, and two `FIELD_ANNOTATIONS` rows —
`swc.description` and `sequence.name`. The other two reworded strings, `composition.title` and
`composition.description`, have no frozen row: `composition` is one of `UNGRADED_KINDS`. The
fourth-through-ninth re-recordings' argument applies unchanged, and the checks were made by the
script that wrote it, before writing, and reported:

- the file re-serializes byte-identically through `json.dumps(indent=2, ensure_ascii=False)` before
  the edit, and keeps its 1 385 lines;
- `KIND_NAMES`, `PLACEMENT_FIELDS` and `COLLECTION_SPECS` byte-identical;
- each re-recorded row differs from its frozen row in `description` alone, each new value being the
  shipped contract's string.

| Row | Class |
|---|---|
| `LAYER_SPECS` `sequences.description`; `KIND_DESCRIPTIONS` `sequence`; `swc.description`, `sequence.name` | rename (prose reworded): the diagram each string names is no longer a LikeC4 view; `sequence.name` also states that the name is the scenario's identity and names its diagram files, and keeps the grammar |
| `sequence.title`, `sequence_step.note` (2026-09-17) | rename (prose reworded): both still said the scenario is drawn as a *dynamic view*, LikeC4's word, which the `6.0.0` screen for `LikeC4`/`C4` did not reach; each now names the sequence diagram and what it shows (the heading `sd {name} — {title}`, the activation's `{runnable} — {note}`). Folded into this same re-recording rather than numbered: the same contract version, the same reason, and the same checks — `description` alone differs, each new value the shipped contract's string, the file still 1 385 lines |

The same contract **removes** `layer:systems` with its one kind, and its frozen rows — the `systems`
row of `LAYER_SPECS` and of `KIND_NAMES`, the `system` entry of `KIND_DESCRIPTIONS` and the three
`system.*` rows of `FIELD_ANNOTATIONS` — are **kept, not re-recorded**: they are the state this
file recorded, and a removed layer has no after-state to record. `packages/tools/tests/test_model_registry.py`
excludes them by name (`systems` in `UNGRADED_LAYERS`, `system` in `UNGRADED_KINDS`), and its
symmetric-difference assertion is what shows the exclusion is exactly the removed layer.

**An eighteenth re-recording, and the fourth whose MODEL moved.** On 2026-09-16, at contract `6.0.0`, the layer removal reached `impact_model/` and `impact_edges.json`. The model held
two `systems/` elements: `VehicleSystem`, which nothing referenced since contract `4.0.0`, and `Shared`, the
leaf half of the one name two layers carry. Both files were deleted. So that the shape the fixture
holds the name for survives, `requirements/Shared.yaml` was added: a requirement whose `id` is
`Shared` and which links nothing, so it is a graph **leaf** exactly as the system was (no declared
reference or relation targets `requirements`), and `requirements` sorts before `swc` in
`displayOrder` exactly as `systems` did. Two values of `impact_edges.json` moved, and nothing else:

- `impact_of["Shared"]`'s first group key, `system` -> `requirement`, still `[]`, still first;
- `format_impact["Shared"]`'s first heading, `system:` -> `requirement:`, still `(no dependents)`.

The checks were made before the edit was kept, by running the oracle's own suites
(`test_model_relations.py`, `test_query.py`) against the moved model: the whole `graph` list is
**unchanged** byte for byte — neither removed system was a node with a dependent, and a requirement
with no links adds no edge — the `impact_of` and `format_impact` entries of the other starts are
unchanged, and the `swc` half of `Shared` keeps its four dependents in order. The file keeps its
key order. `authorization_vocabulary.json` is **not** re-recorded: its `systems` rows are the state
it recorded, and `test_authorization_generic.py` / `test_authorization_scope.py` exclude the removed
layer from the graded comparison through the symmetric difference they already derive, holding the
removal apart from the two replacements by name.

**A nineteenth re-recording, and the second that DELETES a writer.** On 2026-09-16, at contract
`6.0.0`, `writer_paths.json` and `writer_refusals.json` moved, because
`writer/mutations.py` lost `create_system` with `layer:systems` — the function, its export and
`__all__` entry in `writer/__init__.py`, and its tests in `packages/tools/tests/test_writer_model.py`. This
section was written after that commit; the edit it
records is that commit's, and nothing was re-edited to write it. What was deleted, and nothing else:

- `writer_paths.json`: the one `create_system` row of `targets` (`systems/ProbeSystem.yaml`).
  Twelve composed targets become **eleven**, and eleven public `create_*` become **ten**.
- `writer_refusals.json`: `create_system`'s whole entry in `functions` (its seven identity probes,
  `accepted_baseline` through `identity_over_long`) and in `collisions` (its two collision-suffix cases).
  `functions` goes from 11 entries and 82 cases to **10 and 75**, `collisions` from 11 entries and
  22 cases to **10 and 20**. One row of each `filesystem_dependent` property went with the entry:
  `control-character-in-a-component` 5 → **4**, `over-long-component` 6 → **5**.

The argument is the one made for `create_container`, stated in `writer_refusals.json`'s `note`
(the oracle records every public `create_*` that exists today) and at the end of the tenth re-recording: the oracle's subject is the set of public `create_*`
functions, and a removed layer has no writer left to record a verdict for. A frozen verdict for a
function that does not exist could only fail as an import, which proves nothing about the writers
that remain. Keeping the function instead would have left a mutation writing into a directory the
contract no longer declares, which is worse than a missing one. Nothing is added, so nothing here
lacks a before-state.

Checked against the commit's parent (`git show 6e0795c6^:<file>`), by a script that parsed both
versions of both files and compared them:

- every top-level key of the old file survives with an equal value — `model`, `note`,
  `filesystem_dependent_note`, `filesystem_dependent_properties`, `probe_values` and the
  per-change note keys the files then carried — and the one key added was a note for this change, last.
  No per-change note key remains: what they stated that still holds is in `writer_refusals.json`'s
  `note`, and `writer_paths.json` carries no note;
- with `create_system` filtered out of the old lists, `targets`, `functions` and `collisions` keep
  their functions in the same order, and every surviving row re-serializes byte-identically to its
  old self through `json.dumps(indent=2, ensure_ascii=False)` — with one exception, below;
- the exception is `create_sequence` in `functions`: exactly two cases differ,
  `identity_bare_dot` and `identity_control_character`, each in `message` alone, and in each the
  old message with *a valid LikeC4 identifier* replaced by *an identifier, because it names the
  sequence's diagram files* is the new message. `accepted`, `exception`, the grammar
  `^[A-Za-z_][A-Za-z0-9_]*$` and every other case are unchanged. The same contract renamed
  `rule:sequences.sequence.name.likec4-identifier` to `rule:sequences.sequence.name.identifier`,
  and the validator reworded its message to match. That is a rename of the text, and no verdict
  moved.

| Row | Class |
|---|---|
| `writer_paths.json` `targets` `create_system`; `writer_refusals.json` `functions` and `collisions` `create_system` (seven and two cases, one of each `filesystem_dependent` property among them) | **removal**: the function is gone with its layer, so there is no after-state to record |
| `writer_refusals.json` `create_sequence` `identity_bare_dot.message`, `identity_control_character.message` | rename (prose reworded): the validator names the diagram files instead of LikeC4; the verdict, the exception type and the grammar are unchanged |

The filesystem paragraph at the end of this file counts the `filesystem_dependent` rows as they were
when they were first recorded: thirteen, six and seven. Measured at this commit's parent the file
carried eleven, five and six, and after this removal it carries **nine**, four and five. The
paragraph stays as recorded, and the counts above are today's.

**A twenty-first re-recording, prose and one annotation.** On 2026-09-20, at contract `7.0.0`, **eight** rows of `registry_tables.json` were re-recorded, because the bump
declares `kind:data_types.application_type` with its two nested member kinds and rewords every
description the new kind makes false:

- `LAYER_SPECS`' `data_types` row — its `description` (six element kinds and two member kinds
  became seven and four) **and** its `annotation.members`, which gains
  `swcraft_tools.schema.ApplicationDataType` as the union's seventh member. The annotation is
  re-recorded rather than excluded for the reason the fourteenth re-recording gives for the same
  row: the kernel did not move, its declared input did, and a row re-recorded is a row still
  graded;
- `KIND_DESCRIPTIONS`' `value_type` entry, which said a record field and an array element name a
  value type as their type and named the generated `{name}_AppView` the exporter no longer emits.
  `reference_type`'s entry says the same two things and is **not** re-recorded, because it has no
  frozen row at all (it is in `ADDED_KINDS_5_0`);
- five `FIELD_ANNOTATIONS` rows — `record_field.type` and `array.element_type` (each refused only a
  base type and now refuses every implementation type), `data_element.type` and `argument.type`
  (each enumerated the kinds an element may name without the new one), and `data_element.init`
  (which enumerated the types a scalar `init` may sit under without it).

`KIND_NAMES`, `PLACEMENT_FIELDS` and `COLLECTION_SPECS` are byte-identical; `LAYER_SPECS`,
`KIND_DESCRIPTIONS` and `FIELD_ANNOTATIONS` keep their rows, their keys and their order, and each
re-recorded row differs from its frozen row in `description` alone but for the one annotation named
above, each new value being the shipped contract's string.

| Row | Class |
|---|---|
| `LAYER_SPECS` `data_types.annotation.members` | rename-class addition: the union gains a seventh member, exactly as the fourteenth re-recording's gained a third implementation-type class |
| `LAYER_SPECS` `data_types.description`; `KIND_DESCRIPTIONS` `value_type`; `record_field.type`, `array.element_type` | **change of meaning**: a composite's members are typed by the physical view, so a value type and a reference type are refused there where only a base type was |
| `data_element.type`, `argument.type`, `data_element.init` | rename (prose reworded): the kind the bump beneath them added, added to the enumeration each carries |

The rows contract `7.0.0` **adds** have no before-state and are not written into this file: the
`mapping_sets` layer, the five new kinds and `swc.data_type_mappings` are excluded by name in
`packages/tools/tests/test_model_registry.py` (`UNGRADED_LAYERS`, `ADDED_KINDS_7_0`, `ADDED_FIELDS_7_0`),
and its symmetric-difference assertions are what show the exclusion is exactly what the bump added.
`authorization_vocabulary.json` is **not** re-recorded, for the reason contract `6.0.0`'s entry
gives: its rows are the state it recorded, and the two authorization suites exclude a layer with no
before-state through the symmetric difference they already derive.

**A twenty-fourth re-recording, prose and two annotations.** On 2026-09-24, at contract `7.5.0`, **four** rows of `registry_tables.json` were re-recorded, because the bump
declares the two external stub kinds, `kind:interfaces.external_interface` and
`kind:data_types.external_type`, and rewords every description the new kinds make false:

- `LAYER_SPECS`' `interfaces` and `data_types` rows — each `description` (two interface kinds
  became three, seven data type kinds eight) **and** each `annotation.members`, which gains
  `swcraft_tools.schema.ExternalInterface` and `swcraft_tools.schema.ExternalDataType` as its
  union's last member — re-recorded rather than excluded, for the twenty-first re-recording's
  reason;
- two `FIELD_ANNOTATIONS` rows, `sender_receiver.name` and `client_server.name`, each of which said
  the name is unique across *both* interface kinds.

`KIND_NAMES`, `KIND_DESCRIPTIONS`, `PLACEMENT_FIELDS` and `COLLECTION_SPECS` are byte-identical;
each re-recorded row differs from its frozen row in `description` alone but for the two annotations
named above, each new value being the shipped contract's string.

| Row | Class |
|---|---|
| `LAYER_SPECS` `interfaces.annotation.members`, `data_types.annotation.members` | rename-class addition: each union gains one member, as the twenty-first re-recording's gained its seventh |
| `LAYER_SPECS` `interfaces.description`, `data_types.description`; `sender_receiver.name`, `client_server.name` | rename (prose reworded): the kind the bump beneath them added, added to the count or the enumeration each carries |

The rows contract `7.5.0` **adds** — the two kinds and their ten fields — have no before-state and
are not written into this file: they are excluded by name in `packages/tools/tests/test_model_registry.py`
(`ADDED_KINDS_7_5`) and graded positively by
`test_the_external_stub_addition_is_exactly_the_added_rows`. In `authorization_vocabulary.json` the
`_LAYER_MODELS` members of `interfaces` and `data_types` gain the same two classes, the one edit
contract `7.0.0` also made there (its `ApplicationDataType`); its field vocabularies are not
re-recorded, and the two stubs' `external_ref` and `external_dest` are excluded by name in the two
authorization suites (`ADDED_FIELDS_7_5`).

**A twenty-fifth re-recording, prose only.** On 2026-09-25, at contract `7.5.1` (the enum
category's prose-only patch), **one** row of `registry_tables.json` was re-recorded:
`KIND_DESCRIPTIONS`' `enum` entry, because the writer now emits an enumeration's
`APPLICATION-PRIMITIVE-DATA-TYPE` as category `VALUE` and keeps `TEXTTABLE` on its `COMPU-METHOD`,
and the description said the type itself was `TEXTTABLE`. The fourth-through-ninth re-recordings'
argument applies unchanged — the kernel did not move, its declared input did — and the checks were
made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS`, `LAYER_SPECS` and `FIELD_ANNOTATIONS`
  byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed (`enum`),
  the new value being the shipped contract's string. The file keeps its 1 388 lines; 1 differs.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `enum` | **a correction**: `TEXTTABLE` is a computation method's category and not one the standard's table of application data type categories (`[constr_1007]`) lists; the description now names `VALUE` for the type and `TEXTTABLE` for its compu method, as the writer emits since the same change |

Nothing is new without a before-state, so this re-recording adds no exclusion.

**A twenty-sixth re-recording, prose only.** On 2026-09-25, at contract `7.6.0` (ports
on external component stubs), **three** rows of `registry_tables.json` were re-recorded, because a
port of an external stub now carries a name and a direction and no interface, and each description
said every port is typed by one. The kernel did not move, its declared input did, and the checks
were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS` and `LAYER_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed (`port`);
- `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **two** descriptions changed
  (`swc.ports`, `port.interface`) and every `unit`, `reference` and `immutable` unchanged —
  `port.interface` is still a reference to `interfaces` by `name`, only no longer a required one,
  which no frozen table records. The file keeps its 1 388 lines; 3 differ.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `port` | **an addition to the meaning**: a port of an external stub carries its name and direction only |
| `FIELD_ANNOTATIONS` `swc.ports` | the same, for the collection: an external stub's port declares no interface |
| `FIELD_ANNOTATIONS` `port.interface` | the same, for the field, and that a port of any other component without one is reported |

The field the same contract adds, `port.observed_interface`, has no before-state, so it is
excluded by name on the projected side (`test_model_registry.ADDED_FIELDS_7_6`) rather than
recorded.

**A twenty-seventh re-recording, prose only.** On 2026-09-26, at contract `7.7.0` (a
runnable started by an operation call), **two** rows of `registry_tables.json` were re-recorded,
because a runnable's events now include an operation event and a runnable says whether it may be
started again while it runs, and both descriptions named the two older event kinds only. The kernel
did not move, its declared input did, and the checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS` and `LAYER_SPECS` byte-identical — the
  `events` collection spec records no member kinds, so its widening reaches no frozen row;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`runnable`);
- `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **one** description changed
  (`runnable.events`) and every `unit`, `reference` and `immutable` unchanged. The file keeps its
  1 388 lines; 2 differ.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `runnable` | **an addition to the meaning**: an event may be a client's call of an operation the component provides, and a runnable says whether it may run concurrently with itself |
| `FIELD_ANNOTATIONS` `runnable.events` | the same, for the collection: its third member kind, `operation` |

The kind and the field the same contract adds, `operation_event` and `runnable.concurrent`, have no
before-state, so they are excluded by name on the projected side
(`test_model_registry.ADDED_KINDS_7_7`, `test_model_registry.ADDED_FIELDS_7_7`) rather than recorded.

**A twenty-eighth re-recording, prose and union members.** On 2026-09-26, at contract `7.8.0`
(the implementation structures, arrays and pointers), **seven** rows of
`registry_tables.json` were re-recorded, because the data type layer gained three kinds and every
description that listed the kinds a type role admits named the old set only. The kernel did not
move, its declared input did, and the checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `KIND_DESCRIPTIONS` and `COLLECTION_SPECS` byte-identical — the
  two kind descriptions the contract reworded, `reference_type` and `external_type`, belong to kinds
  the oracle never carried (`ADDED_KINDS_5_0`, `ADDED_KINDS_7_5`), so no frozen row reaches them;
- `LAYER_SPECS` keeps its rows and their order, with exactly **one** row changed (`data_types`): its
  annotation's union gains three members after the eight it had, in the contract's kind order, and
  its `description` is reworded;
- `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **five** descriptions changed and
  every `unit`, `reference` and `immutable` unchanged. The file grows from 1 388 to 1 391 lines (the
  three union members); 7 rows differ.

| Row | Class |
|---|---|
| `LAYER_SPECS` `data_types` | **an addition to the meaning**: eleven element kinds and five member kinds, the implementation structure, array and pointer among them |
| `FIELD_ANNOTATIONS` `data_element.type`, `argument.type` | the same: a data element or an argument may name an implementation record, an implementation array or a pointer |
| `FIELD_ANNOTATIONS` `data_element.init` | the same: an `init` is shaped by an implementation record or array as by a record or an array, and a pointer takes none |
| `FIELD_ANNOTATIONS` `record_field.type`, `array.element_type` | the same, as a refusal: an application composite's member never names one of the three |

The four kinds the same contract adds, `implementation_record`, `implementation_array`, `pointer`
and `implementation_element`, have no before-state, so they are excluded by name on the projected
side (`test_model_registry.ADDED_KINDS_7_8`) rather than recorded. In
`authorization_vocabulary.json` the `_LAYER_MODELS` members of `data_types` gain the three top-level
classes after `ExternalDataType`, the edit contract `7.5.0` made there; its field vocabularies are
not re-recorded, and the three new top-level field names, `elements`, `element_name` and `target`,
are excluded by name in the two authorization suites (`ADDED_FIELDS_7_8`).

**A thirtieth re-recording, prose only.** On 2026-09-30, at contract `7.11.0` (the 0.14.0 writer
fidelity change), **one** row of `registry_tables.json` was re-recorded, because a timing event now
names the modes it is disabled in, and its identity leaves out a field still at its default. The
kernel did not move, its declared input did, and the checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS`, `LAYER_SPECS` and `FIELD_ANNOTATIONS`
  byte-identical — every field the contract adds is new, and no existing field's description moved;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`timing_event`). The file keeps its line count; 1 line differs.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `timing_event` | **an addition to the meaning**: the event does not start its runnable in the modes `disabled_modes` names, and a field at its default is not part of the event's identity |

The two kinds and six fields the same contract adds, `port_com_spec`, `disabled_mode`,
`is_service` on both interface kinds, `sw_impl_policy` and `sw_calibration_access` on the data
element, `com_specs` on the port and `disabled_modes` on the timing event, have no before-state, so
they are excluded by name on the projected side (`test_model_registry.ADDED_KINDS_7_11`,
`test_model_registry.ADDED_FIELDS_7_11`) rather than recorded.

**A thirty-first re-recording, prose only.** On 2026-09-30, at contract `7.11.0` (the 0.14.0
writer fidelity change, its descriptions), **ten** rows of `registry_tables.json` were
re-recorded, because the ARXML writer now writes every declared `description` as its element's
`DESC`, and each description said the text reaches no generated artifact (the component's, that it
is deliberately not emitted into ARXML). The kernel did not move, its declared input did, and the
checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `KIND_DESCRIPTIONS`, `COLLECTION_SPECS` and `LAYER_SPECS`
  byte-identical;
- `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **ten** descriptions changed and
  every `unit`, `reference` and `immutable` unchanged; each old value ended with the sentence the
  contract replaced. The file keeps its 1 391 lines; 10 differ.

| Row | Class |
|---|---|
| `FIELD_ANNOTATIONS` `swc.description` | **a correction of the meaning**: the text is written into the ARXML as the component type's `DESC`; an external component emits no element, so its text reaches the diagrams only |
| `FIELD_ANNOTATIONS` `port.description`, `sender_receiver.description`, `client_server.description`, `connector.description`, `base_type.description`, `value_type.description`, `enum.description`, `record.description`, `array.description` | the same: the text is written as the element's `DESC`, exactly as held, and the empty default writes none |

`memory.description` is **not** re-recorded: the frozen `memory` row is the retired resource layer's
(`test_model_registry.UNGRADED_KINDS`), whose text the hardware memory's does not continue, and the
contract did not reword the hardware memory's either. The other rewordings of the same task —
the member kinds', the composition layer's, the three implementation kinds', the two stubs', the
mapping set's and the composition's `title` — belong to kinds the oracle never carried, so no frozen
row reaches them.

**A thirty-second re-recording, prose only.** On 2026-09-30, at contract `7.11.0` (the 0.14.0
writer fidelity change, its value-type bounds), **two** rows of `registry_tables.json` were
re-recorded, because the ARXML writer now writes a value type's `value_min` / `value_max` as the
type's own `DATA-CONSTR`, and the kind's description said the two reach no ARXML element. The
kernel did not move, its declared input did, and the checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS` and `LAYER_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`value_type`); `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **one**
  description changed (`value_type.unit`) and every `unit`, `reference` and `immutable` unchanged.
  The file keeps its line count; 2 lines differ.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `value_type` | **a correction of the meaning**: a declared bound is written as the type's `{name}_Range` `DATA-CONSTR` of `INTERNAL-CONSTRS`; only `unit` reaches no ARXML element |
| `FIELD_ANNOTATIONS` `value_type.unit` | **an addition to the reason**: the unit has no place because Table 5.18 gives an implementation data type's `unit` none, and `[constr_1009]` makes the table binding |

**A thirty-third re-recording, prose only.** On 2026-10-02, at contract `7.11.0` (the 0.14.0 small
fidelity items change, its contract step), **two** rows of `registry_tables.json` were re-recorded,
because a runnable now carries the C symbol of its entry point and a port may state its own init
value for a data element. The kernel did not move, its declared input did, and the checks were made
before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS` and `LAYER_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`runnable`); `FIELD_ANNOTATIONS` keeps its rows and their order, with exactly **one**
  description changed (`data_element.init`) and every `unit`, `reference` and `immutable`
  unchanged. The file keeps its line count; 2 lines differ.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `runnable` | **an addition to the meaning**: a runnable names the C symbol its entry point is generated under |
| `FIELD_ANNOTATIONS` `data_element.init` | **an addition to the meaning**: a component's port may state a value of its own for the element in its `com_specs`, which takes precedence on that port |

The two fields the same change adds, the runnable's `symbol` and the com-spec entry's `init`, have no
before-state: the first is excluded by name on the projected side
(`test_model_registry.ADDED_FIELDS_7_11`), and the second sits on `port_com_spec`, a kind the oracle
never carried (`test_model_registry.ADDED_KINDS_7_11`).

**A thirty-eighth re-recording, prose only.** On 2026-10-06, at contract `8.0.0` (the 0.14.0 stated
names and stub members), **one** row of `registry_tables.json` was re-recorded,
because an init event may now state its SHORT-NAME. The kernel did not move, its declared input did,
and the checks were made before writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS`, `COLLECTION_SPECS`, `LAYER_SPECS` and `FIELD_ANNOTATIONS`
  byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **one** value changed
  (`init_event`). The file keeps its line count; 1 line differs.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `init_event` | **an addition to the meaning**: the event carries its `type` and, when stated, its `name` |

The fields the same change adds have no before-state: a component's `behavior_name` and the timing
and init events' `name` are excluded by name on the projected side
(`test_model_registry.ADDED_FIELDS_8_0_NAMES`); the operation event's and the three access entries'
`name` and an interface stub's `operations` and `data_elements` sit on kinds the oracle never carried
(`ADDED_KINDS_7_7`, `ADDED_KINDS_3_2`, `ADDED_KINDS_7_5`). The rewordings of the operation event's
`operation`, the three access entries' member fields, a com-spec entry's `data_element` and the stub's
own description sit on those kinds too, so no other row of the file moves.

**A thirty-ninth re-recording, a renamed field and its model.** On 2026-10-09, at contract `9.0.0`, a task names a **partition** where it named a core, and the partition names
the core: `field:tasks.task.core` is a removed identifier and `field:tasks.task.partition` took its
slot. Five files moved, every one because its declared input or its model moved and none because the
kernel did, and each was compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `task`: "a core assignment" became "a partition assignment" | every other row of every table byte-identical; the file keeps its line count and 1 line differs. The task's `partition` field and the partition kind have no before-state: `test_model_registry.ADDED_FIELDS_9_0` and the `hardware` layer's `UNGRADED_KINDS` |
| `impact_edges.json` | `impact_model/` gained `PARTITION0` (on `CORE0`) and `PARTITION1` (on `CORE1`), and its two tasks name them; the graph gained the two partition nodes, and `CORE0`'s and `CORE1`'s dependents are now `RAM0` and their partition where they were their task and `RAM0`; `impact_of` and `format_impact` for `CORE0` reach the task through the partition, two hops (`hardware PARTITION0 (core CORE0)`, then `task Task_10ms (… partition PARTITION0)`) | every other node, in order, and its dependents byte-identical; the old node order an ordered subsequence of the new; `impact_of` and `format_impact` for `SpeedSensor`, `Shared` and `RAM0` byte-identical |
| `writer_paths.json` | `create_task`'s recorded argument `core: CORE0` is `partition: PARTITION0`: the writer's parameter was renamed in its own positional slot — a recording of a renamed parameter, not a new verdict | the recorded path and every other function's row byte-identical |
| `writer_refusals.json` | the two `create_task` seeds' file content says `partition: PARTITION0` where it said `core: CORE0`: a seed is a task file the content check parses, and an 8.x task file no longer loads | both verdicts — accepted with the `~1` suffix, refused as a duplicate identity — and their messages unmoved; every other case byte-identical |
| `authorization_vocabulary.json` | `LAYER_FIELDS` `tasks`: `core` became `partition`, in sorted position | every other layer's list byte-identical, `resources` (the historical layer) included |

The partition kind itself is graded nowhere against this fixture — the `hardware` layer has been
ungraded since it replaced `resources` — and its positive statements are
`test_model_registry.test_the_9_0_0_partition_addition_is_exactly_the_added_row`, the discovery
vectors and `test_model_contract_conformance.py`'s conformance gate.

**A fortieth re-recording, prose and one collection shape.** On 2026-10-10, still inside contract
`9.0.0` (the instance identity), a task's runnable entry and a sequence step
name their component by exactly one of `swc` and the new `instance`, and both collections' composite
identity became `(swc, instance, runnable)`. **Nine** rows of `registry_tables.json` were
re-recorded; the kernel did not move, its declared input did, and the checks were made before
writing:

- `KIND_NAMES`, `PLACEMENT_FIELDS` and `LAYER_SPECS` byte-identical;
- `KIND_DESCRIPTIONS` keeps its keys and their order, with exactly **two** values changed
  (`task_runnable_ref`, `sequence_step`); `FIELD_ANNOTATIONS` keeps its rows and their order, with
  exactly **five** descriptions changed (`task.runnables`, both kinds' `swc` and `runnable`) and
  every `unit`, `reference` and `immutable` unchanged; `COLLECTION_SPECS` keeps its rows, with
  exactly **two** `identity_fields` lists gaining `instance` in the middle (`task.runnables`,
  `sequence.steps`). The file grows by two lines (the two list entries); 9 lines differ.

| Row | Class |
|---|---|
| `KIND_DESCRIPTIONS` `task_runnable_ref`, `sequence_step` | **a correction of the meaning**: the component is named by exactly one of `swc` and `instance`, and the identity is the triple |
| `FIELD_ANNOTATIONS` `task.runnables`, the two `swc`, the two `runnable` | **a correction of the meaning**: "half of the pair" became "part of the triple", and the at-most-one-task rule is stated per resolved component |
| `COLLECTION_SPECS` `task.runnables`, `sequence.steps` | **a changed identity**: `identity_fields` is `[swc, instance, runnable]` |

The two `instance` fields have no before-state and are excluded by name on the projected side
(`test_model_registry.ADDED_FIELDS_9_0_INSTANCE`), graded positively by
`test_the_9_0_0_instance_addition_is_exactly_the_added_rows`.

**Amended within the same re-recording, not a forty-first.** The same day two more descriptions
still named "the `(swc, runnable)` pair": `FIELD_ANNOTATIONS` `swc.runnables` and
`KIND_DESCRIPTIONS` `runnable`. Both were reworded to name
the runnable by its component — the type by `swc` or the occurrence by `instance` — and its
`name` (**a correction of the meaning**, the class of the rows above); exactly **two** further
values changed, every key, row and order unchanged, and no line was added.

**A forty-first re-recording, a renamed field and its unit.** On 2026-10-12, at contract `10.0.0`
(the execution-time budgets), a budget carries its unit: `field:tasks.task.wcet_us` became
`field:tasks.task.wcet`, a string such as `1200us`, in the same slot (the runnable's twin and its new
`wcet_type` sit on a kind this oracle grades only by addition). A 9.x task file spelling `wcet_us` no
longer loads — it is refused with the new spelling — so every recorded input carrying one moved, the
`9.0.0` case exactly. Four files moved, every one because its declared input or its model moved and
none because the kernel did, and each was compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `LAYER_SPECS` `tasks` and `FIELD_ANNOTATIONS` `task.period`: "`wcet_us` in microseconds" became "a budget, `wcet`, carries its own unit"; `FIELD_ANNOTATIONS` `task.wcet_us` became `task.wcet`, re-recorded from its before-state with only the name, the description and the `unit` (`microseconds` → `null`) changed, every `reference` and `immutable` unchanged; `cpu_core.enforcement` names `wcet` | every other row of every table byte-identical; the file keeps its line count and 6 lines differ. The task's field is **graded** again: it leaves `test_model_registry.UNGRADED_FIELDS`, where the hardware layer's `cpu_core` wording had hidden it |
| `writer_paths.json` | `create_task`'s recorded argument `wcet_us: 100` is `wcet: "100us"`: the writer's parameter was renamed in its own positional slot (`9.0.0`'s precedent) — a recording of a renamed parameter, not a new verdict | the recorded path and every other function's row byte-identical |
| `writer_refusals.json` | the two `create_task` seeds' file content says `wcet: 100us` where it said `wcet_us: 100`: a seed is a task file the content check parses, and a 9.x task file spelling `wcet_us` no longer loads | both verdicts — accepted with the `~1` suffix, refused as a duplicate identity — and their messages unmoved; every other case byte-identical |
| `authorization_vocabulary.json` | `LAYER_FIELDS` `tasks`: `wcet_us` became `wcet`, in sorted position (last, as before) | every other layer's list byte-identical |

`impact_model/`'s two task files say `wcet: 1200us` and `wcet: 400us` for the same reason;
`impact_edges.json` records no budget and did not move. The runnable's `wcet` and `wcet_type` have no
before-state and are excluded by name on the projected side (`test_model_registry.ADDED_FIELDS_10_0`).

**A forty-second re-recording, the link moved into the element.** On 2026-10-16, at contract
`11.0.0`, a requirement no longer names the components that realize it:
an architecture element names the requirements it satisfies, `satisfies: [REQ-12@3]`, on twelve
kinds, and the requirement gained `version`, `source` and `derived_from` and lost `links` and
`status` — both refused on load with the new shape named. Three files moved, every one because its
declared input or its model moved and none because the kernel did, and each was compared with its
previous state before writing (the recorders were scratch and refused to write unless every check
held):

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `LAYER_SPECS` `requirements` and `KIND_DESCRIPTIONS` `requirement`: the elements name the requirement, not the reverse; `FIELD_ANNOTATIONS` `requirement.id`: it may hold neither `@` nor `:`; the `requirement.status` and `requirement.links` annotation rows and the `requirement.links` collection row are gone with their fields | every other row of every table byte-identical; 3 rows re-recorded and 3 removed, the file 24 lines shorter. The new fields have no before-state: `test_model_registry.ADDED_FIELDS_11_0` on the graded kinds |
| `impact_edges.json` | `impact_model/`'s two requirement files carry `version` and `source` and no `links`; `REQ_SPEED_001` is satisfied by one existing element of each of eleven kinds — the `Shared` component, `SpeedSensor`'s port `pOut`, `SpeedControl`'s runnable, the `Powertrain` composition, `ISpeed`, `IDiagnostics`, `Task_10ms`, `CPU`, `CORE0`, `RAM0` and `PARTITION0` — and a third requirement, `REQ_SPEED_002`, derives from it and is satisfied by `Powertrain`. The graph gains the two requirement nodes, each listing what satisfies it; every requirement row left the dependents of the components it used to follow | every old node, in the same relative order, keeps exactly its old dependents less the `requirement` rows; the only added nodes are the two requirements; the four `impact_of` starts and the four `format_impact` texts are the old ones less the `requirement` rows, layer-grouping order included |
| `writer_refusals.json` | the two `create_requirement` seeds' file content says `version: 1` and `source: architecture` where it said `status: open` and `links: []`: a seed is a requirement file the content check parses, and a 10.x requirement file no longer loads | both verdicts — accepted with the `~1` suffix, refused as a duplicate identity — and their messages unmoved; every other case byte-identical |

`Shared` stays the graph leaf it has been since contract `6.0.0`, for the opposite reason now: no
element names it. The external interface stub's `satisfies` is the one declared edge the fixture
leaves silent, because the tree may hold no stub (`test_external_stubs_byte_identity.py`'s
`WITHOUT_A_STUB`); `test_model_relations.py` names it in `SILENT_ON_THE_FIXTURE` and witnesses it on a
planted tree. `authorization_vocabulary.json` is not re-recorded — its two tests take the dated
deltas `ADDED_FIELDS_11_0` and `REMOVED_FIELDS_11_0`.

**A fiftieth re-recording, implicit access and asynchronous calls.** On 2026-11-08, at contract
`12.7.0`, a runnable's `events` gained a fifth member kind, `call_returned_event`
(`{type: call_returned, port, operation}` with a stated `result` and `name`), a read and a write the
`implicit` form and a call its synchrony, and the runnable's and its `events` field's descriptions were
reworded to name them. One file moved, because its declared input moved and not because the kernel
did, and it was compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `runnable`: the event clause names the return of an asynchronous call of its component, and the access clause says the data elements are read and written explicitly or implicitly and the operations called synchronously or asynchronously; `FIELD_ANNOTATIONS` `runnable.events`: the description names the `call_returned` events | every other row of every table byte-identical; the file keeps its line count and 2 lines differ. The reworded `reads`, `writes` and `calls` descriptions and the three access kinds' have no row here (the access lists' additions are graded by `test_model_registry.ADDED_KINDS_3_2`) |

`impact_edges.json`, `writer_paths.json`, `writer_refusals.json` and `authorization_vocabulary.json`
did not move: no recorded input declares the new forms.

**A forty-ninth re-recording, prose only.** On 2026-11-06, at contract `12.4.2` (the prose patch
that says a requirement is published in the generated `needs.json` rather than rendered as a
Sphinx-Needs directive in the generated RST), six descriptions of the `requirements` layer were
reworded. One file moved, because its declared input moved and not because the kernel did, and it was
compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `LAYER_SPECS` `requirements` and `KIND_DESCRIPTIONS` `requirement`: `rendered as a Sphinx-Needs directive` leaves and each gains *An architecture requirement is published in the generated `needs.json` as a need of its own; an imported one is never published, only named by its id in the links that reach it.*; `FIELD_ANNOTATIONS` `requirement.id`, `.title`, `.description` and `.req_type`: the rendered directive's `:id:`, heading, body and directive name become the published need's id, title, content and need type | every other row of every table byte-identical; the file keeps its line count and 6 lines differ |

`impact_edges.json`, `writer_paths.json`, `writer_refusals.json` and `authorization_vocabulary.json`
did not move: none records a description.

**A forty-eighth re-recording, prose only.** On 2026-11-06, at contract `12.4.1` (the prose patch
that makes a requirement's `baseline` the version of the requirements delivery it was last imported
from), the requirement kind's description gained one clause: an imported requirement is removed by a
re-import when the delivery no longer holds it. One file moved, because its declared input moved and
not because the kernel did, and it was compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `requirement`: `… without losing a link.` becomes `… without losing a link, or removed by one when the delivery no longer holds it.`; every other word of the row is unchanged | every other row of every table byte-identical; the file keeps its line count and 1 line differs. The three field descriptions the patch also rewords (`source`, `baseline`, `stub`) have no row in `FIELD_ANNOTATIONS`, so nothing else here records them |

`impact_edges.json`, `writer_paths.json`, `writer_refusals.json` and `authorization_vocabulary.json`
did not move: none records a description.

**A forty-seventh re-recording, the data-received event.** On 2026-11-05, at contract `12.3.0`, a runnable's `events` gained a fourth member kind, `data_event`
(`{type: data, port, element}` with the timing event's `disabled_modes`), and the runnable's and its
`events` field's descriptions were reworded to name it. (The forty-sixth is the one
`test_model_registry.ADDED_KINDS_12_2` names, contract `12.2.0`'s rewording of the component's and the
runnable's descriptions, which has no section here.) One file moved, because its declared input moved
and not because the kernel did, and it was compared with its previous state before writing:

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `runnable`: the event clause names the reception of a data element on a require port; `FIELD_ANNOTATIONS` `runnable.events`: the description names the `data` events and a changed element | every other row of every table byte-identical; the file keeps its line count and 2 lines differ. The new kind and its one collection have no before-state: `test_model_registry.ADDED_KINDS_12_3` |

`impact_edges.json`, `writer_paths.json` and `writer_refusals.json` did not move: no recorded input
declares a data-received event.

**A forty-fifth re-recording, the inter-runnable variables' two type edges.** On 2026-11-04, at
contract `12.1.0`, a variable's `application_type` and `implementation_type` became
declared references, and `test_model_relations.py` asserts that the model exercises every declared edge.
`impact_model/swc/control/SpeedControl.yaml` gained two variables, `FilteredSpeed` (application type
`SpeedPhys`) and `SpeedFrameCache` (implementation type `SpeedFrame`), and `query.edges._EDGE_DETAILS` two rows
appended after every existing row. The recorder (scratch, not committed) refused to write unless every check
held, and they held:

| File | What moved | Checked before writing |
|---|---|---|
| `impact_edges.json` | `graph`: `SpeedControl` appended as the last dependent of `SpeedPhys` and of `SpeedFrame`, each with no detail | every node of the old `graph` is still a node, in the same order, and no node was added; every node's old dependents are a prefix of its new ones; `impact_of` and `format_impact` did not move (a type is never a dependent of the component that names it) |

The tree's ARXML stays the recorded refusal for its own reason (a `service` component), and its RST and
diagrams read no variable; `impact_model` is the one recorded tree that declares a variable.

**A forty-fourth re-recording, the inter-runnable variables.** On 2026-11-04, at contract `12.1.0`, a component gained `variables` after `runnables` and a runnable
`reads_variables` and `writes_variables` after `calls`, with three member kinds, and the two owning
kinds' descriptions were reworded to name them. One file moved, because its declared input moved and
not because the kernel did, and it was compared with its previous state before writing (the recorder
was scratch and refused to write unless every check held):

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `swc`: `One software component.` becomes `One software component: its ports, its runnable entities and the inter-runnable variables those runnables share.`; `KIND_DESCRIPTIONS` `runnable`: the frozen text survives with one clause added after the `calls` sentence's port clause, naming `reads_variables` and `writes_variables` | every other row of every table byte-identical; the file keeps its line count and 2 lines differ. The three new kinds and the three new fields have no before-state: `test_model_registry.ADDED_KINDS_12_1` and `ADDED_FIELDS_12_1` |

`impact_edges.json`, `writer_paths.json` and `writer_refusals.json` did not move: the lists are
optional and no recorded input sets them.

**A forty-third re-recording, the requirement's provenance.** On 2026-10-18, at contract `11.1.0`, a requirement gained three optional strings after `asil` — `document`,
`baseline` and `revision`, where an external requirement came from — and its kind description one
sentence naming them. One file moved, because its declared input moved and not because the kernel
did, and it was compared with its previous state before writing (the recorder was scratch and
refused to write unless every check held):

| File | What moved | Checked before writing |
|---|---|---|
| `registry_tables.json` | `KIND_DESCRIPTIONS` `requirement`: the frozen sentence survives as its prefix and gains `` `document`, `baseline` and `revision` record where an external requirement came from.`` | every other row of every table byte-identical; the file keeps its line count and 1 line differs. The three new fields have no before-state: `test_model_registry.ADDED_FIELDS_11_1` |

`impact_edges.json`, `writer_paths.json` and `writer_refusals.json` did not move: the fields are
optional and no recorded input sets them. `authorization_vocabulary.json` is not re-recorded — its
two tests take the dated delta `ADDED_FIELDS_11_1`.

**A thirty-seventh re-recording, an addition to `impact_model/` and nothing else.** On 2026-10-05,
at contract `8.0.0` (the 0.14.0 data description elements, as the writer writes
them), `impact_edges.json`'s `graph` was re-recorded because the model gained contract
`8.0.0`'s seven reference edges into the three element layers: `test_model_relations.py` asserts
that the model exercises every declared edge, and those seven had been carved out of that assertion
(its `ELEMENT_EDGES`) while the ARXML writer refused a model holding an element of the three layers
-- the tree's ARXML is a recorded refusal for a reason of its own (a `service` component), which the
interim guard would have pre-empted. The carrier is one chain no recorded start reaches:
`data_types/units/ProbeKmh.yaml`, `data_types/compu_methods/ProbeSpeedCompu.yaml` (a `linear` naming
`ProbeKmh`) and `ProbeGearTable.yaml` (a `texttable`), `data_types/constraints/ProbeSpeedLimits.yaml`,
`data_types/application/ProbeSpeedPhys.yaml` (an application type naming the unit, the linear and
the constraint) and `ProbeGear.yaml` (an enumeration naming the table),
`data_types/implementation/ProbeGearValue.yaml` (a value type naming the table, over a base type of
its own, `data_types/base/ProbeGearBase.yaml`, so that no existing base type gains a dependent and
moves in the graph's order) and `ProbeGearRef.yaml` (a reference type over it, naming the table).

The recorder (scratch, not committed) refused to write unless every check held. They held:

1. every node of the old `graph` is still a node, in the same relative order; six are added
   (`implementation_type ProbeGearValue`, `base_type ProbeGearBase`, `compu_method ProbeGearTable`,
   `unit ProbeKmh`, `compu_method ProbeSpeedCompu`, `data_constraint ProbeSpeedLimits`);
2. every surviving node's dependent list is unchanged -- no row was appended to any;
3. all four `impact_of` starts and all four `format_impact` texts are unchanged.

| Row | Class |
|---|---|
| `graph` nodes of the six elements above, with their dependents | **addition**: no before-state; the layers and the reference fields are contract `8.0.0`'s |
| every other node, every other dependent, their order | **unmoved**, asserted |
| the four `impact_of` starts and the four `format_impact` texts | **unmoved**, asserted byte for byte |

`test_query.py`'s layer-slice pins over the same model were **edited**: the `implementation_types`
slice gains `ProbeGearValue` and `ProbeGearRef` in the kind rank the contract declares (its page
total moves from 8 to 10), `base_types` gains `ProbeGearBase` (3 to 4) and `application_types` gains
`ProbeGear` and `ProbeSpeedPhys`. `impact_model/` still generates no ARXML at either version, for the
same `service` component, and its RST and diagrams read no data description element, so the
byte-identity records over it are not moved by the files.

**A thirty-sixth re-recording, the third and last of the data description split, which RENAMES a
layer and MOVES a directory.** On 2026-10-04, at contract `8.0.0` (the 0.14.0 data description
layers change, its third split step), `layer:data_types` was renamed `layer:implementation_types`
and stores its elements in `data_types/implementation`; `layer:mapping_sets` keeps its name and
stores them in `data_types/mapping_sets`; the contract's layer list puts the three type layers where
`data_types` stood, in the order application, implementation, base; a data element's and an
argument's `type` was renamed `implementation_type`. No verdict, no edge and no node moved; every
change below is a path, an order, a renamed identity or prose. Surgical again: each script loaded
its file, changed only these rows and checked every graded row against the live projection before
writing (`registry_tables.json`).

- `registry_tables.json`, nine rows. `LAYER_SPECS`' `data_types` row takes the live
  `implementation_types` row (its `name`, `snapshot_attribute`, `storage.directory` and
  `description`); the graded `KIND_NAMES`, `KIND_DESCRIPTIONS` and `FIELD_ANNOTATIONS` rows are
  re-ordered into the projection's order (the application kinds before the implementation kinds and
  the base kind after them, where they had been put last), a historical row keeping its place
  after its frozen predecessor; `KIND_DESCRIPTIONS` `data_element` and `argument` and
  `FIELD_ANNOTATIONS` `value_type.name` take the contract's prose; `data_element.type` and
  `argument.type` become `implementation_type` with the contract's prose and a `reference` into
  `implementation_types`. `PLACEMENT_FIELDS` and `COLLECTION_SPECS` are byte-identical.
- `scaffold_directories.json`, the seventh edit and the count unchanged: `data_types` and
  `mapping_sets` are each replaced in place by `data_types/implementation` and
  `data_types/mapping_sets` (its note says so); `data_types/` itself is scaffolded only as a parent.
- `writer_paths.json` (three rows) and `writer_refusals.json` (eight rows): the `create_mapping_set`
  probe's paths moved from `mapping_sets/` to `data_types/mapping_sets/`, and the
  `create_interface` / `create_cs_interface` probes' data element and argument (arguments and
  occupant texts) state `implementation_type` where they stated `type`; not one verdict moved
  (`test_reserved_device_names`' SHA-256 of the latter re-pinned with that reason).
- `impact_edges.json`: the fourteen nodes and dependents of an implementation type are written under
  the layer's new token, `implementation_type` (was `data_type`); no node, no dependent and no order
  moved. `impact_model/` moved by the tree-moving script with every committed tree: its
  implementation types into `data_types/implementation/`, its two mapping sets into
  `data_types/mapping_sets/`, and its interfaces' `type:` keys to `implementation_type:`.

| Row | Class |
|---|---|
| graded kind and field row order; the scaffolded rows' directories; the writer probes' paths | move (no fact changed but where a layer is listed or stored) |
| `LAYER_SPECS` `data_types`; the token in `impact_edges.json`; the two prototype fields | rename (the same layer, the same field, a new name) |
| `KIND_DESCRIPTIONS` `data_element`, `argument`; `value_type.name`; the two prototype fields' prose | **change of meaning** in prose only: the layer and the field are named as the contract now names them |

**A thirty-fifth re-recording, the second that MOVES kinds to a new layer.** On 2026-10-04, at
contract `8.0.0` (the 0.14.0 data description layers change, its second split step), the four
application kinds `enum`, `record`, `array` and `application_type` and their four member kinds left
`layer:data_types` for `layer:application_types`, stored in `data_types/application` and written
last in the contract; a data element and an argument gained `application_type`. Surgical again: each
script loaded its file, changed only the rows below and checked every graded row against the live
projection before writing.

- `registry_tables.json`, thirteen rows. `LAYER_SPECS`' `data_types` row loses the four application
  classes from `annotation.members` and takes the contract's new `description` (the implementation
  types); the five frozen application kind rows of `KIND_NAMES` (`enum`, `record`, `array`,
  `enum_literal`, `record_field`), their `KIND_DESCRIPTIONS` entries and their seventeen
  `FIELD_ANNOTATIONS` rows move, in the projection's order, to the end; `KIND_DESCRIPTIONS`
  `data_element` and `argument`, and `FIELD_ANNOTATIONS` `enum.name`, `record.name`, `array.name`,
  `data_element.type` and `argument.type` take the contract's prose; `array.element_type` and
  `record_field.type` take its prose and their `reference` names `application_types`. The two new
  `application_type` fields have no before-state and are excluded by name
  (`test_model_registry.ADDED_FIELDS_8_0`), as the layer's own `LAYER_SPECS` row is
  (`UNGRADED_LAYERS`). `PLACEMENT_FIELDS` and `COLLECTION_SPECS` are byte-identical.
- `scaffold_directories.json`: `data_types/application` joins both lists (its note says so), the
  sixth edit: `swcraft init` creates twelve model directories.
- `impact_model/` and `impact_edges.json`: the model gains two interfaces no recorded start reaches,
  `interfaces/IPhysical.yaml` (a data element whose `application_type` is `SpeedPhys`) and
  `interfaces/IQualityQuery.yaml` (an argument whose `application_type` is `Quality`), so the two new
  references are exercised (`test_model_relations.py` asserts every declared edge is). The `graph`
  was re-recorded from the live projection after these checks held: the node set is the recorded
  one; every node's dependent list is the recorded one but for `SpeedPhys` and `Quality`, which each
  gain the new interface at its head (the interfaces' pass is emitted before the application types'
  pass); the four `impact_of` starts and the four `format_impact` texts are unchanged byte for byte.
  Two node positions moved: `uint16_base` and `uint8_base` now come before `SpeedPhys` and `Quality`,
  because the record field's and the array's rows are a pass over `application_types` of their own
  after the `data_types` pass, where one pass over the old layer interleaved them by element. No
  rendered output reads the node order. Each node is written under its layer's token
  (`application_type`, `base_type`).

| Row | Class |
|---|---|
| `KIND_NAMES` / `KIND_DESCRIPTIONS` / `FIELD_ANNOTATIONS` application kind rows; the `graph` node order | move (no fact changed but the layer that holds the kind, or the pass that inserts the node) |
| `LAYER_SPECS` `data_types`; `KIND_DESCRIPTIONS` `data_element`, `argument`; the `name`, `type` and `element_type` prose rows; the two retargeted references | **change of meaning**: the layer holds the implementation types; a prototype states the side of its type; a composite member names an application type of its own layer |
| `IPhysical`, `IQualityQuery`; the two dependents they add | **addition**: no before-state |

**A thirty-fourth re-recording, and the first that MOVES a kind to a new layer.** On 2026-10-02, at
contract `8.0.0` (the 0.14.0 data description layers change, its first split step), the `base_type`
kind left `layer:data_types` for `layer:base_types`, a layer of its own stored in the two-segment
directory `data_types/base`, which the contract writes last. The kernel did not move, its declared
input did, and the edit is **surgical, not a regeneration**: a script loaded each file, changed only
the rows below and checked, before writing, that every graded row of every moved table equals the
live projection in order.

- `registry_tables.json`, seven rows. `LAYER_SPECS`' `data_types` row loses
  `swcraft_tools.schema.BaseDataType` from `annotation.members` and takes the contract's new
  `description` (ten kinds); the `base_type` row of `KIND_NAMES`, its `KIND_DESCRIPTIONS` entry and
  its seven `FIELD_ANNOTATIONS` rows move, unchanged in order among themselves, to the end, where
  the projection now lists the kind; `KIND_DESCRIPTIONS` `base_type` and `value_type` and
  `FIELD_ANNOTATIONS` `base_type.name`, `base_type.kind` and `value_type.base` take the contract's
  prose, and `value_type.base`'s `reference` names `base_types`; `base_type.kind`'s `immutable` is
  `true` where it was `null` (below). Every other `unit` and `immutable` is unchanged. `PLACEMENT_FIELDS` and `COLLECTION_SPECS` are byte-identical; the row counts of every
  table are unchanged.
- `scaffold_directories.json`: `data_types/base` joins both lists (its note says so), the fifth
  edit and the third that changes the count: `swcraft init` creates eleven model directories.
- `writer_paths.json` and `writer_refusals.json`: `create_data_type`'s probe is a base type, so its
  two targets, its accepted baseline and its two `collisions` seeds and the suffixed path name
  `data_types/base/` where they named `data_types/`. Not one verdict, exception type, message
  prefix or `reason` moved.

| Row | Class |
|---|---|
| `KIND_NAMES` `base_type`; the base type's `FIELD_ANNOTATIONS` rows; the `writer_*` paths | move (no fact changed but the layer that holds the kind) |
| `LAYER_SPECS` `data_types` (`annotation.members`, `description`) | **change of meaning**: the layer holds ten kinds, and a base type is an element of `base_types` |
| `KIND_DESCRIPTIONS` `base_type`; `FIELD_ANNOTATIONS` `base_type.name` | **change of meaning**: a base type is unique within its own layer and may share its name with a type of another |
| `FIELD_ANNOTATIONS` `base_type.kind` | **change of meaning**: an ordinary literal, no longer the discriminator, so the derivation answers it mutable; the contract keeps it `immutable: true` as it was declared, and the row carries the projection's one override (`immutable: true` where the derivation used to agree and the column was `null`) |
| `KIND_DESCRIPTIONS` `value_type`; `FIELD_ANNOTATIONS` `value_type.base` (`description`, `reference`) | **change of meaning**: the `base` names a base type, resolved in `base_types` alone, and may share the value type's own name |

The `base_types` layer's own `LAYER_SPECS` row has no before-state and is not written into this
file: it is excluded by name (`test_model_registry.UNGRADED_LAYERS`), as `mapping_sets`' was.

**A twenty-second re-recording, and the second that ADDS a writer.** On 2026-09-21, at contract
`7.0.0`, `writer_paths.json` and `writer_refusals.json` moved, because
`writer/mutations.py` gained `create_mapping_set` with `layer:mapping_sets` — the eleventh public
`create_*`, and the first added since `compositions/` gained one. It is a
**recording** and not a translation, for that change's reason: the function is new, so the probes were
run against it rather than a verdict being carried over. What was added, and nothing else:

- `writer_paths.json`: one `targets` row, after `create_data_type`'s two and before
  `create_hardware_element`'s, which is the module's own order. Eleven composed targets become
  **twelve**, and ten public `create_*` become **eleven**. Its `site` is
  `mutations.py:create_mapping_set`, the symbolic form `create_hardware_element`'s row already
  uses: every other row's `site` is the original recorder's line number and is a record of where
  the join site was, so none of them was renumbered here.
- `writer_refusals.json`: one `functions` entry (`accepted_baseline` through `identity_over_long`,
  seven cases) and one `collisions` entry (the two collision-suffix cases). `functions` goes from 10
  entries and 75 cases to **11 and 82**, `collisions` from 10 and 20 to **11 and 22**.
- **Neither `filesystem_dependent` count moved**: `control-character-in-a-component` stays **4** and
  `over-long-component` stays **5**. A mapping set's name is an emitted `DATA-TYPE-MAPPING-SET`
  `SHORT-NAME`, so `require_identifier` refuses both probes as a `ValueError` before either reaches
  the filesystem — `create_composition`'s case exactly, and the reason its entry declares neither
  property either.

Checked before the recorder wrote anything, and it refused to write unless each held:

- neither file already carried a `create_mapping_set` row, in any of the three lists;
- the set of public `create_*` the module exposes minus the functions `writer_paths.json` names is
  **exactly** `{create_mapping_set}` — so the recording closes the gap and opens no other;
- the baseline call really writes `mapping_sets/ProbeMappingSet.yaml` against the recorded model,
  every probe but the baseline is refused, and every refusal is a `ValueError`;
- afterwards, with the new row filtered out of each list, `targets`, `functions` and `collisions`
  keep their functions in the same order and every surviving row re-serializes byte-identically to
  its old self; every other top-level key is equal; and the one key added to each file was a note
  for this recording, last (no such key remains; see `writer_refusals.json`'s `note`).

The recorded messages are the refusal's first 200 characters, which is the bound the original
recorder used for ten of the eleven entries and which the replay reads with `startswith`. The
`collisions` reasons open with a fixed prefix naming the rule they record —
the store keeps every name — and `test_writer_mutations_storage.py` asserts that prefix over every
row.

| Row | Class |
|---|---|
| `writer_paths.json` `targets` `create_mapping_set`; `writer_refusals.json` `functions` and `collisions` `create_mapping_set` (seven and two cases) | **addition**: the function is new with its layer, so there is no before-state to compare against — the compositions writer's class, inverted from contract `6.0.0`'s removal |
| every other row of both files | unmoved, asserted byte for byte after the write |

**A twenty-third re-recording, and the first of `impact_edges.json` whose MODEL moved twice in one
contract.** On 2026-09-21, at contract `7.0.0`, `impact_edges.json` was
re-recorded, because `impact_model/` moved under it. Two edits, with different owners:

- **The model migration**: `SpeedRecord.fields` and
  `SpeedHistory.element_type` name the **physical** view now (`SpeedPhys`, `Quality`) where they
  named `Speed` and `uint8`, because a composite's members are typed by an application type since
  the bump, and `mapping_sets/SpeedMapping.yaml` pairs the physical views with the implementation
  types that store them;
- **this task's one addition**: `swc/shared/Shared.yaml` declares `data_type_mappings:
  [DiagMapping]`, and `mapping_sets/DiagMapping.yaml` is a set with **no** pairs. That is the
  fixture's whole reason for existing: `test_model_relations.py` asserts that the model exercises
  every declared edge, and `field:swc.swc.data_type_mappings` was the one edge contract `7.0.0`
  left unexercised. The carrier is a set with no pairs **on purpose** — a set the `Speed` walk
  reaches would put every component naming it into that walk, and `test_query.py`'s two
  hand-written level-by-level order pins out of `Speed` would have to be re-derived from the code
  they grade. `SpeedMapping` beside it carries the pairs, so the layer's two reference fields are
  exercised too.

`Shared.yaml`'s **description is deliberately unchanged**, and the file says why beside the field: a
component's description is drawn into `ibd-` and `overview-`, and rewording it moved six diagram
files — caught by `test_application_types_byte_identity.py`, which was built for exactly that
class. The note lives in `DiagMapping.yaml` instead.

The recorder (scratch, not committed) ran these checks against the file as it stood at `92ee3f97`
and refused to write unless all held. They held:

1. every node of the old `graph` is still a node, in the same relative order; three are added
   (`data_type SpeedPhys`, `data_type Quality`, `mapping_set DiagMapping`), 19 → **22**;
2. every surviving node's dependent list is an ordered **subsequence** of its new one, **but for
   three rows named in advance** — `uint8 -> SpeedRecord`, `Speed -> SpeedRecord` and
   `Speed -> SpeedHistory`, exactly the three the migration re-points — and the set actually
   removed is asserted to equal that list, so a fourth removal would have been a refusal;
3. all four `impact_of` starts keep their layer grouping and each group's rows in order;
4. all four `format_impact` texts are **byte-identical** — `SpeedSensor`, `Shared`, `RAM0` and
   `CORE0` reach no data type and no mapping set, which is why the one addition costs them nothing.

| Row | Class |
|---|---|
| `graph` nodes `SpeedPhys`, `Quality`, `DiagMapping` | **addition**: no before-state; the kinds and the layer are the bump's |
| `graph` dependents `uint8 -> SpeedRecord`, `Speed -> SpeedRecord`, `Speed -> SpeedHistory` | **removal**, named in advance: the migration re-pointed the two composites onto the physical view |
| `graph` dependent `DiagMapping -> Shared` | **addition**: the relation this task's fixture edit exercises |
| the four `impact_of` starts and the four `format_impact` texts | **unmoved**, asserted byte for byte |

Two recorded oracles over the same model were **edited, not re-recorded**, each after the same
comparison and each saying so beside itself: `test_query.py::_FROZEN_DIAMOND_REPORTS` (`Speed`,
`float32_base`) moved three lines of each report and no other — two removed by the migration, one
(`mapping_set SpeedMapping`) added in their place — and
`test_query.py::_RECORDED_NARROW_FORMS`' one `Speed` row, whose old digests were **reconstructed**
from today's result with those three rows un-applied and matched the record exactly before the new
pair was written.

**A twenty-ninth re-recording, an addition to `impact_model/` and nothing else.** On 2026-09-26,
at contract `7.8.0`, `impact_edges.json`'s `graph` was re-recorded because the
model gained three data types: `test_model_relations.py` asserts that the model exercises every
declared edge, and the contract's three new references — an implementation record element's `type`,
an implementation array's `element_type` and a pointer's `target` — were unexercised. The carrier is
one chain, `data_types/SpeedFrame.yaml` (an implementation record whose elements name `uint16` and
`uint8`), `SpeedFrames.yaml` (an implementation array of `SpeedFrame`) and `SpeedFramesPtr.yaml` (a
pointer to `SpeedFrames`), built **so that no recorded start reaches it**: nothing names the chain,
and `Speed` is not in it, so `test_query.py`'s walks out of `Speed` do not move.

The recorder (scratch, not committed) refused to write unless every check held. They held:

1. every node of the old `graph` is still a node, in the same relative order; two are added
   (`data_type SpeedFrame`, `data_type SpeedFrames` — the pointer has no dependent, so it is no
   node);
2. every surviving node's dependent list is an ordered subsequence of its new one, and exactly two
   gained a row, each at its end: `uint16 -> SpeedFrame` and `uint8 -> SpeedFrame`;
3. all four `impact_of` starts and all four `format_impact` texts are unchanged.

| Row | Class |
|---|---|
| `graph` nodes `SpeedFrame`, `SpeedFrames` | **addition**: no before-state; the kinds are the bump's |
| `graph` dependents `uint16 -> SpeedFrame`, `uint8 -> SpeedFrame` | **addition**, appended: the emission plan walks the new rows last |
| the four `impact_of` starts and the four `format_impact` texts | **unmoved**, asserted byte for byte |

`test_query.py`'s layer-slice pins over the same model were **edited**: the `data_types` slice gains
the three names at its end, in the kind rank the contract declares, and its page total moves from 12
to 15. `impact_model/` generates no ARXML at either version (a `service` component is refused before
any data type is emitted) and its RST and diagrams read no data type, so the byte-identity records
over it are not moved by the three files.

**A twentieth re-recording, and the first whose VALUE SHAPE moved.** On 2026-09-17 (contract
`6.0.0` unchanged) `impact_edges.json` was re-recorded, because neither its model
nor its declared input moved but the value it records did. Until then the reference graph kept, for
each `(node, dependent)` pair, the **first** detail string written and dropped the rest, so a
component reached through three ports on one interface named one of them, and a connector reached
through its endpoint named nothing. Since then each pair holds every distinct detail of its edges as
a tuple of `EdgeDetail(kind, value)` in emission order, and the connector endpoint rows carry the
endpoint as kind `endpoint`, its type and its `port` joined by `.`. And the walks, which dropped an
edge into an element they had already reached, now add that edge's details to the element's row —
the rows and their order are what they were. So every recorded row — in `graph` and in `impact_of` —
now carries two keys where it carried one: `detail`, the **rendered** text (what the old string was),
and `details`, the structured list. `model`, the four starts and the file's key order are unchanged.

**Amended within the same re-recording, not a twenty-first.** The file was first re-recorded
with the walks unchanged and the endpoint as a bare `port`; a check then
found that the connector lines of the unbounded report mostly named no port (a connector reached
first from the composition it is drawn inside kept that edge's empty detail), changed the walks and
the endpoint's kind, and re-recorded the file again before either recording reached `main`. The
argument below is the one the second recorder checked, against the file as it stood at `71616892` —
the state before this re-recording — and not against the first recording.

The argument was written before any code moved, and the recorder — a scratch
script, not committed, like every recorder of this directory — refused to write unless each check
held against the file as it stood at `71616892`:

- **node order and dependent order are untouched**: the 19 `graph` nodes, their 44 dependents and
  the 36 `impact_of` rows keep their `(layer, name)` sequence exactly, because neither change moves
  a key set — the first edge into a pair still fixes the pair's position in the map, and the first
  edge into an element still fixes its row's position in a walk;
- **every recorded non-empty `detail` survives as the first element of `details`**, rendering to the
  recorded string byte for byte. In `graph` no pair gained a second detail — `impact_model/`
  collapses no pair. In `impact_of` eight rows did, each from a later parent the walk reached:
  under `RAM0`, `VehicleTop` and `Powertrain` name both members, `Task_10ms` and `speed_cycle` both
  runnables; under `CORE0` the same four, `Task_10ms` reading `runnables SpeedSensor_Run,
  SpeedControl_Run; core CORE0` (kinds in the rendering table's order, so the recorded `core CORE0`
  is first in `details` and last in the text);
- **an empty `detail` either stays empty or gains `endpoint` details**, only on a `connector` row,
  and only with an owner and a port the connector's own file declares on one of its endpoints. Four
  `graph` rows gained one, each owned by that node: `swc SpeedSensor -> connector Sensor_to_Control
  (via port SpeedSensor.pOut)`, `swc SpeedControl -> … (via port SpeedControl.rIn)`, `composition
  Diagnostics -> connector Diag_to_Powertrain (via port Diagnostics.pDiag)` and `composition
  Powertrain -> … (via port Powertrain.rDiagIn)`. `composition Powertrain -> connector
  Sensor_to_Control` and `composition VehicleTop -> connector Diag_to_Powertrain` stay empty: those
  edges are the connector's own `composition` field, which names where it is drawn and no port;
- **every recorded `format_impact` line is byte-identical or extended**, and none is added, removed
  or reordered. Fifteen lines were extended: `Sensor_to_Control (via port SpeedSensor.pOut)` and
  `Diag_to_Powertrain (via port Powertrain.rDiagIn)` under `SpeedSensor`; `Diag_to_Powertrain (via
  port Diagnostics.pDiag)` under `Shared`; and under both `RAM0` and `CORE0` both connectors naming
  both endpoints, the two compositions naming both members, `speed_cycle` both steps and
  `Task_10ms` both runnables (under `CORE0` beside its `core CORE0`). A connector line names the
  endpoints whose owners the walk reached, so one connector still reads differently under two
  starts — `Diag_to_Powertrain` names one endpoint under `Shared` and two under `RAM0`.

The file was written with `json.dumps(indent=2, ensure_ascii=False)` plus a newline, which
re-serializes the old file byte-identically, so the diff is the rows and nothing else.

**No before-state, and therefore not an oracle here:** the endpoints themselves (the old record
carries `""` there, which proves only that a port was absent), the whole `details` key, the plural
rendering, and **which** later parents' details a walk adds — the old record could only show that
they were absent. The suite that grades them is
`packages/tools/tests/test_query.py` — `TestEveryDetailOfTheReachingEdges`, `TestTheRenderingTable` and
`TestTheConnectorEndpointDetail`, on in-memory models built for each shape — together with the
`TestTheRecordedReportsMoveOnlyWhereTheLogSays` baseline taken on three other fixtures before the
change. The same test module's `_FROZEN_DIAMOND_REPORTS` literals, recorded over this model, were
edited in the same commits and in the same way: first one line of each, `connector
Diag_to_Powertrain` gaining its port, and then six lines of each, every one an
extension — both connectors naming their endpoints owner first, `Powertrain` its two members, and
`Task_10ms` and `speed_cycle` the control component's runnable beside the sensor's.

| File | What it freezes | Read by (every module of `packages/tools/tests` that opens it) |
|---|---|---|
| `registry_tables.json` | the six declared tables of `editing/registry.py` — `KIND_NAMES`, `LAYER_SPECS`, `PLACEMENT_FIELDS`, `KIND_DESCRIPTIONS`, `FIELD_ANNOTATIONS`, `COLLECTION_SPECS`. The kernel deleted all six as **names**; the file keeps them as its own keys, because an oracle records the state it was taken in and renaming its keys would make the frozen comparison compare something else. `packages/tools/tests/test_model_registry.py` now reads the private projection those names were published from. | `test_model_registry.py` |
| `impact_edges.json` | the reference graph in **emission order** (per node, its dependents in insertion order, each with its rendered `detail` and its structured `details`), the ordered `impact_of` results and the rendered `format_impact` text for four starts | `test_model_relations.py`, `test_query.py` |
| `writer_paths.json` | the eleven model-root-relative targets the ten public `create_*` compose (twelve and eleven until contract `6.0.0` removed `create_system`) | `test_writer_mutations_storage.py` |
| `writer_refusals.json` | what every public `create_*` accepts and refuses today, **with the exception type** | `test_writer_mutations_storage.py`, `test_reserved_device_names.py` (its SHA-256) |
| `authorization_vocabulary.json` | `LAYER_NAMES`, `_LAYER_MODELS`, `LAYER_IDENTITY_FIELDS`, `LAYER_FIELDS`, `DELETE_ELEMENT_LAYERS` | `test_authorization_generic.py`, `test_authorization_scope.py` |
| `scaffold_directories.json` | `cli._MODEL_SUBDIRS` and the directories `swcraft init` really creates | `test_cli_scaffold_generic.py`, `test_session.py` |

`impact_model/` is the model the edge oracle was recorded over: a `tmp`-copyable fifteen-layer project
exercising **every** declared `reference` and **all three** relations, plus the shapes the oracle
has to hold — an SWC budgeting a memory no element declares (`ScratchPad`), a requirement linking
two SWCs, one name carried by two layers (`Shared`, a requirement since contract `6.0.0` — a system
before it — and an SWC) and, since contract
`2.0.0`, a memory naming the cores it is accessible from (`RAM0` -> `CORE0`, `CORE1`), which is the
one edge whose source and target layer are the same. It is deliberately **not** validator-clean:
the undeclared memory is a `hardware.undeclared-memory` finding, and it is an edge the graph must
still emit. The "two `cpu_core`s sharing a `core`" shape (`Core0`, `Core0Mirror`) was the fourth
until the hardware layer: a task's `core` is a reference to a hardware element's identity now, so two
elements answering to one key is not a model state that exists. `writer_paths.json` and `writer_refusals.json` were recorded
against a fresh copy of the same model, which is why their prerequisites (`SpeedSensor.pOut`,
`SpeedControl.rIn`, `Task_10ms`) resolve.

Two facts about `writer_refusals.json` worth reading before rewriting anything:

- `create_swc` **accepts** a bare `.` group and a control-character group, which the guarded
  `editing.Registry.compose_path` refuses; and it lets an over-long one reach the filesystem, which
  that composer refuses before staging. That asymmetry is the reason the kernel's `compose_path` is
  pure composition and the segment guards stay in `editing/` — routing mutations through the
  guarded composer would tighten public validation.
- `identity_over_long` and `group_over_long` are 300 characters, past the filesystem's own
  component limit, so several of them are an `OSError` out of `mkdir`/`write_text` rather than a
  refusal. That is the recorded behaviour, and a rewrite must reproduce it — including the exception type.

**A later change added a declaration to that file and changed no verdict in it.** Thirteen of its rows
record a property of the *recording host's* filesystem rather than one of SWCraft — six that a
component carrying U+0001 became a real directory or file, seven that a 300-character component
failed as `ENAMETOOLONG` — and neither holds on Windows, where the first is `WinError 123`
(measured 2026-08-27) and the second has never been observed at all, because the first aborts the
replay before it. Each such row now carries a `filesystem_dependent` key naming which property, the
two properties are described in the new top-level `filesystem_dependent_properties`, and
`test_writer_mutations_storage.py` branches on both by *asking the live filesystem*
(`packages/tools/tests/support/filesystem_support.py`, a real `mkdir`, never a `sys.platform` check and never a
skip). The verdicts themselves are untouched: this file is the record of what the recorder
measured, and a row whose subject turned out to be the filesystem says so beside itself instead of
being rewritten.
