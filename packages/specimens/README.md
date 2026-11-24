# The specimen generator

The specimen generator (`swcraft-specimens`) is SWCraft's deterministic generator of synthetic,
plausibly structured automotive software architectures. It writes an ordinary SWCraft project, a
`swcraft.yaml` manifest and a `model/` tree, either from a preset or from a model **passport**, and
SWCraft's own validator, passport and build then judge what it wrote. The generated models are not
copies or reconstructions of Bosch, WATERS, any OEM, vendor, customer or other real product
architecture, and they do not claim industrial representativeness for a particular vehicle.

The generator is a generator, not an optimizer. Placement is a deterministic synthetic construction
with a small placement oracle used for the reviewed small case. A specimen's connector and delegation
layout is decided by an integer feasibility program (SciPy's `milp`), whose canonical solution, not
an optimum, is built.

## Its place in SWCraft

The generator was developed as a separate project and was imported into this repository at its
release 0.2.2; until then its command was named differently, and SWCraft's release notes name the
old command. It is the fourth Python project of the repository, beside `packages/tools/`,
`packages/mcp/` and `packages/agent/`, and it carries SWCraft's version.

- **An independent oracle, by a process boundary.** The generator is an oracle of
  SWCraft: its passport reading restates SWCraft's `passport.py` rather than importing it, and its
  contract checks restate the public editor schemas. So the generator never imports `swcraft_tools`,
  `swcraft_mcp` or `swcraft_agent`, and no SWCraft module imports `swcraft_specimens`; SWCraft starts
  it only as a separate process. The one thing it reads from SWCraft is the version of the
  `swcraft-tools` distribution installed beside it (package metadata, not an import).
- **Its own executable.** The console script `swcraft-specimens` (`Scripts\swcraft-specimens.exe`
  on Windows) and `python -m swcraft_specimens` behave identically, with the same exit codes: 0
  success, 2 a refused passport or a configuration error, 1 a defect of the generator's own gates.
  It is not a `swcraft` subcommand, so that its independence rests on the process boundary.
- **Delivery.** It ships inside SWCraft's workstation archive as the `swcraft-specimens` wheel and is
  installed beside `swcraft`. It has no release of its own; SWCraft's release notes cover it.
- **Provenance.** `PROVENANCE.md` is the transformation inventory: what is taken from WATERS 2015
  and SWCraft, and what is the generator's own assumption.

## Requirements

Python 3.11. The runtime dependencies are PyYAML (6.x), NumPy (2.x, from 2.4.6) and SciPy (1.x, from
1.17.1). NumPy and SciPy are pinned exactly in SWCraft's lock (`constraints/dev.txt`), ranged in the
generator's metadata: the specimens are byte-pinned, and a different HiGHS can construct a different
canonical layout, so every install SWCraft makes resolves under the lock. An install outside the lock
can take another NumPy or SciPy, and with it another layout.

The generator pins no SWCraft release. The generated `swcraft.yaml` names the SWCraft release found
when it is generated: `--toolchain-version VERSION` on `generate` and `specimen` when given, else the
version of the `swcraft-tools` distribution installed beside the generator, else no
`toolchain_version` at all, which pins nothing. Each `generation.json` names the same release as
`toolchain.swcraft_version`, or `null`. `swcraft-specimens --version` prints the generator's version.

## Generate and inspect models

`generate` takes a preset by name: the package ships the three WATERS presets (`small`, `medium`,
`large`) and six specimen presets under `swcraft_specimens/presets/`, and `swcraft-specimens --help`
lists them. Any argument with a path separator or a suffix is read as a path to a preset file
instead.

```sh
swcraft-specimens generate small --out build/small
swcraft check-model --warnings-as-errors --project build/small/swcraft.yaml
swcraft validate --project build/small/swcraft.yaml
```

`check-model` may report warnings and still exit 0, so `--warnings-as-errors` (or `--format json`
and an empty `findings` list) is what shows a project with zero findings.

The production sizes are 15 components, 57 runnables, 23 connectors, 36 tasks and 11 hardware
elements for `small`; 60 components, 196 runnables, 239 connectors, 51 tasks and 11 hardware elements
for `medium`; and 160 components, 532 runnables, 1306 connectors, 78 tasks and 11 hardware elements
for `large`. The hardware count is the processor, its cores, the memories and one partition per
core. The scenario suite runs 15 scenarios for each production preset against a SWCraft:

```sh
python tests/scenarios/run_scenarios.py --all-presets --swcraft-bin "$SWCRAFT_BIN" \
  --specimens-bin "$SWCRAFT_SPECIMENS_BIN" \
  --build-root build/scenarios --results build/results/scenario-results.json
```

The runner generates each preset with the `swcraft-specimens` executable `--specimens-bin` names
(else `SWCRAFT_SPECIMENS_BIN`; without either it refuses, and it never searches `PATH`); it never
starts the generator any other way.

Each generated project contains `generation.json`, an analysis-independent sidecar recording the
generator input, the semantic domain and component catalog, the composition hierarchy, boundary
ports and delegations, the canonical project digest, timing and communication summaries, resources,
placement and demonstration landmarks. Generation is reproducible for the same preset, seed and
generator source.

The names and flows are intentionally synthetic: examples include `TorqueRequestCoordinator`,
`VehicleSpeedProvider`, `DiagnosticEventRouter`, `AcquireInputs` and `If_VehicleSpeed`. They do not
describe a real vehicle, ECU, OEM, vendor, customer or proprietary project.

## Specimens from a passport

SWCraft describes a model's shape, without a single name, as a **passport**: layer counts, port and
fan-out distributions, unconnected ports, links and identifier lengths. The generator generates a
**specimen** from one: a project that meets every leaf the passport states exactly, or refuses the
passport leaf by leaf (exit 2) and writes nothing.

```sh
swcraft-specimens specimen passport.json --out build/specimen
# or, in two steps, with a preset file to keep:
swcraft-specimens preset-from-passport passport.json --out specimen.yaml
swcraft-specimens generate specimen.yaml --out build/specimen
```

The generator keeps its own copy of the passport schema (passport `1.0.0`); the two copies are kept
apart on purpose, because the restatement is the oracle's independence. Every sidecar, WATERS and
specimen alike, carries the passport of what was generated.

A passport near the size ceiling that needs the entry planner's exact program can take minutes to
hours; the generator then prints one notice line on stderr (not an error, the exit code is
unchanged), and stating `components.ports.total` and `interfaces.fan_out.top` shortens it.

## Tests

The tests are under `tests/`, with three kinds:

- unmarked tests, which need nothing but the package;
- `integration` tests, which run the external SWCraft named by `SWCRAFT_BIN` and skip without it;
  `SWCRAFT_SPECIMENS_BIN` names the generator executable they start, else this interpreter's
  `python -m swcraft_specimens`. With `SWCRAFT_SPECIMENS_REAL_PASSPORTS` naming a directory of
  passports outside the repository, the integration run also builds a specimen of each and compares
  SWCraft's live passport leaf for leaf; it reports figures only and skips without the variable;
- `slow` tests, minutes each.

From the repository root, `python scripts/dev.py test` runs the unmarked ones, `python scripts/dev.py
test-slow` the `slow` ones and `python scripts/dev.py test-model-integration` the `integration` ones; from
this directory, `python -m pytest -m "not integration and not slow"` runs the unmarked ones.
