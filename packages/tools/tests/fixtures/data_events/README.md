# `data_events/` — the data-received event fixture (contract `12.3.0`)

`model/` is a **committed model tree used through a copy**, and it is deliberately **not** a
golden: no `.arxml`, diagram or RST of it is committed anywhere. It holds one receiving component
whose runnables are started when a value arrives:

| Element | What it is here for |
|---|---|
| `swc/chassis/SpeedEstimator.yaml` | the one component, with three require ports and three runnables |
| `SpeedEstimator.R_WheelSpeed` | a require port on the sender/receiver `WheelSpeedIf`, with a `queue_length` for its queued element |
| `SpeedEstimator.R_RearSpeed` | a require port on the sender/receiver stub `RearSpeedIf`, which lists `RearLeft` |
| `SpeedEstimator.R_EcuMode` | a require port on the mode-switch stub `EcuModeIf`, the disabled mode's port |
| `SpeedEstimator.FilterSpeed` | started by the reception of `FrontLeft`, and reading it |
| `SpeedEstimator.MonitorSpeed` | started by a 100 ms timing event **and** by the reception of the queued `FrontRight`, which it does not read; that event is disabled in `Sleep` |
| `SpeedEstimator.TrackRearSpeed` | started by the reception of `RearLeft` through the stub port |
| `interfaces/WheelSpeedIf.yaml` | two elements, `FrontLeft` (standard) and `FrontRight` (queued) |
| `interfaces/RearSpeedIf.yaml`, `interfaces/EcuModeIf.yaml` | the two external interface stubs |
| `data_types/` | the one implementation type the elements carry, and its base |
| `tasks/` | two tasks of one partition, mapping every runnable |
| `hardware/` | the mandatory processor, one core and the one partition every task runs in |

Every task states a `wcet` budget, so `hardware.missing-budget` is silent here.

## Why there is no `compositions/` layer

The byte-identity record (`packages/tools/tests/test_external_stubs_byte_identity.py`'s
`test_every_committed_model_tree_is_recorded`) recognises a committed model tree by a YAML file in
its `compositions/` layer directory, and the data-received event gate
(`packages/tools/tests/test_data_event_byte_identity.py`) asserts that no recorded tree declares an event of
type `data`. This tree declares them by design, so it carries no `compositions/` layer and stays out
of that record; if it ever gains one, it needs a row there.

The cost is one finding: `check-model` over the tree **as committed** reports
`composition.no-root`, which has nothing to do with the events. Every test therefore reads the tree
through `packages/tools/tests/support/data_event_support.py`'s `data_event_model`, which copies it into a throwaway
directory and adds the one root composition there. Over that copy `check-model` reports nothing at
all, no warning included.

## Changing this tree

Every row above is a claim a test asserts. Add a shape rather than repurposing one, and say here
what the new element is for.
