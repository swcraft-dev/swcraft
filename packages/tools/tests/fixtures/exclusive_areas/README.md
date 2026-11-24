# `exclusive_areas/` — the exclusive area fixture (contract `12.2.0`)

`model/` is a **committed model tree used through a copy**, and it is deliberately **not** a
golden: no `.arxml`, diagram or RST of it is committed anywhere. It holds one component whose
runnables must not preempt each other inside the areas they share:

| Element | What it is here for |
|---|---|
| `swc/chassis/SpeedEstimator.yaml` | the one component, with three exclusive areas and three runnables |
| `SpeedEstimator.SpeedState` | an area **two runnables run inside**, with a description |
| `SpeedEstimator.OffsetTable` | an area **one runnable can enter**, without a description |
| `SpeedEstimator.SpareArea` | an area **nobody uses** |
| `SpeedEstimator.FilterSpeed` | the 10 ms runnable that runs inside `SpeedState` |
| `SpeedEstimator.MonitorSpeed` | the 100 ms runnable that runs inside `SpeedState` too |
| `SpeedEstimator.CalibrateOffsets` | the 1 s runnable that can enter `OffsetTable` |
| `tasks/` | three tasks of different priority, one per runnable, so the two runnables sharing `SpeedState` run in different tasks |
| `hardware/` | the mandatory processor, one core and the one partition every task runs in |

The component declares no port, so no data type is needed. Every task states a `wcet` budget, so
`hardware.missing-budget` is silent here.

## Why there is no `compositions/` layer

The byte-identity record (`packages/tools/tests/test_external_stubs_byte_identity.py`'s
`test_every_committed_model_tree_is_recorded`) recognises a committed model tree by a YAML file in
its `compositions/` layer directory, and the exclusive area gate
(`packages/tools/tests/test_exclusive_area_byte_identity.py`) asserts that no recorded tree declares an area.
This tree declares areas by design, so it carries no `compositions/` layer and stays out of that
record; if it ever gains one, it needs a row there.

The cost is one finding: `check-model` over the tree **as committed** reports
`composition.no-root`, which has nothing to do with the areas. Every test therefore reads the tree
through `packages/tools/tests/support/exclusive_area_support.py`'s `area_model`, which copies it into a throwaway
directory and adds the one root composition there. Over that copy `check-model` reports nothing at
all, no warning included.

## Changing this tree

Every row above is a claim a test asserts. Add a shape rather than repurposing one, and say here
what the new element is for.
