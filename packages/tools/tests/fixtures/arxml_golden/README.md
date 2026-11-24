# ARXML goldens — the first-party writer's oracle

These three files capture the **outgoing** `cogu/autosar` writer's byte-for-byte output,
recorded before a single line of `writer/arxml_generation.py` was replaced. They are the
single final acceptance oracle for the first-party ARXML writer: the replacement is correct
when `generate_arxml()` reproduces them exactly.

Every `.arxml` file here is **synthetic**: SWCraft's own writer output over the repository's own
models, with no content from any real export.

⚠ **Four exceptions now, and no more.** `composite-4.2.2.arxml` was regenerated once,
deliberately, to stop emitting XML the licensed AUTOSAR schemas reject — see
[The one deliberate divergence](#the-one-deliberate-divergence-from-the-recorded-cogu-output).
And **the compositions change regenerated both populated goldens**: the writer stopped inventing a composition
called `SwCraftComposition` and started emitting the ones the `compositions/` layer declares, so
the one composition element in each is renamed and its prototypes follow the root's declared
member order. Neither is cogu-derived evidence for its composition element any more; both are
change detectors for it. `empty-4.2.2.arxml` is byte-unchanged (an empty tree declares no
composition, where it used to declare no connector). See
[The compositions regeneration](#the-compositions-regeneration-contract-400). And **the base and
implementation types change regenerated both populated goldens again**: the writer stopped inventing a `{name}_impl` implementation type beside
every base type, and emits the base types and value types the migrated models declare. See
[The base and implementation types regeneration](#the-base-and-implementation-types-regeneration-contract-500).
And **the application types change regenerated `composite-4.2.2.arxml`**: the writer stopped inventing a `{name}_AppView` application view and a
`DATA-TYPE-MAPPING-SET` called `DataTypeMappingSet`, and emits the `application_type`s and the
`mapping_sets/` elements the migrated model declares, with the unit, conversion and range one of
them carries. `basic-4.2.2.arxml` and `empty-4.2.2.arxml` are **byte-unchanged** (neither tree
declares a composite or an application type). See
[The application types regeneration](#the-application-types-regeneration-contract-700).
The enum category, the termination handling and the writer fidelity change were re-frozen since,
each with its own section below
([The enum category regeneration](#the-enum-category-regeneration-2026-09-25-contract-751),
[The termination handling regeneration](#the-termination-handling-regeneration-2026-09-29-contract-7100),
[The writer fidelity regeneration](#the-writer-fidelity-regeneration-contract-7110)), and
[The 0.14.0 regeneration](#the-0140-regeneration-contract-800) tells the whole 0.14.0 stack's
three re-freezes as one story beside that section's pinned table.

**They are never hand-edited.** Not to fix a typo, not to add an element, not to reformat.
A golden that was edited by hand is no longer a record of anything. (The exception above was
*regenerated*, through `generate_arxml()`, after the writer was fixed — not edited.)

| Golden | Model root | Distinct tags |
|---|---|---|
| `basic-4.2.2.arxml` | `examples/basic/model` | 74 |
| `composite-4.2.2.arxml` | `packages/tools/tests/fixtures/component_exchange/model` (used **in place** — no copied fixture to keep in sync) | 111 |
| `empty-4.2.2.arxml` | an empty model tree (every ARXML-bound layer directory, all empty — `conftest.make_empty_model_root`) | 4 |

`basic`'s 74 tags are a strict subset of `composite`'s 111, so `composite` alone carries every
tag **the two golden models reach** — six of the eleven data type kinds
(`base_type`, `value_type`, `application_type`, `enum`, `record`, `array`; no golden declares a
`reference_type`, an `external_type`, an `implementation_record`, an `implementation_array` or a
`pointer`), a `mapping_sets/` element, both interface kinds, an init event, a client-server
com-spec. It is not the complete emittable set: a *provided* client-server port's
`SERVER-COM-SPEC`/`QUEUE-LENGTH` appear in no golden (no golden model provides a CS interface),
and neither does a composite nested inside another composite.
Those live in targeted tests instead — `test_arxml_golden.py::TestShapesNoGoldenReaches` and the
frozen-literal assertions in `test_arxml_datatypes_writer.py`. `basic` is kept anyway: it is the regression guard for the
`python scripts/dev.py build` reference model. `empty` pins the degenerate shapes neither
populated model can reach.

Guarded by `packages/tools/tests/test_arxml_golden.py`, which also owns `.gitignore`'s second
negation — `.gitignore:3` is `*.arxml` and the first negation is non-recursive, so without
`!packages/tools/tests/fixtures/arxml_golden/*.arxml` these files are silently never committed and
`git add` reports no error.

## Regenerating

Only when an *intentional* output difference has been accepted. Record the difference in the
change's plan **before** regenerating, or the oracle quietly becomes a snapshot of whatever the
writer happens to do.

⚠ **"Accepted" means accepted in writing, in the plan, before the run.** Every plan that can
change ARXML output carries a `Qualification Impact` section; that is where a change says
whether it intends an output difference. A pure migration says `None` there and has
no such difference to accept at all. So:

> A failed golden is a defect in the change, never an approved one, unless the plan in hand says
> in writing that it authorizes an ARXML semantic change — and until it does, the goldens are not
> to be regenerated. Absent that authorization a diff means the change is wrong, not that the
> expectation is stale.

That sentence is carried verbatim by `packages/tools/tests/test_arxml_golden.py` as well.

### ⚠ Regenerating after a *model* change

Read this before touching `examples/basic/model/` or
`packages/tools/tests/fixtures/component_exchange/model/`.

Two of the three goldens are generated from **live model trees that the product keeps
changing**. Any edit to either tree — a new SWC, a new data type, a renamed port — makes the
byte test fail, and the only way to make it pass again is to regenerate.

**Regenerating for that reason destroys what the golden is.** These files record the *former*
`cogu/autosar` writer's output, captured before a line of `writer/arxml_generation.py` was
replaced. That library is uninstalled and re-adding it is guarded
(`test_version_policy.py::TestAutosarIsGone`), so a regenerated golden is produced by the very
writer it is supposed to grade: from that moment it is a self-snapshot, useful only as a
change-detector, and the cross-implementation evidence for the affected model is gone for good.

So, in order of preference:

1. **Do not change these two model trees** to exercise something new. Add a throwaway model
   root in the test instead — `make_empty_model_root()` plus a few YAML files, the shape
   `test_arxml_datatypes_writer.py`'s `nested_composites` fixture uses.
2. If the change is unavoidable (the reference model genuinely gains a feature), regenerate,
   and say plainly in the commit message that the affected golden is no longer a cogu-derived
   record. Then check that the shapes the change touches are covered by a **frozen-literal**
   assertion somewhere, since the golden can no longer be an independent oracle for them.
3. Never regenerate to make an unexplained diff go away. An unexplained diff is the finding.

From the repository root, with the repo-local venv:

```sh
.venv/bin/python -m swcraft_tools.cli generate arxml \
    --project examples/basic/swcraft.yaml
# then copy examples/basic/dist/model.arxml -> basic-4.2.2.arxml
```

The composite and empty goldens have no manifest of their own; regenerate them through the
boundary function directly:

```python
from swcraft_tools.writer import generate_arxml

G = "packages/tools/tests/fixtures/arxml_golden"
generate_arxml("examples/basic/model", f"{G}/basic-4.2.2.arxml")
generate_arxml("packages/tools/tests/fixtures/component_exchange/model", f"{G}/composite-4.2.2.arxml")
# empty: create a tmp dir holding every ARXML-bound layer directory, all empty
generate_arxml("<that tmp dir>", f"{G}/empty-4.2.2.arxml")
```

`packages/tools/tests/conftest.py` exposes `make_empty_model_root(base)` for the third one, and
`GOLDEN_MODEL_ROOTS` maps the two populated golden names to their model roots.

## Slicing a golden in a test

`conftest.golden_slice(text, short_name, tag=None)` returns the element whose direct
`SHORT-NAME` child is `short_name`, dedented by its own indentation, so a fragment emitted at
the top of a fresh sub-document compares equal to the same section nested six levels deep in a
whole file. Sections are addressed **by name, never by line number**, so an unrelated golden
change cannot silently retarget a test. Pass `tag=` when one `SHORT-NAME` is carried by more
than one element (an SWC and its `SW-COMPONENT-PROTOTYPE`, for instance); an ambiguous lookup
raises rather than picking the first match.

## The cogu artifact inventory

Output artifacts the library invented, which the first-party writer must reproduce
deliberately rather than by accident. Several are load-bearing for code that reads the output
back. Read from the two populated goldens; **the default is keep**: a row is kept while it has a
live consumer, and a row that is dropped or changed names what that breaks.

| # | Artifact | Observed form | Consumer | Decision |
|---|---|---|---|---|
| 1 | ⚠️ **Retired by the base and implementation types change.** The row was *a `{base}_impl` `IMPLEMENTATION-DATA-TYPE` per primitive*: `float32` → `SW-BASE-TYPE float32` + `IMPLEMENTATION-DATA-TYPE float32_impl`. The writer invents no implementation type any more: a `base_type` emits one `SW-BASE-TYPE` and nothing else, and a declared `value_type` emits its own `IMPLEMENTATION-DATA-TYPE` | `float32_base` (a `base_type`) → `SW-BASE-TYPE float32_base`; `float32` (a `value_type` over it) → `IMPLEMENTATION-DATA-TYPE float32` with `BASE-TYPE-REF DEST="SW-BASE-TYPE"` to `/DataTypes/float32_base` and `SW-CALIBRATION-ACCESS NOT-ACCESSIBLE` | `arxml_import/registry.py` twin-collapse and `component_exchange/import_.py`'s structural collapse, both retired with the twin | **dropped** — what it breaks is named: the two collapses above, which lose their input; every `TYPE-TREF` that named `/DataTypes/{x}_impl` names the declared value type `/DataTypes/{x}` |
| 2 | `{enum}_CompuMethod` `COMPU-METHOD` | `Gear_T` → `COMPU-METHOD Gear_T_CompuMethod` (`CATEGORY TEXTTABLE`, `COMPU-SCALE` per literal with `SHORT-LABEL`, closed `LOWER-LIMIT`/`UPPER-LIMIT` both equal to the literal value, `COMPU-CONST/VT`), emitted **before** the `APPLICATION-PRIMITIVE-DATA-TYPE` that references it | enum reduction in `arxml_import/type_mapping.py` | **keep** |
| 3 | `TMT_<runnable>` timing-event names | `TMT_SensorMain` | `arxml_reader.py` event mapping | **keep** |
| 4 | `IT_<runnable>` init-event names | `IT_SensorMain` | `arxml_reader.py` event mapping | **keep** |
| 5 | Event-name de-duplication counter | cogu appends a counter on a second event of the same kind for one runnable, and **retroactively renames the first**: `TMT_R` + `TMT_R` becomes `TMT_R_0` + `TMT_R_1`. Unreachable through the MCP/writer tool surface (`create_timing_event`/`create_init_event` allow at most one of each per runnable), so no golden exhibits it — but `generate_arxml()` reads YAML directly with no such guard, so a **hand-edited** model file reaches it | `generate_arxml()` over hand-edited YAML | **keep**, counter included — ported as `_unique_event_names()` and pinned against the live cogu path by `test_arxml_components_writer.py` |
| 6 | `<swc>_InternalBehavior`, **nested** in `APPLICATION-SW-COMPONENT-TYPE/INTERNAL-BEHAVIORS` | `SensorSwc_InternalBehavior` | reader traversal; byte equality | **keep** |
| 7 | `<swc>_Implementation`, a **package-level sibling interleaved after each SWC** (not collected at the end) | `SWC-IMPLEMENTATION SensorSwc_Implementation` with `BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR"` | reader traversal; byte equality | **keep** |
| 8 | Provide-first port order in `PORTS`, declaration order preserved **inside** each group | fixture declares `pSpeed, pPose, rCalib`; output is `pSpeed, pPose, rCalib` — provide-first, *not* alphabetical | `component_exchange/divergence.py`'s `projected_swc` applies a stable provide-first sort (`sorted(swc.ports, key=lambda p: p.direction != "provide")`) so that authored order — which the file cannot carry — is normalized out of **both** sides of the round-trip comparison; the same fact is stated as prose in that module's docstring. The citation is written by symbol, never by line | **keep** |
| 9 | ⚠️ **Retired by the small fidelity items change (contract `7.11.0`, [The 0.14.0 regeneration](#the-0140-regeneration-contract-800)).** The row was *`PORT-API-OPTIONS` sorted by port `SHORT-NAME` case-insensitively* (`sorted(provide + require, key=name.lower())`, a different order from `PORTS`). The writer writes no `PORT-API-OPTIONS` any more: the model states no port API option, and an absent option and one with both features off have one effect (`[SWS_Rte_02613]`, `[SWS_Rte_03799]`, `[SWS_Rte_07100]`) | the same SWC emitted `pPose, pSpeed, rCalib` there; now no container at all | none in the product: the order survives only in the re-freeze harness, whose `port-api-options` reversal (`fidelity_strip_support.restore_port_api_options`) rebuilds the old container in the old order so the byte records still compare | **dropped** — what it breaks is named: byte equality with the recorded cogu output for every behaviour of a component with a port, which the harness reverses; a source option that states anything but both features off is a line of the import report |
| 10 | ⚠️ **Changed by the base and implementation types change.** The row was *`NATIVE-DECLARATION` defaulting to the type's own name*. The writer derives none any more: it emits `NATIVE-DECLARATION` exactly as the base type declares it and refuses a base type that declares none, which `check-model` reports | `float32_base` → `float32` (the fixture declares it) vs. `uint32_t` (`examples/basic` declares it) | none on the read side: `component_exchange/import_.py` carries the declared value as written. It used to reset a value equal to the type's own name to empty, which only mirrored the default; once the default was gone that reset dropped a real declaration, so it was removed with it | **changed** — emitted as declared, never derived; the default and its import-side reset are both retired |
| 11 | Default sender com-spec | `NONQUEUED-SENDER-COM-SPEC` with `DATA-ELEMENT-REF`, `HANDLE-OUT-OF-RANGE NONE` (written since the writer fidelity regeneration: multiplicity 1, Table 4.67) + `USES-END-TO-END-PROTECTION false` — the silent values. Since the same regeneration a component port's `com_specs` entry replaces them attribute by attribute, and a `queued` data element takes a `QUEUED-SENDER-COM-SPEC` with the same two; no golden declares either | RTE semantics read by the customer toolchain | **keep** — classified by the writer fidelity regeneration: `HANDLE-OUT-OF-RANGE NONE` and `USES-END-TO-END-PROTECTION false` are the *silent value of a multiplicity-1 attribute*, a SWCraft addition that asserts no mechanism (no default clause exists) |
| 12 | Default receiver com-spec | `NONQUEUED-RECEIVER-COM-SPEC` with `DATA-ELEMENT-REF`, `HANDLE-OUT-OF-RANGE NONE`, `USES-END-TO-END-PROTECTION`, `ALIVE-TIMEOUT 0`, `ENABLE-UPDATE false`, `HANDLE-NEVER-RECEIVED false`, `HANDLE-TIMEOUT-TYPE NONE` — **in that order**. The writer fidelity regeneration added the two `HANDLE-*` attributes (multiplicity 1, Tables 4.60 and 4.61) and removed `FILTER/DATA-FILTER-TYPE ALWAYS`, which `[constr_1044]` allows on an integer base type only. These are the silent values: a component port's `com_specs` entry replaces them attribute by attribute, and a `queued` data element takes a `QUEUED-RECEIVER-COM-SPEC` with the first two and its entry's `QUEUE-LENGTH` (none on a composition port, which then carries no wrapper when no com-spec is left); no golden declares either | as above | **keep**, less the `FILTER` — classified by the writer fidelity regeneration: the `FILTER ALWAYS` was a **defect** (`[constr_1044]`: a data filter applies to an integer base type only) and is **dropped**, which loses nothing, the model holding no filter; every other attribute is the *silent value of a multiplicity-1 attribute*, a SWCraft addition that asserts no mechanism — the receiver's `HANDLE-OUT-OF-RANGE` included, which the measured sources state on no receiver |
| 13 | Default client com-spec | one `CLIENT-COM-SPEC` **per operation** of the required CS interface, in the interface's own operation order, each with `OPERATION-REF DEST="CLIENT-SERVER-OPERATION"`. Both goldens' CS interfaces have a single operation, so the *cardinality* is not pinned by a golden — it is pinned by a targeted test instead (`TestShapesNoGoldenReaches`) | as above | **keep** — classified by the writer fidelity regeneration: a *SWCraft addition that asserts no value* — `ClientComSpec` has no attribute but its operation reference (Table 4.73), so the element restates what the port already implies |
| 13b | Default **server** com-spec | one `SERVER-COM-SPEC` per operation, with `OPERATION-REF` + `QUEUE-LENGTH 1`, on a **P**-port over a client-server interface. **Neither golden contains this** — no golden model provides a CS interface — so `SERVER-COM-SPEC` and `QUEUE-LENGTH` are two emitted tags outside the 111 (see "Reachable but not in the goldens" below) | RTE semantics read by the customer toolchain | **keep** — classified by the writer fidelity regeneration: a *SWCraft addition that is a behaviour* — `queueLength` has multiplicity 1 and no default (Table 4.74), and 1 rejects a request while an earlier one is processed; it is the smallest value the standard admits and the value every measured source states |
| 14 | ⚠️ **Retired with row 9.** The row was *the `PORT-API-OPTION` body*: `ENABLE-TAKE-ADDRESS false`, `INDIRECT-API false`, `PORT-REF` with a `P-`/`R-PORT-PROTOTYPE` `DEST` | none written; the harness's reversal restores this body | byte equality, reversed by the harness | **dropped** with row 9 — the five tags `PORT-API-OPTIONS`, `PORT-API-OPTION`, `ENABLE-TAKE-ADDRESS`, `INDIRECT-API` and `PORT-REF` left both goldens |
| 15 | `IS-SERVICE false` on both interface kinds | emitted for every `SENDER-RECEIVER-INTERFACE` and `CLIENT-SERVER-INTERFACE`; `true` since the writer fidelity regeneration for an interface declaring `is_service: true`, which no golden does | byte equality; toolchain expectations | **keep** — classified by the writer fidelity regeneration: `false` is the *silent value of a multiplicity-1 attribute* (`isService`, Table 3.18, no default clause), a SWCraft addition; a declared `is_service` is written as declared |
| 16 | `SUPPORTS-MULTIPLE-INSTANTIATION false`, last child of `SWC-INTERNAL-BEHAVIOR` | after `RUNNABLES` | byte equality | **keep** |
| 17 | `SERVER-ARGUMENT-IMPL-POLICY USE-ARGUMENT-TYPE` on every CS argument **except an array-typed one** (directly, through reference types, or an `implementation_array`), which carries none: absent means `useArrayBaseType` there (Table 4.8, SWCT R4.2.2), and the old writer stated the opposite — the composite golden's `ICalibration.SetOffset.samples` lost the line in the writer fidelity regeneration | after `DIRECTION` | byte equality; toolchain expectations | **keep**, narrowed — an *equivalent default* on every other argument (Table 4.8); on an array-typed argument it stated the opposite of the default, a **defect**, fixed by omission |
| 18 | `SW-CALIBRATION-ACCESS NOT-ACCESSIBLE` | in every `IMPLEMENTATION-DATA-TYPE`'s `SW-DATA-DEF-PROPS-CONDITIONAL` **and** in every `ARGUMENT-DATA-PROTOTYPE`'s — but **not** in a `VARIABLE-DATA-PROTOTYPE`, which carries no `SW-DATA-DEF-PROPS` at all unless its data element states `sw_calibration_access` or `sw_impl_policy` (since the writer fidelity regeneration, which no golden declares): then the stated values alone | byte equality | **keep**, including the asymmetry — an *equivalent default* (`[TPS_SWCT_01559]`: the default calibration access is `notAccessible`) |
| 19 | `CATEGORY` values | `FIXED_LENGTH` (`SW-BASE-TYPE`, one per `base_type`), `VALUE` (the `IMPLEMENTATION-DATA-TYPE` of a `value_type`), an `APPLICATION-RECORD-ELEMENT`'s and an array `ELEMENT`'s the category of the type it references (`VALUE`, `STRUCTURE` or `ARRAY`, `[constr_1152]`; every member was `VALUE` until the writer fidelity regeneration, and no golden declares a composite-typed member), `VALUE` also on an enum's `APPLICATION-PRIMITIVE-DATA-TYPE` since the enum category regeneration below (it was `TEXTTABLE` until then), `TEXTTABLE` (an enum's compu method only), `STRUCTURE` (record), `ARRAY` (array). A `reference_type` emits `TYPE_REFERENCE` on its `IMPLEMENTATION-DATA-TYPE`; **neither golden declares one**, so that value is pinned by `test_arxml_datatypes_writer.py`'s frozen literals and `test_arxml_xsd_conformance.py::TestTheThreeDataTypeShapesConform` instead | byte equality; toolchain expectations | **keep** |
| 20 | `RUNNABLE-ENTITY/SYMBOL` mirroring the runnable name — ⚠️ **narrowed by the small fidelity items change (contract `7.11.0`)**: only for a runnable that states no `symbol`; a stated `symbol` is written as stated | `<SYMBOL>SensorMain</SYMBOL>` (no committed golden model states a `symbol`) | byte equality | **keep**, for a runnable stating no `symbol` |
| 21 | ⚠️ **Retired by the writer fidelity regeneration.** The row was *the millisecond timing grid*: `round(x * 1000)` on write and `/1000` on serialize, so a sub-millisecond value was quantized. `PERIOD` and `MINIMUM-START-INTERVAL` are now written exactly, through `format_float` | `min_start_interval: 0.005` → `MINIMUM-START-INTERVAL 0.005`; `period: 0.02` → `PERIOD 0.02` — the same bytes as before for every committed value, each a whole number of milliseconds, so no golden moved; a sub-millisecond value such as `0.0004` is written as itself | none: `component_exchange/import_.py`'s quantizing projection was deleted with the grid | **dropped** — what it breaks is named: a sub-millisecond difference on `import component` is a divergence now, where the projection hid it |
| 22 | Array element wrapper | `ELEMENT` named `Element`, with `TYPE-TREF`, `ARRAY-SIZE-SEMANTICS FIXED-SIZE`, `MAX-NUMBER-OF-ELEMENTS`; the enclosing `APPLICATION-ARRAY-DATA-TYPE` also carries an **empty** `SW-DATA-DEF-PROPS/SW-DATA-DEF-PROPS-VARIANTS` | byte equality | **keep** |
| 23 | ⚠️ **Half retired by the compositions change.** The row was *the fixed composition name `SwCraftComposition`, with an empty `PORTS` element*. The writer invents no composition any more — one is emitted per element declared in `compositions/`, under its own `SHORT-NAME` — so only the `PORTS` half survives: the element is emitted even when the composition declares no boundary port | `<PORTS>` / `</PORTS>` on two lines | byte equality | **keep** (the `PORTS` half). The name half is **dropped**, and what it breaks is named: nothing, because no code keyed on the literal any longer — `validator.RESERVED_COMPOSITION_NAME`, `mutations.check_reserved_swc_name`, `arxml_import/reconcile.reserved_swc_names` and `component_exchange/name_policies.reserved_name_violations` (then in `import_.py`): none of them holds it any more |
| 24 | `PROVIDER-IREF`/`REQUESTER-IREF` carry **no** `DEST` | their children `CONTEXT-COMPONENT-REF` / `TARGET-P-PORT-REF` / `TARGET-R-PORT-REF` all do | `arxml_reader.py`; byte equality | **keep** |
| 25 | `CONTEXT-COMPONENT-REF` points **into the composition** while `TARGET-*-PORT-REF` points at the **component type**'s port (`/ComponentTypes/<type>/<port>`) | two different base paths in one connector. ⚠️ The compositions change made the split **wider**: the context is `/ComponentTypes/<composition>/<member>`, where the member is the one of the endpoint's type inside the connector's own composition — so the two paths differ by more than a prefix whenever a member's name differs from its type's | `arxml_reader.py` connector flattening | **keep** |
| 26 | The empty-element form: **nothing is ever self-closed** | zero occurrences of `/>` in any golden, while `lxml` self-closes by default | byte equality | **keep** — this is the single easiest way for the replacement to diverge |
| 27 | XML prolog and root attributes | `<?xml version="1.0" encoding="utf-8"?>` (lowercase `utf-8`), then `AUTOSAR` with `xmlns`, `xmlns:xsi`, `xsi:schemaLocation` **in that attribute order**; two-space indent; final newline | byte equality | **keep** (only the schema filename in `xsi:schemaLocation` changes for 4.3.0) |

### Degenerate shapes (rows no populated golden covers)

| # | Shape | Observed behavior | Decision |
|---|---|---|---|
| E1 | An **empty** AR-PACKAGE | emits **no** `ELEMENTS` wrapper at all — just `AR-PACKAGE`/`SHORT-NAME`. Pinned by `empty-4.2.2.arxml` | **keep**: `ArxmlDocument` must create `ELEMENTS` lazily, on first child |
| E2 | SWCs but **no connectors** | ⚠️ **Reversed by the compositions change.** It used to emit **no** `COMPOSITION-SW-COMPONENT-TYPE` and not run `check_connector_consistency()`, both gated on one `if not connectors:`. A composition is a declared element now, so the model emits the composition it declares, with its members and no `CONNECTORS` wrapper; only the **check** is still gated on the connector list. Occurs in real use: a freshly `swcraft init`-ed project, a model mid-authoring | **keep**: no populated golden reaches the shape either way, so it has its own test — `test_arxml_golden.py::TestGoldenShape::test_a_model_with_swcs_but_no_connectors_still_emits_its_declared_composition`, plus the surviving check half in `test_arxml_composition_writer.py::TestTheGateIsTheLayerNotTheConnectorList` |
| E3 | An SWC with **no ports** | still emits an **empty** `PORTS` element (open/close on two lines), not an omitted one — the opposite of E1's rule for `ELEMENTS` and of E4's for the behavior lists | **keep** |
| E4 | An SWC with **no runnables** | its `SWC-INTERNAL-BEHAVIOR` is still emitted (and so is `SWC-IMPLEMENTATION`), but `EVENTS` and `RUNNABLES` are **omitted entirely**. `PORT-API-OPTIONS` was gated on **ports**, not on runnables. ⚠️ **Since the small fidelity items change (contract `7.11.0`) no behaviour carries `PORT-API-OPTIONS`** (row 9), so ports no longer matter here: a runnable-less SWC's behaviour holds `SHORT-NAME`, `DATA-TYPE-MAPPING-REFS` when it names a mapping set, `HANDLE-TERMINATION-AND-RESTART` (since contract `7.10.0`) and `SUPPORTS-MULTIPLE-INSTANTIATION` | **keep** |

### Reachable but not in the goldens

`composite-4.2.2.arxml`'s 111 tags are the complete inventory **for the two golden models**,
not the complete set SWCraft can emit: the shapes of the five data type kinds no golden declares
are not in it, and two tags appear in neither golden because neither golden model *provides* a
client-server interface:

* `SERVER-COM-SPEC`
* `QUEUE-LENGTH`

Both are pinned by targeted tests (`TestShapesNoGoldenReaches` in
`packages/tools/tests/test_arxml_golden.py`) rather than by byte equality: a shape between
"populated" and "empty" gets a targeted test. The writer emits them
(`_emit_com_specs()`), pinned a second time by `test_arxml_components_writer.py`'s
`component_shapes` fixture — which also covers the other shapes neither golden reaches: a
multi-member interface's com-spec cardinality, a require port declared before a provide port,
mixed-case port names, two runnables, and the event-name de-duplication counter.

## XSD conformance status

**Settled.** All three goldens validate against the licensed `AUTOSAR_4-2-2.xsd`, and each
golden's model, generated at 4.3.0, validates against `AUTOSAR_4-3-0.xsd`. The assertions live
in `packages/tools/tests/test_arxml_xsd_conformance.py`; `python scripts/dev.py test-xsd` re-runs them
with `--require-autosar-xsd`, which fails rather than skips when a schema is missing.

Byte equality to these goldens and XSD validity did conflict once; the next section records how
that conflict was settled.

## The one deliberate divergence from the recorded cogu output

`composite-4.2.2.arxml` is **no longer a byte-for-byte record of the cogu writer's output.** It
was regenerated once, deliberately, to fix invalid XML the cogu writer produced.

**What was wrong.** `APPLICATION-RECORD-ELEMENT/TYPE-TREF` and an array
`ELEMENT`'s `TYPE-TREF` carried `DEST="IMPLEMENTATION-DATA-TYPE"` whenever the field or element
was typed by a primitive or an alias. Both schemas restrict that attribute to `APPLICATION-*`
subtypes:

```
attribute DEST='IMPLEMENTATION-DATA-TYPE': value must be one of
['APPLICATION-ARRAY-DATA-TYPE', 'APPLICATION-COMPOSITE-DATA-TYPE',
 'APPLICATION-DATA-TYPE', 'APPLICATION-PRIMITIVE-DATA-TYPE',
 'APPLICATION-RECORD-DATA-TYPE']
```

Identical in 4.2.2 and 4.3.0, and inherited from `cogu/autosar` — the byte-equality criterion
is what kept it alive through the writer replacement.

**The order of criteria that settled it:** (1) the official XSD, (2) the semantic round trip,
(3) byte equality with cogu. The golden exists to detect change, not to preserve a discovered
defect, and the writer must not deliberately emit invalid ARXML.

**What the writer emits now.** For each value type or reference type a record field or array element
references, an `APPLICATION-PRIMITIVE-DATA-TYPE` named `{name}_AppView` (its *application
view*) plus one `DATA-TYPE-MAPPING-SET` named `DataTypeMappingSet` tying every view to its
implementation type. The member's `TYPE-TREF` then names that view, with an `APPLICATION-*`
`DEST` — so both the attribute **and** the reference text are correct; a corrected `DEST` over
a path that still named the implementation type would have satisfied the schema while saying
something false.

Both names are reserved (`arxml_generation.APP_TYPE_SUFFIX`, `DATA_TYPE_MAPPING_SET_NAME`),
reported by `check-model` and refused by the writer, exactly as `{x}_impl` was until the base and
implementation types change retired the twin. (Before the compositions change the
list ended `… and `SwCraftComposition``; that name is no longer reserved by anything, because the
writer no longer mints it.) The suffix is `_AppView` and **not** `_app`: `_app` is the token
`arxml_import/registry.py`'s `_reserve()` mints when two data types of one layer share a bare name, so
reserving it made a successful `swcraft import model` produce a project the writer then
refused. `packages/tools/tests/test_reserved_name_policies.py` pins the two policies against each other.
The whole addition is **conditional**: a model with no record/array over
a value or reference type emits neither element, which is why `basic-4.2.2.arxml` and
`empty-4.2.2.arxml` are untouched (verified: their SHA-256 digests are unchanged).

**It still round-trips.** `arxml_import.map_data_types` already reads `DATA-TYPE-MAPPING-SET`s
(mapping-table row 5), and `component_exchange/import_.py`'s `_normalize_writer_dialect`
collapses the `{name}_AppView` pair back out — the one generated data type it still collapses,
since the base and implementation types change retired the `{base}_impl` twin and both twin collapses with it — so
`TestSemanticRoundTrip`'s Pydantic equality and the component export/import round trip are
unaffected. **Superseded by contract `7.0.0`**: the view is a declared `application_type` and the
set a declared element, so the writer invents neither and `_normalize_writer_dialect` is removed
with them; both round trips are unaffected again, this time because nothing is collapsed at all.

Shapes that are **not** affected: an enum, record or array-typed field (already an
`APPLICATION-*` type, referenced directly as before) and every interface reference (a
`VARIABLE-DATA-PROTOTYPE`/`ARGUMENT-DATA-PROTOTYPE` `TYPE-TREF` still names the implementation
type — that is not a composite member and the schemas do not restrict it).

Because `composite-4.2.2.arxml` is now writer-generated, it is a change detector for the
composite shapes rather than cross-implementation evidence for them. The shapes carry assertions
of their own: `test_arxml_datatypes_writer.py::TestNoApplicationViewIsGenerated` (no view and no
mapping set is invented), `::TestRecordEmitter`/`::TestArrayEmitter`,
`test_arxml_application_types_writer.py` (the bytes of the declared application types), plus
`test_arxml_xsd_conformance.py::TestTheCompositeShapeIsWhatTheSchemasRejected`.


## The compositions regeneration (contract `4.0.0`)

Both populated goldens were re-frozen by the compositions change, and this section is the record the
regeneration procedure above asks for.

**The authorization.** The change's `Qualification Impact` section is **not** `None` and states
in writing, before the run, that every ARXML golden changes; without it a diff would have been the
finding.

**What changed, and how it is asserted.** Not by reading a diff. The
expected difference was hand-derived from the design and committed as data —
`differences.json`, one entry per golden — **before** the writer was changed and before
anything was regenerated; the pre-change bytes are committed beside it under `differences/before/`;
and `packages/tools/tests/test_arxml_golden_differences.py` asserts that the observed difference between
the two **equals** the committed list. An unmatched difference fails a test.

The whole difference, for each populated golden, is one rename plus one reorder:

* the composition element `SwCraftComposition` becomes the root the model declares, `Root`, and
  every path built from the old name follows it — the element, its `SW-COMPONENT-PROTOTYPE`s, its
  `ASSEMBLY-SW-CONNECTOR` and the connector's two `CONTEXT-COMPONENT-REF`s;
* the prototypes change order, from the `swc` layer's read order to the root's declared `members`
  order.

No element is added or removed and no `TARGET-*-PORT-REF` moves — it has always pointed at the
type's port, never at the prototype. `empty-4.2.2.arxml` is unchanged, and its entry says so
rather than leaving the third golden unmentioned.

**Digests.** Before: `a6ef5407…d5fd14c` (basic), `2b2fde13…5fbaec2c` (composite),
`5ffcd165…565560ba` (empty). After: `0a2b7114…c07d623c` (basic), `5564f986…c4b8cb4a` (composite),
`5ffcd165…565560ba` (empty, unchanged). The full digests are pinned in `test_arxml_golden_differences.py`.

**The run was XSD-green first.** Both goldens were generated to a scratch directory, validated
against the licensed 4.2.2 **and** 4.3.0 schemas through the product's own
`arxml_validation.check_arxml()`, and only then copied into place. The rule that made
that order necessary: a golden is never frozen from a run whose XSD check failed, and `validate`
writes the file before it checks it.

## The base and implementation types regeneration (contract `5.0.0`)

Both populated goldens were re-frozen by the base and implementation types change, and this section is the record the
regeneration procedure asks for. **A regenerated golden is a change detector, not correctness
proof**: it is produced by the writer it grades. The independent evidence for the new data-type
shapes is the licensed XSDs at 4.2.2 and 4.3.0 (`test_arxml_xsd_conformance.py::TestTheThreeDataTypeShapesConform`)
and the frozen literals in `test_arxml_datatypes_writer.py`.

**The authorization.** The change's `Qualification Impact` section is **not** `None`: it states
that the change moves the emitted bytes of every model that declares a data type, and that the two
goldens declaring a type are re-frozen by regeneration only.

**What changed**, read element by element against the bytes the compositions regeneration left (`differences/after/`):

* no top-level element is added or removed, no package changes shape, and the distinct-tag counts
  stay 74 and 99;
* each `SW-BASE-TYPE {x}` is now `SW-BASE-TYPE {x}_base` — the migrated model declares a
  `base_type` of that name — and the invented `IMPLEMENTATION-DATA-TYPE {x}_impl` is now the
  declared `value_type` `IMPLEMENTATION-DATA-TYPE {x}`, whose `BASE-TYPE-REF` names `{x}_base`;
* every reference that named `/DataTypes/{x}_impl` names `/DataTypes/{x}`, and an alias's
  `BASE-TYPE-REF` to `/DataTypes/{x}` names `/DataTypes/{x}_base`;
* the `DataTypes` package lists every base type first and every value type after, where it listed
  each base type beside its twin (the writer's passes: base, value, reference);
* **one derived difference beyond the twin**: a base type that leaves `native_declaration` unset
  emits its own name (row 10), so the composite's three base types emit `NATIVE-DECLARATION`
  `float32_base`, `uint16_base` and `uint8_base` where they emitted `float32`, `uint16` and `uint8`.
  `basic`'s twelve set it explicitly and did not move. **Superseded by the re-freeze after this
  one**: the writer derives no native declaration any more (row 10), the three base types declare
  `float32`, `uint16` and `uint8`, and emit them again — `composite-4.2.2.arxml` 20 720 → 20 705
  bytes.

`empty-4.2.2.arxml` is byte-unchanged (its tree declares no data type).

**Digests.** Before: `0a2b7114…c07d623c` (basic), `5564f986…c4b8cb4a` (composite),
`5ffcd165…565560ba` (empty). After: `83929b4c…404e1564` (basic, 21 322 → 21 357 bytes),
`314e092c…be52f940` (composite, 20 690 → 20 720 bytes), `5ffcd165…565560ba` (empty, unchanged).
The full digests are the committed files' own.

**The run was XSD-green first**, as the rule above requires: all three were generated to a scratch
directory and validated against the licensed 4.2.2 and 4.3.0 schemas through
`arxml_validation.check_arxml()` before being copied into place.

**`differences/before/` was not re-frozen.** Its difference from the compositions goldens is the compositions
delta and stays what it was; because the live goldens now carry this change's delta as well, the
compositions comparison reads its after-side from `differences/after/`, the bytes that regeneration committed.

## The application types regeneration (contract `7.0.0`)

`composite-4.2.2.arxml` was re-frozen by the application types and mapping sets change, and this section is the record the
regeneration procedure asks for. **A regenerated golden is a change detector, not a correctness
proof**: it is produced by the writer it grades. The independent evidence for the new shapes is
the licensed XSDs at 4.2.2 and 4.3.0 (`test_arxml_xsd_conformance.py`) and the hand-computed
conversion in `test_arxml_application_types_writer.py`, where a physical value is worked out in
the test's own docstring from the coefficients the golden carries.

**The authorization.** The change's `Qualification Impact` section is **not** `None` (the
boundary is affected) and states that the goldens are re-frozen with each diff read and recorded.

**What changed**, read line by line against the bytes the base and implementation types regeneration left — 59 changed lines, and the
**same 59** at 4.3.0, compared line for line rather than assumed:

* `APPLICATION-PRIMITIVE-DATA-TYPE float32_AppView` → `Distance_T` and
  `APPLICATION-PRIMITIVE-DATA-TYPE Speed_T_AppView` → `Velocity_T`. The writer invented one view
  per value type a composite named; the model declares two `application_type`s instead, and their
  names are the model's, not a suffix of another element's;
* `Velocity_T` declares a unit, a conversion and a range, so three elements are **added** before
  it — `UNIT Velocity_T_Unit` with `DISPLAY-NAME km/h`, `COMPU-METHOD Velocity_T_Compu` of
  category `LINEAR` whose one `COMPU-SCALE` carries `COMPU-RATIONAL-COEFFS` with numerator
  `[0, 0.1]` and denominator `[1]`, and `DATA-CONSTR Velocity_T_Range` whose `PHYS-CONSTRS` are
  the closed interval `[0, 400]` — and `Velocity_T` itself gains the `SW-DATA-DEF-PROPS` block
  that references all three. `Distance_T` declares none of the three, emits none of them and is
  byte for byte what a `_AppView` view used to be: the presence rule is per declared field;
* the two `TYPE-TREF`s that named a view name the declared type (`Samples_T`'s `ELEMENT` and
  `Pose_T`'s `x` record element); every other `TYPE-TREF` and every `DEST` is unchanged;
* `DATA-TYPE-MAPPING-SET DataTypeMappingSet` → `PhysicalTypes`, the name the model's
  `mapping_sets/PhysicalTypes.yaml` declares. Its two `DATA-TYPE-MAP`s keep their
  implementation sides (`/DataTypes/float32`, `/DataTypes/Speed_T`) and name the declared
  application types on the other side. The maps are in the model's declared order, not the
  writer's former type order;
* **nothing else moved**: no package changes shape, no component, interface, connector,
  composition, init constant or access point differs, and no element is removed.

The distinct-tag count goes **99 → 111**. The twelve are `UNIT`, `DISPLAY-NAME`, `UNIT-REF`,
`COMPU-RATIONAL-COEFFS`, `COMPU-NUMERATOR`, `COMPU-DENOMINATOR`, `V`, `DATA-CONSTR`,
`DATA-CONSTR-RULES`, `DATA-CONSTR-RULE`, `PHYS-CONSTRS` and `DATA-CONSTR-REF` — named, not just
counted, by `test_arxml_golden.py::TestGoldenShape::test_the_twelve_tags_the_physics_added_are_named`.
`COMPU-METHOD` and the `COMPU-SCALE` wrappers are not among them: the golden's enum, with its
`TEXTTABLE` compu method, already emitted those.

**Digests.** Before: `c9a45d62…d350394` (composite, 20 705 bytes). After: `02db8ba5…4cc3805`
(composite, 22 366 bytes). `basic-4.2.2.arxml` (`83929b4c…404e1564`, 21 357 bytes) and
`empty-4.2.2.arxml` (`5ffcd165…565560ba`, 494 bytes) are **unchanged**, which is the
regeneration's byte-identity claim for a model that declares nothing it added.

**The run was XSD-green first**, as the rule above requires: the re-frozen bytes were validated
through `arxml_validation.check_arxml()` against the licensed 4.2.2 schema, and the same model's
4.3.0 output against the 4.3.0 schema, both before this file was committed. The `init_values`
fixture's 4.2.2 and 4.3.0 output — the other tree whose ARXML moved, which has no golden of its
own — was validated the same way.

**`differences/before/` and `differences/after/` were not re-frozen**, and both still carry the two
retired inventions. They are the compositions change's authorised-difference record, not live output;
regenerating either would destroy it.

**What no longer holds of this file.** It has never been a cogu-derived record for its
composition element (contract `4.0.0`) or for its data types (contract `5.0.0`), and from contract `7.0.0` it is not
one for its application types, its units, its conversions, its ranges or its mapping set either.
What it is, for those, is a change detector — and the shapes it detects are each pinned
independently by a frozen literal in `test_arxml_application_types_writer.py`.

## The enum category regeneration (2026-09-25, contract `7.5.1`)

`composite-4.2.2.arxml` was re-frozen by the enum category change, and this section is the record
the regeneration procedure asks for. **A regenerated golden is a change detector, not a correctness
proof**: the proof that the new value is right is the standard's text —
`[constr_1007]`'s table of the categories an application data type may take (table 5.8 at R4.2.2
and R4.3.0) lists no `TEXTTABLE`, and `[constr_1634]` (R22-11) pairs a `TEXTTABLE` compu method with
the application category `VALUE`.

**The authorization.** The change's `Qualification Impact` section is **not** `None` (the
boundary is affected), and the re-freeze is gated by a test rather than trusted.

**What changed: one line.** `APPLICATION-PRIMITIVE-DATA-TYPE Gear_T`'s `CATEGORY` goes from
`TEXTTABLE` to `VALUE`; its `COMPU-METHOD Gear_T_CompuMethod` keeps `CATEGORY TEXTTABLE`, and nothing
else moves. That is not read off a diff alone:
`test_enum_category_refreeze.py` rewrites the enum type's `CATEGORY` line back to `TEXTTABLE` — a
text substitution on that one line, never an XML re-serialization — and asserts the result hashes to
the digest this file had before, with exactly one substituted line for the model's one enum.

**Digests.** Before: `02db8ba5…4cc3805` (22 366 bytes). After: `8d1ff2fb…956f0a4f` (22 362 bytes,
the four characters `TEXTTABLE` is longer than `VALUE`). `basic-4.2.2.arxml` and `empty-4.2.2.arxml`
declare no enum and are **unchanged**. The distinct-tag count does not move: no tag is added or
removed.

**The run was XSD-green first**: the re-frozen bytes were validated through
`arxml_validation.check_arxml()` against the licensed 4.2.2 schema, and the tree's 4.3.0 output
against the 4.3.0 schema, before this file was committed. Neither schema constrains a category's
value, which is why the old value passed as well; the XSD run shows only that nothing else broke.

**`differences/before/` and `differences/after/` were not re-frozen**: both still carry `TEXTTABLE` on the
enum type, as the records of the compositions change they are, pinned by SHA-256 in
`test_arxml_golden_differences.py`.

## The termination handling regeneration (2026-09-29, contract `7.10.0`)

`basic-4.2.2.arxml` and `composite-4.2.2.arxml` were re-frozen by the termination handling change,
and this section is the record the regeneration procedure asks for. **A regenerated golden is a
change detector, not a correctness proof**: the proof that every behaviour must carry the element
is the standard's text — `handleTerminationAndRestart` has multiplicity 1 in
`SwcInternalBehavior`'s class table at R4.2.2 and R4.3.0 (SWCT `:22932` / `:25469`), and R22-11's
`[constr_1934]` requires it at RTE generation time.

**The authorization.** The change's `Qualification Impact` section is **not** `None` (the
boundary is affected) and names both goldens and the one line each gains per component. Contract
`7.10.0` refuses a component whose behaviour is emitted and that sets no
`handle_termination_and_restart` (`rule:swc.undeclared-termination-and-restart`), so
`examples/basic` and `fixtures/component_exchange` — our own models — declare `no_support` on every
component, a value of the standard's own enumeration, and one that claims no capability.

**What changed: one line per component, two per golden.**
`<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>` in each
`SWC-INTERNAL-BEHAVIOR`, after `EVENTS` — `ExampleSWC` and `ExampleConsumerSWC` in `basic`,
`SensorSwc` and `ActuatorSwc` in `composite`; nothing else moves. That is not read off a diff
alone: `test_termination_refreeze.py` removes exactly those lines through
`termination_strip_support.strip_added_termination` — a text substitution on the line, never an XML
re-serialization, selecting each behaviour by its component's name — and asserts the result hashes
to the digest the golden had before, with one removed line per component the model declares.

**Digests.**

| Golden | Before | After |
|---|---|---|
| `basic-4.2.2.arxml` | `83929b4c…4e1564` (21 357 bytes) | `6f88354d…e41d407b` (21 537 bytes) |
| `composite-4.2.2.arxml` | `8d1ff2fb…956f0a4f` (22 362 bytes) | `8dbcbbc8…e52a43c` (22 542 bytes) |

Each golden grows by two 90-byte lines. The distinct-tag counts go **74 → 75** and **111 → 112**
(the one new tag), and the strict-subset relation holds. `empty-4.2.2.arxml` declares no component
and is **unchanged**.

**The byte-identity records were not re-recorded.** Every record that hashes a re-frozen tree's
ARXML compares the output after `termination_strip_support.strip_recorded`, so its recorded digest and its
claim hold; `test_enum_category_refreeze.py` chains the two undos (the termination lines first,
then the enum category) and keeps its pre-change digests.

**The run was XSD-green first**: both re-frozen goldens validated through
`arxml_validation.check_arxml()` against the licensed 4.2.2 schema, and both trees' 4.3.0 output
against the 4.3.0 schema, before these files were committed (`dev.py test-xsd`, 203 passed).

**`differences/before/` and `differences/after/` were not re-frozen**: they are the records of the compositions change,
pinned by SHA-256 in `test_arxml_golden_differences.py`.

## The writer fidelity regeneration (contract `7.11.0`)

`basic-4.2.2.arxml` and `composite-4.2.2.arxml` were re-frozen by the writer fidelity change, task
by task, and this section is the record the regeneration procedure asks for. **A regenerated golden
is a change detector, not a correctness proof**: it is produced by the writer it grades. The proof
that each new value is right is the standard's text each class cites, re-read in the local
documents and XSDs; no golden is promoted to an oracle.

**The authorization.** The change's `Qualification Impact` section is **not** `None` (a
behaviour change of the default com-specs, and new emissions confined to declared values), and
states that each writer step regenerates the goldens by the rule above (never by hand) through a
harness built before the first writer change.

**How each move is asserted.** Not by reading a diff. `packages/tools/tests/support/fidelity_strip_support.py`
holds one reversal per change class: it parses a document with blank text dropped, undoes each
class, and re-serializes through the writer's own serializer, so its first test is identity — every
committed tree's pre-change output at both versions, and every golden, survived parse-and-serialize
byte for byte. `test_writer_fidelity_refreeze.py` recorded each golden's SHA-256 (`PRE_CHANGE`) and
every committed tree's at both versions (`BRANCH_POINT`) at the head that built the harness, and
asserts that the strip maps each back to it: a byte the harness does not reverse fails there. The
table below is measured, not written from memory — the same module recomputes it and fails when a
class names a golden or a tree it did not move, misses one it did, or states another count.

**What moved, per class.** Each count is the number of places the class's reversal changed in that
document; a tree's count is the same at 4.2.2 and 4.3.0 (asserted). A class that moved nothing
moved nothing because no golden's model and no recorded tree declares the field it writes from.

| Class | What the writer writes now | Change | Goldens | Trees |
|---|---|---|---|---|
| `data-description-packages` | every data type, generated form and mapping set in its layer's package under `/DataTypes` (`Base`, `Implementation`, `Application`, `MappingSets`), a private form beside its type, and every reference to one by that path; the reversal moves each package's elements back into `/DataTypes` in that order and rewrites each reference, and runs first; the count is the elements moved | data description layers | `basic-4.2.2` (24), `composite-4.2.2` (18) | `examples/basic/model` (24), `packages/tools/tests/fixtures/access_points/model` (2), `packages/tools/tests/fixtures/compatible_interfaces/model` (3), `packages/tools/tests/fixtures/component_exchange/model` (18), `packages/tools/tests/fixtures/compositions/model` (2), `packages/tools/tests/fixtures/diagrams/model` (2), `packages/tools/tests/fixtures/init_values/model` (20), `packages/tools/tests/fixtures/interface_compatibility/model` (3), `packages/tools/tests/fixtures/operation_events/model` (2), `packages/tools/tests/fixtures/safety/model` (2), `packages/tools/tests/fixtures/showcase/model` (4) |
| `descriptions` | every declared `description` as its `DESC` (`L-2 L="FOR-ALL"`, exactly as held), and a composition `title` other than its `name` as its `LONG-NAME`; the count is both together | writer fidelity | `basic-4.2.2` (9), `composite-4.2.2` (2) | `examples/basic/model` (9), `packages/tools/src/swcraft_tools/templates/vehicle-architecture/model` (2), `packages/tools/tests/fixtures/access_points/model` (6), `packages/tools/tests/fixtures/compatible_interfaces/model` (22), `packages/tools/tests/fixtures/component_exchange/model` (2), `packages/tools/tests/fixtures/compositions/model` (21), `packages/tools/tests/fixtures/diagrams/model` (12), `packages/tools/tests/fixtures/hardware/model` (4), `packages/tools/tests/fixtures/init_values/model` (4), `packages/tools/tests/fixtures/interface_compatibility/model` (15), `packages/tools/tests/fixtures/operation_events/model` (2), `packages/tools/tests/fixtures/safety/model` (9), `packages/tools/tests/fixtures/showcase/model` (7) |
| `member-categories` | an `APPLICATION-RECORD-ELEMENT`'s and an array `ELEMENT`'s `CATEGORY` is its type's (`[constr_1152]`), no longer always `VALUE` | writer fidelity | none | `packages/tools/tests/fixtures/init_values/model` (2) |
| `compu-method-units` | a `LINEAR` compu method's `UNIT-REF` names its type's own unit (`[constr_1175]`) | writer fidelity | `composite-4.2.2` (1) | `packages/tools/tests/fixtures/component_exchange/model` (1) |
| `value-type-ranges` | a bounded value type's `DATA-CONSTR-REF` and the `{name}_Range` it names | writer fidelity | none | `packages/tools/tests/fixtures/init_values/model` (1) |
| `filters` | no `FILTER`/`DATA-FILTER-TYPE ALWAYS` on a non-queued receiver com-spec (`[constr_1044]`); the count is the filters removed | writer fidelity | `basic-4.2.2` (1), `composite-4.2.2` (1) | `examples/basic/model` (1), `packages/tools/tests/fixtures/access_points/model` (4), `packages/tools/tests/fixtures/compatible_interfaces/model` (2), `packages/tools/tests/fixtures/component_exchange/model` (1), `packages/tools/tests/fixtures/compositions/model` (5), `packages/tools/tests/fixtures/diagrams/model` (4), `packages/tools/tests/fixtures/init_values/model` (13), `packages/tools/tests/fixtures/interface_compatibility/model` (2), `packages/tools/tests/fixtures/safety/model` (11), `packages/tools/tests/fixtures/showcase/model` (3) |
| `range-and-timeout-handling` | `HANDLE-OUT-OF-RANGE NONE` on every sender and receiver com-spec and `HANDLE-TIMEOUT-TYPE NONE` on every non-queued receiver com-spec (each multiplicity 1, no default) | writer fidelity | `basic-4.2.2` (3), `composite-4.2.2` (5) | `examples/basic/model` (3), `packages/tools/tests/fixtures/access_points/model` (10), `packages/tools/tests/fixtures/compatible_interfaces/model` (12), `packages/tools/tests/fixtures/component_exchange/model` (5), `packages/tools/tests/fixtures/compositions/model` (13), `packages/tools/tests/fixtures/diagrams/model` (13), `packages/tools/tests/fixtures/init_values/model` (39), `packages/tools/tests/fixtures/interface_compatibility/model` (7), `packages/tools/tests/fixtures/safety/model` (30), `packages/tools/tests/fixtures/showcase/model` (9) |
| `argument-policies` | no `SERVER-ARGUMENT-IMPL-POLICY` on an array-typed argument (Table 4.8: absent means `useArrayBaseType`); the count is the policies removed | writer fidelity | `composite-4.2.2` (1) | `packages/tools/tests/fixtures/component_exchange/model` (1) |
| `impl-policies` | a data element's stated `sw_impl_policy` as `SW-IMPL-POLICY` | writer fidelity | none | none |
| `data-element-calibration-access` | a data element's stated `sw_calibration_access` as `SW-CALIBRATION-ACCESS` | writer fidelity | none | none |
| `disabled-modes` | a timing event's `disabled_modes` as `DISABLED-MODE-IREFS` | writer fidelity | none | none |
| `queued-com-specs` | a queued data element's `QUEUED-SENDER-COM-SPEC` / `QUEUED-RECEIVER-COM-SPEC`, with a component port's `QUEUE-LENGTH` | writer fidelity | none | none |
| `is-service` | `IS-SERVICE true` on an interface declaring `is_service: true` | writer fidelity | none | none |
| `port-api-options` | no `PORT-API-OPTIONS` in a component's behaviour: the model states no port API option, and an absent option and a false/false one have one effect (`[SWS_Rte_02613]`, `[SWS_Rte_03799]`, `[SWS_Rte_07100]`); the reversal restores the old container, one option per port sorted case-insensitively; the count is the containers restored | small fidelity items | `basic-4.2.2` (2), `composite-4.2.2` (2) | `examples/basic/model` (2), `packages/tools/tests/fixtures/access_points/model` (2), `packages/tools/tests/fixtures/compatible_interfaces/model` (5), `packages/tools/tests/fixtures/component_exchange/model` (2), `packages/tools/tests/fixtures/compositions/model` (3), `packages/tools/tests/fixtures/diagrams/model` (4), `packages/tools/tests/fixtures/init_values/model` (1), `packages/tools/tests/fixtures/interface_compatibility/model` (4), `packages/tools/tests/fixtures/operation_events/model` (1), `packages/tools/tests/fixtures/safety/model` (4), `packages/tools/tests/fixtures/showcase/model` (5) |
| `value-forms` | an init value in the form its type asks for: a prototype's own value of an application primitive as an `APPLICATION-VALUE-SPECIFICATION` (`CATEGORY` `VALUE`, the type's `UNIT-REF`, one `V`), an enumeration's literal as its text (`VT`) at every level (`[constr_1221]`); a number in a member keeps the numerical form; the reversal maps each application value back to the `NUMERICAL-VALUE-SPECIFICATION` the old writer wrote, a `VT` to its scale's `LOWER-LIMIT`, keeps a member's `SHORT-LABEL` and drops `CATEGORY` and `UNIT-REF`, runs last, and refuses a `TEXT-VALUE-SPECIFICATION`; the count is the values mapped back | application value form | none | `packages/tools/tests/fixtures/init_values/model` (3) |

The `port-api-options` row is the small fidelity items change's (same contract, `7.11.0`, folded): its reversal landed before the writer change, so on the output of the time it changed nothing, and the writer change then moved every behaviour of a component with a port and nothing else. The `init` template's tree and the `hardware` tree declare no component port and did not move.

The `data-description-packages` row is the data description layers change's (written while the contract was still `7.11.0`): its reversal landed before the writer change, so on the output of the time it changed nothing.

**Timing moved no byte.** `PERIOD` and `MINIMUM-START-INTERVAL` are written exactly since the same
change (the millisecond grid is gone), and every committed timing value is a whole
number of milliseconds, so no tree and no golden moved for it and the harness has no reversal for
it. Inventory row 21 records the retirement.

**Digests.**

| Golden | Before | After |
|---|---|---|
| `basic-4.2.2.arxml` | `6f88354d…e41d407b` (21 537 bytes) | `78eeb8a5…8dd8902e` (22 173 bytes); after `data-description-packages` `427c24e1…13937293` (23 488 bytes) |
| `composite-4.2.2.arxml` | `8dbcbbc8…1e52a43c` (22 542 bytes) | `912197da…3917fc3c` (21 395 bytes); after `data-description-packages` `94d6bc9d…9b66673a` (23 021 bytes) |
| `empty-4.2.2.arxml` | `5ffcd165…565560ba` (494 bytes) | unchanged |

`basic` moved by 30 added and 3 removed lines, `composite` by 12 added and 4 removed. The
distinct-tag counts go **75 → 79** and **112 → 116**: each golden gains `DESC`, `L-2`,
`LONG-NAME`, `L-4`, `HANDLE-OUT-OF-RANGE` and `HANDLE-TIMEOUT-TYPE` and loses `FILTER` and
`DATA-FILTER-TYPE`, and the strict-subset relation holds. `empty-4.2.2.arxml` declares nothing any
class writes from and is **unchanged**. Those figures are the classes before `port-api-options`,
whose after digests were `679afb70…d30cbd58` (22 882 bytes) and `32d24231…a2e13032` (22 958 bytes);
the `port-api-options` class then removed 14 lines from `basic` and 29 from `composite` and nothing
else, and the distinct-tag counts went **79 → 74** and **116 → 111**: each golden loses
`PORT-API-OPTIONS`, `PORT-API-OPTION`, `ENABLE-TAKE-ADDRESS`, `INDIRECT-API` and `PORT-REF`.

**The byte-identity records were not re-recorded.** Every record that hashes a tree's ARXML compares
the output after `termination_strip_support.strip_recorded`, which applies the fidelity strip first
and the termination strip after it, so each recorded digest and its claim hold; the two chained
re-freeze tests (`test_enum_category_refreeze.py`, `test_termination_refreeze.py`) apply the
fidelity strip first too and keep their pre-change digests.

**The run is XSD-green.** Every committed tree the harness records is generated at 4.2.2 and 4.3.0
and validated against the licensed schema of its version by
`test_arxml_xsd_conformance.py::TestEveryRecordedTreeConforms`, beside the goldens' own class, and
`dev.py test-xsd` turns an absent schema into a failure there. The schemas show only that each
document is valid for its version; a value's meaning is the cited standard text's.

**`differences/before/` and `differences/after/` were not re-frozen**: they are the records of the compositions change,
pinned by SHA-256 in `test_arxml_golden_differences.py`, which pins no live golden.

## The 0.14.0 regeneration (contract `8.0.0`)

The section above is the pinned record: its class table and its digests are recomputed by
`test_writer_fidelity_refreeze.py`, so it stays exactly as the tests read it. This section is the
same re-freeze told once for the whole 0.14.0 stack — the writer fidelity change and the five
changes built on it (small fidelity items, data description layers, data description elements,
application value form, stated names and stub members) plus the load-once change after them — and
nothing in it is read by a test. **A regenerated golden is a change detector, not a correctness
proof**: every golden below was produced by the writer it grades. What each new value means is the
standard's text each change cites; the licensed schemas show only that each document is valid for
its version.

**Three steps, and none after.** The goldens moved in exactly three changes, each by regeneration
through `generate_arxml()` under the harness, never by hand —
`git log 480c2009..HEAD -- 'packages/tools/tests/fixtures/arxml_golden/*.arxml'` names five commits:

1. **The writer fidelity change, task by task** (contract `7.11.0`):
   - `cc64b887` — the argument policy (`argument-policies`): `composite-4.2.2` `8dbcbbc8…1e52a43c`
     (22 542 bytes) → `e0dccf8e…f0793688` (22 447 bytes), one line removed; `basic` unchanged;
   - `d17d79c4` — the descriptions (`descriptions`): `basic-4.2.2` `6f88354d…e41d407b` (21 537
     bytes) → `5384134b…23d620df` (22 803 bytes), 27 lines added; `composite-4.2.2` →
     `046a8dd5…f2ce714c` (22 677 bytes), 6 added;
   - `4e4c1158` — the compu method's unit and the classified defaults (`compu-method-units`,
     `filters`, `range-and-timeout-handling`; `member-categories` landed in the same commit and
     moved the `init_values` tree only):
     `basic-4.2.2` → `679afb70…d30cbd58` (22 882 bytes), 3 added and 3 removed; `composite-4.2.2`
     → `32d24231…a2e13032` (22 958 bytes), 6 added and 3 removed.

   The other seven classes of that change (`member-categories`, `value-type-ranges`,
   `impl-policies`, `data-element-calibration-access`, `disabled-modes`, `queued-com-specs`,
   `is-service`) moved no golden byte — no golden model declares the field they write from; the
   first two move the `init_values` tree only. Timing moved no byte (inventory row 21).
2. **The small fidelity items change** (`c2e06ee4`, the same contract, folded): no
   `PORT-API-OPTIONS` (`port-api-options`) — `basic-4.2.2` → `78eeb8a5…8dd8902e` (22 173 bytes),
   14 lines removed; `composite-4.2.2` → `912197da…3917fc3c` (21 395 bytes), 29 removed; 22 of the
   26 recorded tree outputs moved, the `init` template's and `hardware`'s did not (no component
   port). Inventory rows 9 and 14 are retired by it, row 20 is narrowed by the `symbol` it carries,
   and the no-runnables degenerate shape no longer turns on ports.
3. **The data description layers change** (`69262d85`; contract `8.0.0` opened there):
   every data type in its layer's package under `/DataTypes` (`data-description-packages`) —
   `basic-4.2.2` → `427c24e1…13937293` (23 488 bytes), 245 lines added and 235 removed, the 24
   moved elements re-indented two levels deeper; `composite-4.2.2` → `94d6bc9d…9b66673a` (23 021
   bytes), 235 added and 215 removed, 18 elements; eleven recorded trees, 2 to 24 elements each.

`empty-4.2.2.arxml` (`5ffcd165…565560ba`, 494 bytes) moved in none of them. **After step 3 no
golden byte moved**: the data description elements, the application value form, the stated names
and stub members and the load-once changes each compared 35 raw digests — the three live goldens,
the six `differences/*` copies and the 26 recorded tree outputs (13 trees × 4.2.2 / 4.3.0), a check
run outside the repository — at the end of every step; the only outputs
that moved are the `init_values` tree's two (below), and at the integration branch point
(`487259f1`) the 35 are **0 moved** against the digests recorded at the application value form's
head. The oracle-by-the-standard change (2026-10-07: the reader's number grammars, a linear
conversion's scale limits written, the duplicate-name message) moved none either: it compared the 35
at the end of each of its tasks, and at its head (`08abee33`) they are again **0 moved** against
those same digests, so this section gains no step.

**Distinct tags**, re-measured at the integration branch point: **74** (`basic`), **111**
(`composite`), **4** (`empty`), the strict-subset relation holding. Step 1 took them 75 / 112 →
79 / 116 (each golden gains `DESC`, `L-2`, `LONG-NAME`, `L-4`, `HANDLE-OUT-OF-RANGE`,
`HANDLE-TIMEOUT-TYPE` and loses `FILTER`, `DATA-FILTER-TYPE`), step 2 back to 74 / 111 (each loses
`PORT-API-OPTIONS`, `PORT-API-OPTION`, `ENABLE-TAKE-ADDRESS`, `INDIRECT-API`, `PORT-REF`), and
step 3's packages add no tag name — the tag sets are equal at `c2e06ee4` and at the branch point.

**How each move is asserted.** `packages/tools/tests/support/fidelity_strip_support.py`'s `REVERSALS` holds one
reversal per change class — **15** at the branch point: the writer fidelity change's twelve, the small
fidelity items' `port-api-options` (last when it landed), the data description layers'
`data-description-packages` (run first) and the application value form's `value-forms` (last). Each reversal
landed before its writer change, so on the output of its time it changed nothing; after it, the strip maps
every golden back to its `PRE_CHANGE` digest and every recorded tree's output at both versions back to its
`BRANCH_POINT` digest (`test_writer_fidelity_refreeze.py`), and a byte no reversal undoes fails there. The
data description elements and the stated names and stub members changes added no reversal: no recorded tree
states an element of the three new layers, a stated name or a stub's member list.

**The harness helper, and its limit.** Moving an element two package levels up re-serializes it at
another depth, and the serializer gives a childless element's closing tag the indentation of its
depth. `fidelity_strip_support._forget_empty_element_indentation` drops, only inside the subtrees
the `data-description-packages` reversal moves, that closing-tag indentation text (a newline plus
the indent unit times the depth, exactly), so a moved element re-serializes at its old depth.
Without it **5 of the 29 strip cases** (3 goldens + 13 trees × 2 versions) differed by the
closing-tag indentation of an empty `SW-DATA-DEF-PROPS-VARIANTS` and by nothing else. **It cannot
tell** a writer that changed the indentation of an empty moved element from one that did not: that
one property of the moved subtrees is outside what the strip asserts.

**The `init_values` tree's move.** The application value form change moved no golden — no golden
model holds a value specification — and **one** recorded tree, `packages/tools/tests/fixtures/init_values/model`,
at both versions, by its 3 enumeration leaves written as their text: 4.2.2 `5bf1e6dc…d851` →
`5c51c5ac…d988`, 4.3.0 `67460201…b53e` → `fcfe4cd6…b184`. `value-forms` maps each back to the
`NUMERICAL-VALUE-SPECIFICATION` the old writer wrote, a `VT` to its scale's `LOWER-LIMIT` — a lookup
across the document's `TEXTTABLE` methods, which refuses a text on no scale or on scales of
different limits — so the tree strips to its `BRANCH_POINT` digest.

**The byte-identity records were not re-recorded.** A record that hashes ARXML output compares it
through one of two strips, or through neither. The seven `test_*_byte_identity.py` modules'
tree records, and `test_enum_category_refreeze.py`'s before its own reverse map, compare the
output after `termination_strip_support.strip_recorded`, which applies the fidelity strip first.
`test_writer_fidelity_refreeze.py`'s `BRANCH_POINT`, the golden `PRE_CHANGE` digests of it,
`test_termination_refreeze.py` and `test_description_emission.py`, and
`test_handle_termination_and_restart.py`'s record before its own termination strip, compare after
`fidelity_strip_support.strip_fidelity` directly. Either way each recorded digest and its claim
hold. **Three** records go through neither strip.
`test_package_placement_writer.py`'s `NO_DATA_TYPE_BRANCH_POINT` hashes two committed trees' **raw**
output, which holds because those trees declare no data type and so gain no layer package.
`test_external_stubs_byte_identity.py`'s `WITH_A_STUB_THAT_LISTS_NOTHING` hashes raw output too,
of models whose stub lists no member (below).
`RAW_BEFORE` (`test_arxml_symbol_and_port_init_writer.py`, the small fidelity items' record) applies
its own reversals and was **narrowed, not re-recorded**, by three of them, in this order:
`flatten_data_description_packages` (`data-description-packages`), `restore_port_api_options`
(`port-api-options`, step 2) and `restore_numerical_values` (`value-forms`). This enumeration is the
ARXML-hashing calls a search for `sha256` over `packages/tools/tests/` finds; it does not reach a test that
compares two outputs with each other and records no digest (the eighth such module,
`test_safety_byte_identity.py`, is one), and the digests of committed files are
`differences/before/` and `differences/after/` below. The stated names and stub members change added a byte record of its own kind,
`test_external_stubs_byte_identity.py`'s `WITH_A_STUB_THAT_LISTS_NOTHING` (two test models at both
versions, recorded with the writer before that change): the evidence that a stub with an empty
member list writes no com-spec and no wrapper — a change detector too, not a golden.

**Not reached by the record.** The data description elements change's fixture tree
`packages/tools/tests/fixtures/data_description/model` is not a recorded tree: no byte record claims it, the
`data-description-packages` reversal refuses its three element packages by design, and the
tree-enumerating gates that would claim it exclude it by name with their reasons. The models the
application value form and the stated names changes build under `tmp_path` are named by no digest
record either.

**The XSD evidence.** Every recorded tree is generated at 4.2.2 and 4.3.0 and validated against the
licensed schema of its version (`test_arxml_xsd_conformance.py::TestEveryRecordedTreeConforms`,
beside the goldens' own class); `dev.py test-xsd`, which fails rather than skips without a schema,
grew with the stack — **240** passed after the writer fidelity record, 250 (small fidelity items,
data description layers), 254 (data description elements), 260 (application value form), **267**
(stated names and stub members, load-once) — and passes **267** at the integration branch's head.

**`differences/before/` and `differences/after/` were not re-frozen** in any step: they are the compositions change's
records, pinned by SHA-256 in `test_arxml_golden_differences.py`, which pins no live golden, and
their six raw digests are among the 35 unmoved.

**Only the re-frozen goldens moved.** The only golden files that moved are `basic-4.2.2.arxml` and
`composite-4.2.2.arxml`, each in the commit that moved it (the five above); `empty-4.2.2.arxml` and
the six `differences/*` copies never moved; every record is green through the strips at the branch
point.

**What this record does not prove.** That a value is right: a golden, a strip and a byte record are
change detectors written by the same hands as the writer, and a reversal that mirrors a writer
defect would strip it cleanly. The independent evidence is the standard's text each change quotes,
the licensed schemas (validity only, never meaning), and the input-vs-output oracle on real tool
exports held outside the repository, which judges the importer and the writer together against a
closed list rather than against a SWCraft-made expectation.
