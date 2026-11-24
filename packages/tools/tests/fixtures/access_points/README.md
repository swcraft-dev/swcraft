# `access_points/` — the throwaway model root for the access-point oracles

`model/` is a **committed model tree used in place**, exactly the way
`init_values/model` and `component_exchange/model` are — but it is deliberately **not** a
golden. Nothing under `fixtures/arxml_golden/` is generated from it, `GOLDEN_NAMES` and
`GOLDEN_MODEL_ROOTS` do not name it, and no `.arxml` of its output is committed anywhere.
That is the golden README's own first order of preference ("add a throwaway model root in the
test instead"): a fourth golden would be parametrized over by eleven modules, most of which it would reach
vacuously or wrongly.

The tree exists so the access-point oracles have **one** model that carries every access shape the
writer can emit:

| Oracle | Module |
|---|---|
| 1 — XSD at both versions, plus the two mis-placed-container negative controls | `packages/tools/tests/test_arxml_xsd_conformance.py` |
| 3 — the typed, unprojected access round trip | `packages/tools/tests/test_arxml_golden.py` |
| the 4.3.0 token substitution | `packages/tools/tests/test_arxml_4_3_0.py` |
| the reader and the mapper reading the **writer's own** output | `packages/tools/tests/test_arxml_reader_markers.py`, `packages/tools/tests/test_arxml_import_swcs.py` |

The constant that names this root lives in `packages/tools/tests/conftest.py` beside
`INIT_VALUES_MODEL_ROOT` — `ACCESS_POINTS_MODEL_ROOT` — because many modules read it: `grep -rl ACCESS_POINTS_MODEL_ROOT packages/tools/tests`
lists them, and more name the root by its path.

Oracle 2 — the hand-derived frozen fragments — is `packages/tools/tests/test_arxml_access_writer.py`,
which builds its own throwaway roots per test and does not read this one. Oracle 5, the
additive-delta gate, runs over `examples/basic` (`test_arxml_components_writer.py`) and reaches
only two of the four containers, which is why the class docstring there points at this root for
the other two.

The tree passes `check-model` clean (`check_cross_layer_consistency` reports no issue), which is
what makes it a model a user could have authored: the mandatory `hardware/` layer holds exactly
one processor and one core, and `requirements/REQ-ACC-001.yaml` is satisfied by both components
so no `trace.requirement-unallocated` warning fires.

## What each element exercises

### `swc/access/AccessSwc.yaml` — the component that declares, and the runnable that does not

| Element | What it is here for |
|---|---|
| `AccessMain.reads[rSignals.speed]` | the by-argument read — `DATA-RECEIVE-POINT-BY-ARGUMENTS`, `R_rSignals_speed` |
| `AccessMain.reads[rSignals.level] (by: value)` | the by-value read — `DATA-RECEIVE-POINT-BY-VALUES`, the container split that is the **only** carrier of the form. The two reads sit on **one** port, so the container and not the port is what tells them apart |
| `AccessMain.writes[pSignals.speed]` | the write — `DATA-SEND-POINTS`, `W_pSignals_speed`, `DEST` `P-PORT-PROTOTYPE` where the reads carry `R-` |
| `AccessMain.calls[rControl.Apply]` | the call — `SERVER-CALL-POINTS`, `Call_rControl_Apply`, with `TIMEOUT` `0` |
| `QuietRun` | a runnable declaring **none**, inside a **declaring** component: the byte-identical case that cannot be satisfied by "this component emits nothing" |
| the port `rSpare` | a declared port **no runnable touches** — 28.1's silence rule, witnessed: it is a port in the output and no access point anywhere |

The three lists are all on **one** runnable on purpose: their four containers then have to sit in
the schema's order between `CAN-BE-INVOKED-CONCURRENTLY` and `SYMBOL` inside one
`RUNNABLE-ENTITY`, which is the position both negative controls perturb.

### `swc/access/ServerSwc.yaml` — the component that declares nothing

It provides `IControl`, so the called operation has a server, and it declares no access list at
all. It is what makes the SWCraft access marker's **absence** part of the same generated document as its
presence: `AccessSwc_InternalBehavior` carries the `ADMIN-DATA/SDGS/SDG GID="SWCRAFT"` block and
`ServerSwc_InternalBehavior` carries no `ADMIN-DATA` at all. A marker emitted per document rather
than per component would fail here and nowhere else in the suite.

### `interfaces/` — one interface of each kind

`ISignals` is the sender-receiver interface both the reads and the write go through, and
`AccessSwc` both **requires** and **provides** it (`rSignals`, `pSignals`), which is what lets one
interface serve a read and a write with the two `DEST`s the schemas police. `IControl` is the
client-server interface with the one operation `Apply`; its argument is typed so the interface is
complete.

### `data_types/uint8.yaml` — the one type

Deliberately the whole of the layer. `arxml_import/type_mapping.py`'s `map_data_types` is
single-pass and maps records strictly before arrays, so a nested type here would risk an
unresolvable field taking its interface — and with it every access point that names one — out of
the round trip for a reason that has nothing to do with access points. That trap is a known
finding; this root avoids it by carrying no composite type at all.

### `hardware/` and `requirements/`

`CPU` (the one processor) and `CORE0` are what the mandatory `hardware/` layer requires.
Both components satisfy `REQ-ACC-001`, because `check-model` lists a requirement nothing
satisfies as a warning (`trace.requirement-unallocated`), and the links keep the tree free of it;
until contract `12.0.0` a component that named none was a warning too. Neither layer is emitted
into the ARXML — `layer:hardware` and `layer:requirements` are both `arxmlBound: false`.

## What is deliberately **not** here

Every declared name is short on purpose. Three of the fourteen generated forms are derived from a
**pair** — `R_`/`W_` give a port name and a member name 125 characters between them, `Call_` gives
122 — and a fixture that came near either budget by accident would make an unrelated rename fail
the writer rather than this fixture's own tests. The longest pair here is `rSignals` + `speed` = 13.

**The pair at the 125/122 boundary is a test, never a fixture file**:
`test_arxml_xsd_conformance.py`'s `_boundary_model_root` builds it through the write path, and
`TestTheAccessJointBudgetsAreTheOnesTheEmitterSpends` is where the boundary is asserted.

The name collision — two different pairs deriving one string — is likewise a test
(`test_validator_name_uniqueness.py`, `test_arxml_access_writer.py`) and not a shape this root
carries: a root that carried it would not pass the `check-model` every oracle above rests on.

## Changing this tree

It is not a golden, so it may be edited — but every row above is a claim some test asserts. Add a
shape rather than repurposing one, and say in this file what the new element is for.
