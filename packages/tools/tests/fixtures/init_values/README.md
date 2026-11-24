# `init_values/` — the throwaway model root for the init-value oracles

`model/` is a **committed model tree used in place**, exactly the way
`component_exchange/model` is — but it is deliberately **not** a golden root. Nothing under
`fixtures/arxml_golden/` is generated from it, `GOLDEN_NAMES` and `GOLDEN_MODEL_ROOTS` do not
name it, and no `.arxml` of its output is committed anywhere. That is the golden README's own
first order of preference ("add a throwaway model root in the test instead"): a fourth golden would
be parametrized over by every module that reads the golden names, most of which it would reach
vacuously or wrongly.

The tree exists so the four init-value oracles have **one** model that carries every init-value shape
the writer can emit:

| Oracle | Module |
|---|---|
| 1 — XSD at both versions, plus the mis-placed-`INIT-VALUE` negative control | `packages/tools/tests/test_arxml_xsd_conformance.py` |
| 3 — the typed, unprojected `init` round trip | `packages/tools/tests/test_arxml_golden.py` |
| the 4.3.0 token substitution | `packages/tools/tests/test_arxml_4_3_0.py` |
| the reader and mapper reading the **writer's own** output | `packages/tools/tests/test_arxml_reader.py`, `packages/tools/tests/test_arxml_import_interfaces.py` |

The constant that names this root lives in `packages/tools/tests/conftest.py` beside
`GOLDEN_MODEL_ROOTS` — `INIT_VALUES_MODEL_ROOT` — because many modules read it: `grep -rl INIT_VALUES_MODEL_ROOT packages/tools/tests`
lists them, and more name the root by its path.

Oracle 2 — the hand-derived frozen fragments — is `packages/tools/tests/test_arxml_init_writer.py`, which
builds its own throwaway roots per test and does not read this one.

The tree passes `check-model` clean (`check_cross_layer_consistency` reports no issue), which is
what makes it a model a user could have authored: the mandatory `hardware/` layer holds exactly
one processor and one core, and `requirements/REQ-INIT-001.yaml` is satisfied by the one component
so no `trace.requirement-unallocated` warning fires.

## What each element exercises

### `interfaces/ISignals.yaml` — one sender-receiver interface, every value shape

| Data element | Type | `init` | What it is here for |
|---|---|---|---|
| `speed` | `uint8` | `7` | the `int` scalar at depth 0 — a top-level `NUMERICAL-VALUE-SPECIFICATION`, an implementation-typed prototype's own value is never an application value (`[constr_1385]`) |
| `plain` | `uint8` | *(none)* | the element **without** a value, and it sits between two that have one, so "data-element order" and "only valued elements get a constant" cannot both be satisfied by accident |
| `ratio` | `Ratio_T` | `12.5` | a **bounded** value type (`value_min`/`value_max`): the value is checked against the bounds, and a `bool`, which no bound applies to, is refused |
| `flag` | `boolean` | `true` | the `bool` row of the emission table: emitted as `1`, and read back as the `int` `1` — a recorded lossiness, asserted as that outcome rather than exempted |
| `mode` | `Mode_T` | `'ON'` | the enum row: emitted as an `APPLICATION-VALUE-SPECIFICATION` whose one `VT` is the literal's **name** (the application value form; not the literal's declared `value`, `1`), read back as that name by the direct enum resolution — a number under an enum is never read as a literal. The re-freeze harness's `value-forms` reversal maps the `VT` back to its scale's `LOWER-LIMIT`, so the recorded digests stand. The literal is `ON` **quoted**, and so is `OFF`: both are YAML 1.1 booleans unquoted, which is the YAML authoring rule |
| `huge` | `float32` | `1.0e+20` | the float whose fixed-point text carries no `.` until `format_init_number` appends one — the case a formatter without the appended `.` gets wrong |
| `negative` | `float32` | `-1.0e+20` | the same, negative, so the sign is carried through the appended `.0` |
| `tiny` | `float32` | `1.0e-7` | a float whose fixed-point text is long and fractional (`0.0000001`), never scientific notation |
| `whole` | `float32` | `100.0` | the float with no fractional digits: it must **not** come back as an `int`, which is what the appended `.0` is for |
| `bytes` | `Bytes_T` | `[1, 2]` | the array shape — `ARRAY-VALUE-SPECIFICATION/ELEMENTS`, members carrying no `SHORT-LABEL` |
| `floats` | `Floats_T` | `[1.0e+20, -1.0e+20, 1.0e-7, 100.0]` | the four float leaves again, **inside a list**, so the type-preservation claim is not only about depth 0 |
| `limits` | `Limits_T` | `{low, high, tiny, whole}` | the record shape — `RECORD-VALUE-SPECIFICATION/FIELDS`, every member labelled with its declared field name — and the four float leaves a third time, **inside a map** |
| `deep` | `Grid_T` | `[{row: {index, mode}}, {row: {index, mode}}]` | the depth-3 nesting the oracles ask for, as an **array of records of records**, with an enum leaf at the bottom. Where the other orientation is covered is the section below |

