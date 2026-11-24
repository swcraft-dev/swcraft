# `handle_termination_and_restart/` — every value of the field, and its absence (contract `7.9.0`)

`model/` is a **committed model tree used in place**, and it is deliberately **not** a golden: no
`.arxml`, diagram or RST of it is committed anywhere. It is the one committed model that sets each of
`handle_termination_and_restart`'s three values beside a non-stub component that declares none. Many
other trees set the field: the committed trees of the byte-identity record
(`packages/tools/tests/test_external_stubs_byte_identity.py`'s `WITHOUT_A_STUB`) declare `no_support`
on their components, a line the record strips before hashing (`termination_strip_support`), and the
agent-bench models use the other two values as well.

| Element | What it is here for |
|---|---|
| `swc/behaviour/NoSupportSwc.yaml` | `no_support`, beside a timing event — the element follows `EVENTS` |
| `swc/behaviour/TerminatedSwc.yaml` | `can_be_terminated`, beside an init event |
| `swc/behaviour/RestartedSwc.yaml` | `can_be_terminated_and_restarted` on a component with no runnable, so no `EVENTS` — the element follows the behaviour's `SHORT-NAME` |
| `swc/behaviour/SilentSwc.yaml` | declares no value — the absence fixture: `check-model` reports it and the writer refuses it (`rule:swc.undeclared-termination-and-restart`, contract `7.10.0`), never a synthesized `NO-SUPPORT` |
| `interfaces/ISignal.yaml`, `data_types/uint8*.yaml` | the one interface and type every port speaks |

Read by `packages/tools/tests/test_handle_termination_and_restart.py` (the writer, the round trips, the
carry of `import component`) and by `packages/tools/tests/test_arxml_xsd_conformance.py`
(`TestTheHandleTerminationAndRestartConforms`, each literal at 4.2.2 and 4.3.0 against the licensed
schemas), and by `packages/tools/tests/test_termination_required.py` (the refusal). Since contract `7.10.0`
the writer refuses the tree as committed, for `SilentSwc`, so every test that generates it generates
a copy without that one component (`conftest.handle_termination_and_restart_declared_copy`).

There is no `compositions/`, `hardware/` or `requirements/` layer: the tree is read by the writer and
the importers, and `check-model`'s layer-presence findings are not what it is for. A tree without a
`compositions/` layer is also what keeps it out of the byte-identity record, whose digests predate
the field and hold only because the `no_support` lines are stripped before hashing.
