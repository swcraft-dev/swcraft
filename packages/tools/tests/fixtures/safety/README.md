# `safety/` — the safety acceptance model

`model/` is a **committed model tree used in place**, exactly the way `access_points/model`,
`init_values/model` and `component_exchange/model` are, and deliberately **not** a golden: nothing
under `fixtures/arxml_golden/` is generated from it, `GOLDEN_NAMES` and `GOLDEN_MODEL_ROOTS` do not
name it, and no `.arxml` of its output is committed anywhere. That is the golden README's own first
order of preference ("add a throwaway model root in the test instead").

The tree exists so that **one** model carries every case of the six coexistence rules, both
exemptions included, and the fixture cases of the placement rules — and so the byte-identity
gate has a model that declares **every safety and capability field** on every kind that can hold one: the
level on a requirement, a component and a data element, the two declared arguments, the
capability of a core and of a memory, and the level and the argument of a partition. The capability
half is why the tree carries a memory and a budget on every component.

The constant that names this root is `SAFETY_MODEL_ROOT` in `packages/tools/tests/conftest.py`, beside
`ACCESS_POINTS_MODEL_ROOT`.

## What it is, in one line

Four components, three interfaces (two sender-receiver, one client-server), three tasks, three
requirements, a processor, a core, three partitions and a memory, and the mandatory root
composition. It mints
**exactly seventeen** safety findings — eight coexistence findings and nine placement findings — and
**one** other cross-layer finding, `task.instance-in-multiple-partitions` on `TorqueLimiter` (below),
so a new finding of any other kind appearing here is this fixture telling you something changed. In particular no `hardware.*` finding: every component
budgets `RAM0` (one of them with `0`), `RAM0` lists `CORE0`, and the four budgets total 896 bytes
against a limit of 4096 × 0.5 = 2048.

## Every element, and the case it is for

### `data_types/`

| Element | Why it is here |
|---|---|
| `uint8` | the one value type every data element and the one operation argument use; carries no level and never could |
| `uint8_base` | the base type `uint8` rests on; nothing but `uint8` names it |

### `hardware/`

