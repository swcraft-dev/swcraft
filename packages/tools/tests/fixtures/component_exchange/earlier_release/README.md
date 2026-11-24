# `earlier_release/` — component files in an earlier writer's shape

The three `.arxml` files are **synthetic**: SWCraft's own output, trimmed by hand. Each was cut from
a component export that the previous release's source wrote for `examples/basic`'s `ExampleSWC`,
so every name in them comes from that example model, and there is no content from any real export.

| File | The shape |
|---|---|
| `ExampleSWC.arxml` | an interface typed by the `float32_impl` twin the earlier writer invented over every primitive |
| `TwinBesideAValueType.arxml` | an interface typed by a value type resting on `float32` directly, the twin beside it referenced by nothing |
| `TwinBehindARecordView.arxml` | a record field typed by `float32_AppView`, which the mapping set maps onto the twin |

`test_component_import.py::TestAComponentFileAnEarlierReleaseWroteIsRefusedByItsTwin` holds the
refusal on all three, and `test_arxml_import_cli.py` reads them among the synthetic exports.
`.gitignore` negates this folder by name, because `*.arxml` is ignored at the root.
