# `inter_runnable_variables/` — the inter-runnable variable fixture (contract `12.1.0`)

`model/` is a **committed model tree used through a copy**, and it is deliberately **not** a
golden: no `.arxml`, diagram or RST of it is committed anywhere. It holds one component whose
runnables share data without a port:

| Element | What it is here for |
|---|---|
| `swc/chassis/SpeedEstimator.yaml` | the one component, with three variables and three runnables |
| `SpeedEstimator.FilteredSpeed` | an **implicit** variable typed by an **application** type, with an `init`, a description and a calibration access |
| `SpeedEstimator.PeakSpeed` | an **explicit** variable typed by an **implementation** type, with an `init` |
| `SpeedEstimator.SpareCounter` | an explicit variable **nobody reads or writes**, with no `init` |
| `SpeedEstimator.FilterSpeed` | the 10 ms runnable that writes `FilteredSpeed` |
| `SpeedEstimator.LogSpeed` | a 50 ms runnable that reads `FilteredSpeed` through an access with a **stated name** |
| `SpeedEstimator.MonitorSpeed` | a 100 ms runnable that reads `FilteredSpeed`, and **reads and writes** `PeakSpeed` |
| `data_types/` | the application type `VehicleSpeed`, the mapping set that pairs it with `uint16`, and `uint8`/`uint16` with their base types |
| `hardware/` | the mandatory processor and core |

Every variable that is read states an `init`, so `swc.variable.read-without-init` is silent here;
the warning's cases are changes to a copy, written in the tests.

## Why there is no `compositions/` layer

The byte-identity record (`packages/tools/tests/test_external_stubs_byte_identity.py`'s
`test_every_committed_model_tree_is_recorded`) recognises a committed model tree by a YAML file in
its `compositions/` layer directory, and the inter-runnable variable gate
(`packages/tools/tests/test_inter_runnable_variable_byte_identity.py`) compares every recorded tree that
declares no variable. This tree declares variables by design, so it carries no `compositions/`
layer and stays out of that record; if it ever gains one, it needs a row there.

The cost is one finding: `check-model` over the tree **as committed** reports
`composition.no-root`, which has nothing to do with the variables. Every test therefore reads the
tree through `packages/tools/tests/support/inter_runnable_variable_support.py`'s `variable_model`, which copies it
into a throwaway directory and adds the one root composition there. Over that copy `check-model`
reports nothing at all, no warning included.

## Changing this tree

Every row above is a claim a test asserts. Add a shape rather than repurposing one, and say here
what the new element is for.