| Element | Why it is here |
|---|---|
| `CPU` | the one `kind: processor`; `hardware/` is a mandatory layer |
| `CORE0` | the one core all three partitions are on, **rated `B`**. Each partition declares `D`, so the partition rule reports each of them once |
| `P_Argued` | `Task_Argued`'s partition, **rated `D`** on `CORE0`, carrying a partition `coexistence` text — the memory-mixing argument. It runs `TorqueLimiter` (`D`) and `SpeedSensor` (derived `B`), so the argument is what keeps `safety.component-below-partition` silent here: the **argued** case |
| `P_Homogeneous` | `Task_Homogeneous`'s partition, **rated `D`** on `CORE0`. It runs `TorqueLimiter` alone, so nothing in it is below or above it |
| `P_Mixed` | `Task_Mixed`'s partition, **rated `D`** on `CORE0`, with **no** argument. It runs `PlainWorker` (`B`) and `DiagnosticsAgent` (derived `QM`) beside `TorqueLimiter`, so `safety.component-below-partition` reports the two lower components — `QM` beside a rated partition included. No component is above `D`, so `safety.component-above-partition` stays silent over the whole tree (its cases are `test_validator_capability.py`'s) |
| `RAM0` | the one memory, **rated `B`**, `size: 4096`, `threshold: 0.5`, `accessible: [CORE0]`. Every component budgets it, which is what keeps `hardware.missing-budget` silent under the default `enforcement: strict`. Each partition runs a component budgeting it non-zero, so `safety.partition-memory-below-level` reports it once per partition, whatever the number of components (`P_Mixed`'s three are one finding) |

### `compositions/`

| Element | Why it is here |
|---|---|
| `Root` | the root every project owes since contract `4.0.0`; each of the four components stands in it exactly once. So `TorqueLimiter`, whose runnables run in all three tasks and therefore in all three partitions, is **one** instance in three partitions, and `task.instance-in-multiple-partitions` reports it — the fixture's one finding outside the `safety.*` rules, asserted by name in `packages/tools/tests/test_validator_coexistence.py` |

### `interfaces/`

| Element | Why it is here |
|---|---|
| `ISafetySignals` | **a rated element, a lower-rated one and an unrated one on ONE interface** — which is the measured reason the level sits on the element and not on the interface. `torque` is `D`, `speed` is `B`, `raw` declares nothing and is therefore **unrated, not `QM`**: neither port rule ever compares against it |
| `IPlainSignals` | `status` is `A` and `debug` declares nothing. The low-rated element every consumer case is built on, beside an unrated one that must stay silent |
| `IDiagnostics` | the client-server interface. It declares **no data element at all**, which is exactly what a `measures` key on a port of it cannot name |

### `requirements/`

| Element | Why it is here |
|---|---|
| `REQ-SAFETY-001` | allocates `D` to `TorqueLimiter`, which **declares** `D` — so `safety.component-below-requirements` finds nothing here |
| `REQ-SAFETY-002` | allocates `B` to two components at once: one that declares nothing (so it **derives** `B`) and one that declares `B` |
| `REQ-SAFETY-003` | allocates **no** level. The component it names therefore derives `QM` — the case that separates "derived QM" from "declared QM" |

### `swc/safety/`

| Element | Why it is here |
|---|---|
| `TorqueLimiter` | **declares `D`.** Its `pSafety` provide port covers the provider rule's three silent cases in one: `torque` (`D`) is **equal**, `speed` (`B`) is **below**, `raw` is **unrated**. Its `rPlain` require port declares a measure for **another** element (`debug`, which is unrated and needs none), so `status` is still reported — the "an entry for another element" case. Its `rPlainMeasured` port declares the measure for `status` and is **silent** — the exemption. Four runnables, because a `{swc, runnable}` pair may be mapped to at most one task and this component appears in all three. Budgets **512** bytes of `RAM0`: a `D` component charging a `B` memory, the one memory finding |
| `SpeedSensor` | **declares nothing** and derives `B` from `REQ-SAFETY-002`. Provides `torque` (`D`) — the provider rule over a **derived** level, whose finding names the allocating requirement as a legal repair. Budgets **`0`** bytes of `RAM0`: a derived `B` equal to the memory anyway, and a zero budget declares no use, so the memory rule never looks |
| `DiagnosticsAgent` | **derives `QM`**, because nothing allocates it a level. Its `pPlain` provide port provides `status` (`A`) — a derived `QM` producing a rated element is a mis-declaration and is reported. Its `pDiag` provide port carries a `measures` map on a **client-server** interface. It mints `measure-on-provide-port` and **only** that: `measure-unknown-element` reads `require` ports alone, because rule 4's one repair — remove the map — decides the key question too. Its `rSafety` require port carries a key spelled `torgue`, which `ISafetySignals` does not declare — the dangling key on a **sender-receiver** interface. Budgets **128** bytes of `RAM0`: a derived `QM` is above nothing, so it is silent |
| `PlainWorker` | **declares `B`.** Its `rPlain` port consumes `status` (`A`) with no measure and is reported; its `rPlainBlank` port declares `status: ''`, which closes rule 2's finding exactly as a named mechanism does (rule 2 reads the key), and since contract `12.4.0` is `safety.measure-blank`'s finding instead — the one blank text of the tree. (The task rule is the other way round: a `coexistence` of `''` is the field's default and the undeclared state, so it closes nothing.) Budgets **256** bytes of `RAM0`: `B` on a `B` memory is equal, and silent |

### `tasks/`

| Element | Why it is here |
|---|---|
| `Task_Mixed` | **three** distinct effective levels — `D` declared, `B` declared, `QM` derived — and **no** `coexistence`. One finding naming all three, in component-name order. It is also the case that says **`QM` beside a rated component is mixed** |
| `Task_Argued` | two distinct levels, `D` declared and `B` derived, **with** a `coexistence` text. Silent — the exemption |
| `Task_Homogeneous` | **two runnables of one component**, so one level and nothing to report — and it carries a `coexistence` text anyway. Silent, which is a deliberate asymmetry: an argument written ahead of a re-rating is legal, where a measure on a provide port is not |

## The seventeen findings, in the order the aggregate mints them

1. `safety.provider-below-element` — `DiagnosticsAgent` (derived `QM`) provides `status` (`A`) on `pPlain`
2. `safety.provider-below-element` — `SpeedSensor` (derived `B`) provides `torque` (`D`) on `pSafety`
3. `safety.consumer-above-element` — `PlainWorker` (`B`) requires `status` (`A`) on `rPlain`
4. `safety.consumer-above-element` — `TorqueLimiter` (`D`) requires `status` (`A`) on `rPlain`
5. `safety.measure-unknown-element` — `DiagnosticsAgent` `rSafety`, key `torgue`
6. `safety.measure-on-provide-port` — `DiagnosticsAgent` `pDiag`
7. `safety.task-mixed-asil` — `Task_Mixed`
8. `safety.measure-blank` — `PlainWorker` `rPlainBlank`, key `status`
9. `safety.partition-on-unqualified-core` — `P_Argued` (`D`) is on `CORE0` (capability `B`)
10. `safety.partition-on-unqualified-core` — `P_Homogeneous` (`D`) is on `CORE0` (capability `B`)
11. `safety.partition-on-unqualified-core` — `P_Mixed` (`D`) is on `CORE0` (capability `B`)
12. `safety.memory-below-component` — `TorqueLimiter` (`D`) budgets 512 bytes of `RAM0` (capability `B`)
13. `safety.component-below-partition` — `DiagnosticsAgent` (derived `QM`) is run by `P_Mixed` (`D`), which declares no argument
14. `safety.component-below-partition` — `PlainWorker` (`B`) is run by `P_Mixed` (`D`), which declares no argument
15. `safety.partition-memory-below-level` — `P_Argued` (`D`) runs `TorqueLimiter`, budgeting `RAM0` (capability `B`)
16. `safety.partition-memory-below-level` — `P_Homogeneous` (`D`) runs `TorqueLimiter`, budgeting `RAM0` (capability `B`)
17. `safety.partition-memory-below-level` — `P_Mixed` (`D`) runs `DiagnosticsAgent`, `PlainWorker` and `TorqueLimiter`, budgeting `RAM0` (capability `B`)

Each line names the **component and the port** the finding is about, and
`packages/tools/tests/test_validator_coexistence.py` asserts that pair against the minted record — the
file-stem drift gate above cannot see a port name, so the numbered list is the only place one is checked.

`pDiag` mints one finding and not two: `safety.measure-unknown-element` reads `require` ports only,
so the provide-port rule owns that map alone.

Lines 9 to 17 are the placement rules' and name no port: the partition rule's line names the
**partition and the core**, the memory rule's the **component and the memory**, the per-type rules'
the **component and the partition** or the **partition and the memory**. Lines 1 to 8 come from
`check_safety_coexistence` and 9 to 17 from `check_hardware_capability`, which the composed tuple
runs after it, rule by rule in that function's order (the three per-type
rules after the memory rule). The per-type rules name each component once per partition, in name
order, and each memory once per partition. The three partition findings are one per partition, in the partitions'
filename order: the rule compares the partition's declared level with its core, and the fixture
gives each task a partition of its own.

## What this fixture is **not**

It is not a safety case and not an example to copy: it is a model built to make every rule fire and
every exemption abstain in one tree, so several of its declarations are deliberately wrong. A model
that passes these rules is not thereby safe, and this one does not pass them.

It also does not exercise the cases that need a *shape* it cannot hold at the same time as the
others — a task with no runnables, a `{swc, runnable}` naming no declared component, two component
files of one name, an undeclared interface, an unrated core, two hardware elements of one name, a
component no task runs. Those are unit cases in `packages/tools/tests/test_validator_coexistence.py` and
`packages/tools/tests/test_validator_capability.py`, each of which builds a throwaway root per test.

`packages/tools/tests/test_validator_coexistence.py` asserts this README's element list equals the tree's, so
the two cannot drift.
