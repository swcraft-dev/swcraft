# `operation_events/` — the mixed-event fixture (contract `7.7.0`)

`model/` is a **committed model tree used through a copy**, and it is deliberately **not** a
golden: no `.arxml`, diagram or RST of it is committed anywhere. It holds the one shape the
tests call the **mixed-event fixture**:

| Element | What it is here for |
|---|---|
| `swc/server/ServerSwc.yaml` | one component with one provide port, `pRecords`, on `IRecords` |
| `ServerSwc.ServeAndTick` | one runnable started by **two** event kinds: a `timing` event and an `operation` event (`pRecords` / `ReadRecord`) |
| `interfaces/IRecords.yaml` | the client-server interface declaring `ReadRecord`, the operation the event names |
| `data_types/uint8*.yaml` | the one type the operation's two arguments carry |
| `hardware/`, `requirements/` | the mandatory processor and core, and the link that keeps the requirement clear of `trace.requirement-unallocated` (it kept the component clear of `cross-layer.swc-without-requirement-link` until contract `12.0.0` removed that rule) |

**Mixed event kinds are not judged.** The RTE specification describes mixing as unsupported in
explanatory text, with no tagged requirement; SWCraft does not judge it. The fixture exists to
show that such a model is **kept as written** — loaded, read, validated, written, imported and
exported with both events — and never to label it RTE-conformant. A pass over it is evidence that
nothing drops or rejects either event, not that an RTE generator accepts the runnable.

## `operation_events.arxml` — the importer's synthetic export

Beside the model tree, and unrelated to it, the directory holds one hand-written ARXML export,
**synthetic**, with invented names only and no content from any real export, which
`packages/tools/tests/test_operation_event_import.py` imports. Its header comment lists every shape: two
operations of one provide port on one runnable that may be invoked concurrently, an operation event
beside a timing event, the twelve shapes the importer skips with a reason (among them the three
external-interface-stub cases and a chain skip through a `PR-PORT-PROTOTYPE` the
port mapper skips), and a `SERVER-COM-SPEC` with a queue length
of 2. It is committed through a `.gitignore` negation of its own, and the test module pins the
directory's `*.arxml` files as a set.

## Why there is no `compositions/` layer

The byte-identity record (`packages/tools/tests/test_external_stubs_byte_identity.py`'s
`test_every_committed_model_tree_is_recorded`) recognises a committed model tree by a YAML file in
its `compositions/` layer directory, and every recorded tree must declare **no** operation event
(`packages/tools/tests/test_operation_event_byte_identity.py`'s premise). This tree declares one by
design, so it carries no `compositions/` layer and stays out of that record; if it ever gains one,
it needs a row there, and the premise check would refuse it.

The cost is one finding: `check-model` over the tree **as committed** reports
`composition.no-root`, which has nothing to do with the events. The event tests therefore read the tree
through `packages/tools/tests/support/operation_event_support.py`'s `mixed_event_model`, which copies it into a
throwaway directory and adds the one root composition there. Over that copy `check-model` reports
nothing at all. Not every reader goes through it: the byte-identity records
(`test_arxml_symbol_and_port_init_writer.py`, `test_writer_fidelity_refreeze.py`,
`support/termination_strip_support.py`) and `test_package_placement_writer.py` name the committed tree
directly.

## Changing this tree

Every row above is a claim a test asserts. Add a shape rather than repurposing one, and say here
what the new element is for.