Every declared name is short on purpose. The eighth generated form's budget belongs to the
`(interface, element)` **pair** — 122 characters between them — and a fixture that came near it
by accident would make an unrelated rename fail the writer rather than this fixture's own tests.
The longest pair here is `ISignals` + `negative` = 16.

**The pair at the 122/123 boundary is a test, never a fixture file**:
`test_arxml_xsd_conformance.py`'s `_boundary_model_root` builds it through the write path, and
`TestTheJointBudgetIsTheOneTheEmitterSpends` is where the boundary is asserted.

### `data_types/` — the types those elements are typed by

`base/` holds the three base types; `implementation/` holds the value types `uint8`, `float32`,
`boolean` and the bounded `Ratio_T`; `application/` holds the application types `Count_T` and
`Sample_T`, the enum `Mode_T`, the arrays `Bytes_T`, `Floats_T` and `Grid_T` and the records
`Limits_T`, `Row_T` and `Cell_T`; `mapping_sets/PhysicalTypes` maps `Count_T`, `Sample_T` and
`Mode_T` onto implementation types. `Grid_T` is the array of `Cell_T` — and `Grid_T` → `Cell_T` →
`Row_T` are the three levels of the depth-3 shape.

## Which depth-3 shape this tree carries

The tree carries the array→record→record orientation, and it maps clean through the importer
(empty skip list, every leaf typed, the enum leaf back as its literal name). The record→array→record
orientation — a record whose field is an array of records — is **not** in this tree; the importer
maps it as well (`arxml_import/type_mappers.py`'s `_map_composites` maps a composite member before
the record that names it), and it is covered in front of the writer and the schemas:

| Where | What it proves |
|---|---|
| `packages/tools/tests/test_arxml_init_writer.py` — `Deep_T`/`Rows_T`/`Row_T` in the frozen fragments | the writer **emits** the record→array→record spelling, byte for byte, against a hand-derived expectation |
| `packages/tools/tests/test_arxml_xsd_conformance.py` — `TestARecordWithAnArrayFieldConforms` over `_record_over_array_model_root` | the licensed schemas **accept** it, at both versions, with a non-vacuity assertion that the three levels really nest |

### `swc/signals/SignalsSwc.yaml` — the one component, and why it has two ports

`pSignals` **provides** and `rSignals` **requires** the same init-carrying interface. Without
both, one of the two COM-SPEC `INIT-VALUE` positions would never reach the XSD oracle at all:
the sender's after `USES-END-TO-END-PROTECTION`, the receiver's after `HANDLE-NEVER-RECEIVED`.
The two positions are measured facts about the schemas' sequences, and the negative control in
`test_arxml_xsd_conformance.py` is the other half — a hand-built document with the receiver's
`INIT-VALUE` moved one position earlier, which both schemas refuse.

### `hardware/` and `requirements/`

`CPU` (the one processor) and `CORE0` are what the mandatory `hardware/` layer requires.
`REQ-INIT-001` was added for one reason: until contract `12.0.0` `check-model` listed
`SWC 'SignalsSwc' satisfies no requirement` as a **warning** for **any** component that named none,
and the link kept this tree free of it. That contract removed the warning; the requirement stays,
and `SignalsSwc`'s link is what keeps it from being `trace.requirement-unallocated`'s warning (a
warning, not a refusal: without the link the tree still passes, with that one line listed). `SignalsSwc` is the one component that names it. Neither layer is emitted into the
ARXML — `layer:hardware` and `layer:requirements` are both `arxmlBound: false` — and neither
carries an `init`. They are here so that `check-model` accepts
the tree, which is the property the oracles rest on: this is a model a user could author, not a
shape only a test can produce.

## Changing this tree

It is not a golden, so it may be edited — but every table above is a claim some test asserts.
Add a shape rather than repurposing one, and say in this file what the new element is for.
