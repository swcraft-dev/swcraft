# `fixtures/diagram_collisions/model` — the names a drawn element's model entry must tell apart

A committed model tree used **in place**, on the precedent of `fixtures/diagrams/model`. It holds
the shapes where a view draws two elements with one `data-role` and one `data-name` that open two
different files, or one file twice — the reason a page's model block keys an element by its
**ordinal** among the view's `[data-role]` elements and never by its name
(`writer/diagrams/page.py`, the model block). Nothing generated from it is committed:
`packages/tools/tests/test_diagram_generation.py` draws it fresh and checks every entry each page writes.
It passes `check-model`.

| Element | Why it is here |
|---|---|
| `compositions/Root.yaml` | the root; places `LeftC` and `RightC`, so the nesting view draws both under one frame |
| `compositions/LeftC.yaml`, `RightC.yaml` | each holds a member named `Sensor`, of the types `SensorA` and `SensorB`: the nesting view draws two `Sensor` boxes that open two files |
| `swc/collisions/SensorA.yaml` | declares `Init` and `Run`; `TaskA` runs `Init` and `TaskB` runs `Run`, so the deployment view draws `SensorA` once under each task — two boxes, one file |
| `swc/collisions/SensorB.yaml` | declares a runnable `Init` as `SensorA` does |
| `tasks/TaskA.yaml`, `tasks/TaskB.yaml` | the two tasks above, both on `PARTITION0` |
| `sequences/boot.yaml` | steps `SensorA.Init` then `SensorB.Init`: two activations named `Init`, each opening its own component's runnable |
| `hardware/CPU.yaml`, `CORE0.yaml`, `PARTITION0.yaml` | what `check-model` requires of any model, and the deployment view's frame |
| `requirements/REQ_COLLISIONS_001.yaml` | the requirement both components satisfy |

What it does not reach: two compositions or two sequences of one name — `generate diagrams`
refuses them before anything is drawn — and an identity declared by two files, which
`test_diagram_generation.py` writes into a copy of `fixtures/diagrams/model` under `tmp_path`.
