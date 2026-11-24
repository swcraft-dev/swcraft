# `fixtures/showcase/model` — the Vehicle Control showcase's model, copied

A committed copy of the model tree of the Vehicle Control showcase, the demonstration project
that lives in its own repository (`swcraft/swcraft-vehicle-control-showcase`), taken from that
repository's `model/` at commit `528214436fd1a45397d40a600356dcd9ca044bf4` (2026-09-15) with
two changes: the empty `systems/` directory is left out, because contract `6.0.0` removed that
layer, and the placeholder `.gitkeep` files are not copied. No element file was edited.

It exists for one reason: the diagram generator's goldens are frozen on four models this
repository owns, and the showcase is the one a colleague is shown. Its
shape differs from the other three — five components side by side in one root, no connector,
one scenario whose task activates three components — and a suite in this repository cannot reach the other
repository, which is why `test_c4_views.py` never compared it.

Its main use is `packages/tools/tests/test_diagram_generation.py`'s goldens. Other suites read it
too, as one tree among others: the byte-identity records (`test_*_byte_identity.py`,
`test_arxml_symbol_and_port_init_writer.py`, `test_writer_fidelity_refreeze.py`,
`support/termination_strip_support.py`), `test_query.py`, `support/impact_record_support.py`,
`test_needs_export.py`, `test_package_placement_writer.py`, `test_validator_coexistence.py` and
`packages/mcp/tests/test_diff_impact_tools.py`; `grep -rn fixtures/showcase` lists them, and this
list is not checked. `conftest.GOLDEN_MODEL_ROOTS` does not name it. It passes `check-model`.

**It is a copy and it goes stale.** Nothing compares it with the showcase repository. When the
showcase's model changes and the goldens should follow, copy its `model/` again, name the new
commit here, re-freeze the goldens (`python packages/tools/tests/test_diagram_generation.py --freeze`)
and record the diff in the commit.

**One edit since the copy, at contract `9.0.0`.** The copy is edited, not
re-copied: contract `9.0.0` removed a task's `core` and made every task name a partition, and the
showcase repository migrates on the release, after the change lands here — so a fresh copy would
not load. Three files moved and nothing else: `hardware/Partition0.yaml` is new (an
unrated partition on `Core0`, the core both tasks named), and `tasks/Task_100ms.yaml` and
`tasks/Task_50ms.yaml` name it in place of `core: Core0`. Every other element is the copy's; the
deployment view, the one diagram that reads a task's core, groups by the core the partition names,
so no golden moves for this edit. The showcase's own partitions, with levels, are its migration's,
not this copy's.
