# Specimen acceptance passports

The 18 passports here are the acceptance fixtures of the later shapes,
copied byte for byte from a hand-off
for SWCraft, which commits a specimen of each.

**Every one is synthetic.** Each is the `swcraft passport` of a model that SWCraft builds or ships
for its own tests and demonstrations: an output of its benchmark workload generator, one of its
committed scenario or harness configurations, its passport example vectors, its showcase bench or
its basic example project. None is a real model's passport. Each file was read whole before it was
committed: a passport names nothing (it is names-free by schema), and the only strings these 18
hold are the two version leaves, which `tests/test_specimen_acceptance.py` holds.

| Passport | What it stands for |
|---|---|
| `large-bench` | the benchmark workload generator's large bench |
| `benchmark-default` | the benchmark workload generator at its default knobs |
| `passport-small` | one of the groups above; the hand-off does not say which |
| `no-memory` | one of the groups above; the hand-off does not say which (it states no memory) |
| `map-bound` | the read-batch map's over-the-bound shape: 2 component pairs, 41 sender-receiver and 10 client-server interfaces, 2 data types per kind, 2 tasks |
| `scenario-data-element-readers-paged` | the scenario configuration of that name |
| `scenario-effective-level-paged` | the scenario configuration of that name |
| `scenario-find-sixteen-names` | the scenario configuration of that name |
| `scenario-impact-summary-bounded` | the scenario configuration of that name |
| `scenario-import-conflicts-reported` | the scenario configuration of that name |
| `scenario-level-list-paged` | the scenario configuration of that name |
| `scenario-sender-receiver-table-paged` | the scenario configuration of that name |
| `scenario-unconnected-one-component` | the scenario configuration of that name |
| `example-minimal` | the passport contract's minimal example vector, byte-identical to `tests/fixtures/passport/examples/minimal.json` |
| `example-configuration-only-controlled` | the passport contract's configuration-only example vector, byte-identical to `tests/fixtures/passport/examples/configuration-only-controlled.json` |
| `harness-two-rows` | one of the groups above; the hand-off does not say which |
| `showcase-twin` | the twin of SWCraft's showcase bench |
| `basic-twin` | the twin of SWCraft's basic example project |

**The acceptance.** 17 of the 18 are this slice's acceptance: `swcraft-specimens specimen` on each exits
0 and the passport pass meets every stated leaf. `showcase-twin` needs three connection shapes
the specimen generator does not build. It stays refused here;
once the later shapes are met, its one remaining refusal, `interfaces.fan_out.top` (`fan-out`),
is pinned.

**The refusals.** On the day of the hand-off all 18 were refused. The hand-off recorded each
refusal's stderr for 17 of them; those refusals, re-measured, are recorded in the plan's Context
(*Probes*, 1 and 2), and `tests/test_specimen_acceptance.py`'s `REFUSED` holds the passports
still refused, with their refusals; every other one builds.
