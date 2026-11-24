# SWCraft

SWCraft is an architecture-as-code toolchain for **software architects of AUTOSAR Classic
systems**. The architecture is a YAML model — software components, ports and interfaces, data
types, compositions, tasks, hardware and the requirements they trace to — kept in git and edited by
hand, in VS Code or by an assistant. Every other artifact is generated from it, and
`swcraft validate` is the gate that says whether the model is buildable.

## What it produces

- **AUTOSAR Classic ARXML** (4.2.2 or 4.3.0) from the model, checked against the official XSD when
  you provide it, and the import of existing ARXML into a model;
- **diagrams** — component, composition and sequence views, laid out and rendered as SVG;
- **Sphinx-Needs JSON** for the requirements toolchain, and the test tables a test team works from;
- **reports** — a change report between two model states, architecture metrics, a resource report
  against the hardware's budgets, and allocation experiments;
- **an assistant's view of the model**: an MCP server whose tools read, query and edit the model
  through validated, staged change sets, a VS Code extension that registers it and shows the model,
  and a chat agent.

## Install

SWCraft installs from one archive per platform, `swcraft-<version>-<platform>.zip`, published with
every release on the [Releases page of `github.com/swcraft-dev/swcraft`](https://github.com/swcraft-dev/swcraft/releases):

| Platform | Archive |
|---|---|
| Linux (x64) | `swcraft-<version>-linux-x64.zip` |
| Windows (x64) | `swcraft-<version>-windows-x64.zip` |
| macOS (Apple silicon) | `swcraft-<version>-macos-arm64.zip` |

Each archive comes with a `SHA256SUMS-<platform>` file beside it. It carries the four Python
wheels and their whole third-party closure, the VS Code extension, CPython 3.11, Node.js, the two
companion VS Code extensions and the installer, so nothing needs to be on the machine first and
nothing is fetched while it installs. The macOS archive is not signed, so macOS quarantines what
it unpacks and refuses to start the programs inside; clear the flag on the unpacked folder once,
before you run the installer:

```sh
xattr -dr com.apple.quarantine <unpacked folder>
```

The whole procedure, per platform, is the guide's
[Getting started](docs/guide/getting-started.md) page.

## Quick start for an architecture project

An architecture repository consumes SWCraft as an **installed toolchain** and never through a
path into this checkout — and it installs nothing itself. The diagram layout engine, `elkjs`, is
package data inside the `swcraft-tools` wheel, so the archive above is the whole of it:

```sh
# 1. install the toolchain, once per machine: unpack the archive and run the one file inside it
#    (`swcraft_install.cmd` on Windows). On Windows the installer edits the user PATH; on
#    macOS/Linux it PRINTS one `export PATH=...` line for your shell profile and edits nothing.
#    Either way open a NEW terminal afterwards — PATH is read at start-up
./swcraft_install.sh

# 2. scaffold the project (--integration moon writes its build graph; `swcraft.yaml` records
#    the release it was scaffolded with, as `toolchain_version`, and nothing else pins it)
swcraft init ~/projects/vehicle-arch --integration moon

# 3. build the model — exit 0 means the outputs are fresh; on non-zero read the failure,
#    not the directory: a model error stops the graph before generation (a warning is
#    listed on stderr and the graph goes on), while a failed ARXML schema check leaves
#    the refused file where it was written
cd ~/projects/vehicle-arch
moon run :build
```

There is no project `.venv`, no `bootstrap.py` and no chat configuration to generate: the
toolchain is one installation under the user profile with one `swcraft` on `PATH`, and the VS
Code extension registers SWCraft's MCP server with the editor itself.

The full procedure — including installing on a build agent (`--no-vsix`), adding SWCraft to an
*existing* architecture repository, and what a downstream CI/CD pipeline wraps — is the guide's
[An architecture repository](docs/guide/architecture-repository.md) page; updating the pinned
toolchain is [Upgrading](docs/guide/upgrading.md), and the symptoms with their fixes are
[Troubleshooting](docs/guide/troubleshooting.md).

## Documentation

| You are | Start here |
|---|---|
| An architect **using** SWCraft | [`docs/guide/index.md`](docs/guide/index.md) — getting started, the model, VS Code, the assistant, the command line, diagrams, the test tables, experiments, the change report, architecture metrics, upgrading, How-to pages for the common changes with a Refusals page that explains each refusal code, plus a generated reference for every layer, rule, command and tool |
| An integrator adding SWCraft to an **architecture repository** | [`docs/guide/architecture-repository.md`](docs/guide/architecture-repository.md) — the build graph, an existing repository and a build machine |
| A developer working on **SWCraft itself** | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

The user guide also ships inside every installation: `swcraft docs` serves the guide of the
installed release on `127.0.0.1` and opens it in a browser, with no network involved.
`python scripts/dev.py docs` builds the same site from this checkout; the pages are ordinary
Markdown and read fine in the repository without it.

An introduction to SWCraft with a short video is on
[the project site](https://swcraft.tarandevich.org).

What a release breaks, and what upgrading takes, is in each release's note under
[`docs/release-notes/`](docs/release-notes/).

## The repository

**`packages/tools/` is the SWCraft core**: the model API, the generators, the ARXML import and export, the
validation and the `swcraft` command line. The MCP server, the agent and the extension build on it;
the specimen generator imports none of SWCraft, on purpose, so that it can be an independent oracle.
All five projects carry one version:

| Path | Project | What it is |
|---|---|---|
| `packages/tools/` | `swcraft-tools` | The core: model API, generators, import/export, ARXML validation, the `swcraft` CLI |
| `packages/mcp/` | `swcraft-mcp` | The MCP server: the model's tools for an assistant |
| `packages/agent/` | `swcraft-agent` | The chat agent, an LLM host around the MCP server |
| `packages/specimens/` | `swcraft-specimens` | Synthetic models from a passport, an independent oracle for the tests, started as a process |
| `vscode_extension/` | `swcraft-vscode` | The VS Code extension |

Beside them: `examples/basic/` (an example project to open in VS Code: manifest, YAML model,
generated outputs, and the Copilot files `swcraft init` writes; the SWCraft extension registers its MCP
server as for any project),
`scripts/` (`dev.py`, the developer gate, `build_artifacts.py` and the workstation installer, with
their tests in `scripts/tests/`) and
`docs/` (the user guide and the release notes).

## Prerequisites

This section and the next two are for working on SWCraft itself, from a checkout of this
repository.

- [moon](https://moonrepo.dev) — **required for SWCraft development**, not merely for
  convenience: `python scripts/dev.py test-model-integration` runs a real `moon run :build`
  in a scaffolded standalone architecture project, and that acceptance fails (it never
  skips) when moon cannot be resolved. It is also the build orchestrator of every
  architecture project scaffolded with `--integration moon`.
- Python 3.11 (provisioned automatically via proto)
- Node.js 22.13 (provisioned automatically via proto — used by `vscode_extension/` and by
  `swcraft generate diagrams`, which lays the diagrams out with elkjs on Node.js)

`python scripts/dev.py install` provisions everything: it creates the repo-local `.venv`,
installs the four Python projects into it editable (the specimen generator, `packages/specimens/`,
among them), stages the pinned `elkjs` payload into the `swcraft-tools` package tree and runs
`npm ci` for the extension.
Every other `dev.py` command resolves that venv's interpreter and fails with an actionable
message when it is absent — it never falls back to whatever `python` happens to be on PATH.

Two global flags come before the command name:

```sh
python scripts/dev.py --skip-node install   # Python only: no `npm ci`, no Biome, no node --test
                                            #   (elkjs is still staged: the fetch needs no npm)
python scripts/dev.py --repo-root DIR test  # operate on another checkout (default: this one)
```

`--skip-node` covers exactly three commands — `install`, `lint` and `test` — and on `test` it also
reaches the `tools` and `scripts` pytest steps, where the tests that lay a diagram out on a real Node.js are
skipped naming the flag; without the flag, a checkout with no Node.js **fails** them. It is **not** a way
to run the full gate without Node: `artifacts` builds the packaged `.vsix`, which only npm can
produce, so `artifacts` (and therefore `verify`) refuses `--skip-node` and refuses a missing
npm, at plan time, before the first subprocess runs.

## Commands

Everything runs out of the repo-local `.venv` that `install` creates.

```sh
python scripts/dev.py install    # create .venv, install the four Python projects, stage elkjs + npm ci
python scripts/dev.py lint       # ruff check + ruff format --check (+ mypy over the four Python projects: strict for packages/specimens/),
                                 #   lint-imports, the quality ratchets (scripts/quality_ratchet.py), then Biome
python scripts/dev.py test       # the fast gate; deselects the six marker-gated stages
python scripts/dev.py build      # clean -> check-model -> generate ARXML/diagrams/needs -> validate
python scripts/dev.py verify     # the full gate (install, lint, test, build, artifacts,
                                 #   runtime, wheelhouse, workstation-archive, test-artifacts,
                                 #   test-slow, test-large-bench, test-model-integration)

# Marker-gated stages -- each deselected marker has exactly one stage of its own
python scripts/dev.py test-artifacts         # -m artifact
python scripts/dev.py test-slow              # -m slow
python scripts/dev.py test-large-bench       # -m large_bench; the functional scenarios on the large bench
python scripts/dev.py test-model-integration # -m integration; FAILS (never skips) without moon
python scripts/dev.py benchmark              # -m benchmark; report-only, not in verify
python scripts/dev.py test-real-models       # -m real_models on SWCRAFT_REAL_MODELS; not in verify

# Generated-and-committed artifacts: run these, then commit the result. The drift tests
# inside `test` are the check, so neither has a --check mode
python scripts/dev.py schemas         # vscode_extension/schemas/ + package.json's yamlValidation
python scripts/dev.py docs-reference  # docs/guide/reference/ + mkdocs.yml's reference nav
                                      #   + the agent profiles' guide index

# Packaging and delivery
python scripts/dev.py docs       # build the user-guide site into dist/docs/site/, then
                                 #   write its guide-corpus.json (the assistant's guide)
python scripts/dev.py package    # the .vsix
python scripts/dev.py artifacts  # dist/python/*.whl + sdists, dist/vscode/*.vsix,
                                 #   dist/docs/swcraft-guide-<v>.zip, manifest
python scripts/dev.py runtime    # dist/runtime/ = the pinned payloads (Node.js, CPython 3.11,
                                 #   the two companion .vsix; elkjs staged into the package) + the notices and
                                 #   RUNTIME.json; network; in verify between artifacts and wheelhouse
python scripts/dev.py update-pins  # rewrite scripts/workstation/pins/runtime-pins.json for
                                 #   review; network; not a verify stage
python scripts/dev.py wheelhouse # dist/wheelhouse/ = those wheels + their closure, downloaded on
                                 #   the CPython `runtime` unpacked (refused before `runtime`)
python scripts/dev.py workstation-archive  # dist/workstation/*.zip: the installer, the two
                                 #   wrappers, the .vsix, the wheelhouse, runtime/, extensions/
                                 #   and THIRD-PARTY-NOTICES.md, in one file (about 200 MB)
python scripts/dev.py coverage   # report-only, not in verify

# Selected by PATH rather than a marker: re-runs the XSD suite with --require-autosar-xsd,
# which turns an absent licensed schema into a FAILURE (see below)
python scripts/dev.py test-xsd
```

The product's own CLIs — `swcraft` and `swcraft-agent` — are documented for their users in
[`docs/guide/cli.md`](docs/guide/cli.md), and every command, option and default is generated
into [`docs/guide/reference/cli.md`](docs/guide/reference/cli.md). What the VS Code extension
contributes is in [`docs/guide/vscode.md`](docs/guide/vscode.md) and
[`vscode_extension/README.md`](vscode_extension/README.md); the MCP tools an assistant can
call are in [`docs/guide/reference/tools.md`](docs/guide/reference/tools.md).

### AUTOSAR schema (optional, for full XSD validation)

The official AUTOSAR XSDs require an AUTOSAR account and are not redistributable, so none is
committed. Validation works without them and degrades to a structural check that says so in its
verdict line. To enable full XSD validation, provide the schema for the version you generate —
the provisioning script takes the version as its one argument:

| Version | Expected path | Mirror-URL variable |
|---|---|---|
| `4.2.2` (default) | `schemas/AUTOSAR_4-2-2.xsd` | `SWCRAFT_AUTOSAR_XSD_URL_4_2_2` (or the legacy unversioned `SWCRAFT_AUTOSAR_XSD_URL`) |
| `4.3.0` | `schemas/AUTOSAR_4-3-0.xsd` | `SWCRAFT_AUTOSAR_XSD_URL_4_3_0` |

```sh
SWCRAFT_AUTOSAR_XSD_URL_4_2_2=https://your-mirror/AUTOSAR_4-2-2.xsd \
  .venv/bin/python scripts/download_autosar_schema.py            # 4.2.2, the default
SWCRAFT_AUTOSAR_XSD_URL_4_3_0=https://your-mirror/AUTOSAR_4-3-0.xsd \
  .venv/bin/python scripts/download_autosar_schema.py 4.3.0
```

`schemas/` is the default only in a source checkout; an installed `swcraft` has no default and
reads the schema from `SWCRAFT_AUTOSAR_XSD_PATH`
([`docs/guide/getting-started.md`](docs/guide/getting-started.md), *Validate — the gate*).

Which schema a file is checked against comes from the file itself — the `xsi:schemaLocation`
on its root element; a file that declares no `xsi:schemaLocation` is never XSD-validated.

**The XSD-conformance gate.** `python scripts/dev.py test-xsd` validates every committed golden
and every golden's model, generated at each supported version, against the **real** licensed
schemas — and fails rather than skips when one is missing, which is what makes it a gate. Those
same tests skip loudly inside `python scripts/dev.py test`, so `verify` stays runnable without a
license and `test-xsd` is deliberately not one of its stages. Because `schemas/` is gitignored,
name both schemas explicitly when they live elsewhere:

```sh
SWCRAFT_AUTOSAR_XSD_PATH_4_2_2=/path/to/AUTOSAR_4-2-2.xsd \
SWCRAFT_AUTOSAR_XSD_PATH_4_3_0=/path/to/AUTOSAR_4-3-0.xsd \
  python scripts/dev.py test-xsd
```

### Local artifacts

`python scripts/dev.py artifacts` builds the shipped artifacts;
`python scripts/dev.py runtime` fetches the archive's payloads and
`python scripts/dev.py wheelhouse` populates the offline installation source on the CPython it
unpacked:

```text
dist/
  python/     swcraft_tools-<v>-py3-none-any.whl  (+ .tar.gz)   <- shipped artifacts
              swcraft_mcp-<v>-py3-none-any.whl    (+ .tar.gz)
              swcraft_agent-<v>-py3-none-any.whl  (+ .tar.gz)
              swcraft_specimens-<v>-py3-none-any.whl  (+ .tar.gz)
  vscode/     swcraft-vscode-<v>.vsix
  docs/       swcraft-guide-<v>.zip   the rendered user guide, one archive
              site/                   the same site unpacked; `dev.py docs` stops here
  runtime/    the archive's payloads for this platform  <- machine-local
              node.{tar.xz,tar.gz,zip}, python.tar.gz, the two companion
              <publisher>.<name>.vsix, THIRD-PARTY-NOTICES.md and RUNTIME.json; plus python/,
              the CPython unpacked for `wheelhouse` to download on (never an archive member)
  wheelhouse/ the four wheels above + their whole third-party closure  <- machine-local
              PLATFORM.json   (python version + platform tag this closure was resolved for)
  workstation/ swcraft-<v>-<platform>.zip + SHA256SUMS   <- the one file a workstation is
              handed: a README, the installer, the two wrappers, the .vsix, the wheelhouse,
              runtime/ (Node.js, CPython, RUNTIME.json), extensions/ (the two
              companion .vsix) and THIRD-PARTY-NOTICES.md
  manifest.json   version, built_at, git_commit, {name, kind, filename, sha256, size}
                  per artifact, and `sources`, a {count, sha256} fingerprint of the file
                  set they were built from
  SHA256SUMS

packages/tools/src/swcraft_tools/guide_site/    the same rendered guide again, copied into the
                                       package so the swcraft-tools wheel carries it and
                                       `swcraft docs` can serve it
packages/tools/src/swcraft_tools/writer/diagrams/elkjs/
                                       the pinned elkjs payload (five files), staged by
                                       `install` and `runtime`, carried by the wheel
```

That last tree is a build product: gitignored, rewritten from scratch by every
`dev.py artifacts`, and removed by **no** clean step — `dist/` is what the clean step empties.
It costs about 3.3 MiB in a working tree and stays there until the next artifact build replaces
it or you delete it (`rm -rf packages/tools/src/swcraft_tools/guide_site`).

What the manifest records under `docs/` is the **archive**, not the unpacked `site/`: one hash
per file is what keeps `sha256sum -c` from `dist/` a complete check of a release. The site is
built to be **served, not opened**: over `file://` its directory-style links render as directory
listings and its search cannot load its index. Unpack it and point any static file server at it
(`python -m http.server` is enough).

Which of the two an architect reaches for depends on whether SWCraft is installed. **With an
install, `swcraft docs` is the whole procedure**: the same rendered site ships inside the
`swcraft-tools` wheel, and the command serves it on `127.0.0.1` and opens a browser — search and
navigation work because it is served, no network is involved, and by default the guide is the one
that shipped with the installed product rather than one a URL happened to point at. *By default*,
because two things override it: `SWCRAFT_GUIDE_SITE` points wherever you say, including an older
release's unpacked archive, and an editable development install resolves the package data to the
live checkout, so one `dev.py artifacts` leaves a payload there that is served until it is
rebuilt. **The archive stays the artifact for everyone else**: a machine with no
SWCraft on it, a review copy, an intranet mirror. `SWCRAFT_GUIDE_SITE=dist/docs/site swcraft docs`
serves a working tree's own render, which is what a documentation change is previewed with.

The wheelhouse is what makes the workstation install run with no index and no network, and
`dist/runtime/` is what makes it need no Python, no Node.js and no Marketplace. Both, and the
archive that wraps them, are machine-local (one platform, one Python version) and are
deliberately not part of the manifest: they are provisioning, not release artifacts. `dist/` is
gitignored in full.

All the shipped artifacts carry one lockstep version, sourced from
`packages/tools/src/swcraft_tools/_version.py`.

## Installing the VS Code extension from this checkout

```sh
cd vscode_extension && npm ci      # or `python scripts/dev.py install` from the repo root
npm run package                    # build, then package -> swcraft-vscode-<version>.vsix
code --install-extension vscode_extension/swcraft-vscode-<version>.vsix
```

Then run **SWCraft: Onboard Project** from the Command Palette. Pressing F5 (Run Extension)
against the committed `.vscode/launch.json` is a *developer*-only workflow for iterating on the
extension itself, not the documented end-user install path — that one is
[`docs/guide/vscode.md`](docs/guide/vscode.md).

## Contributing

Pull requests are not accepted on GitHub: this repository is a one-way copy of each release.
Bug reports and suggestions go to Issues, and anyone who wants to help is welcome to say so in
one — [`CONTRIBUTING.md`](CONTRIBUTING.md) says how a change gets in and how its author is credited.

## Licence

SWCraft is licensed under the Apache License, Version 2.0 ([`LICENSE`](LICENSE)). The name
"SWCraft" is not licensed: the licence grants no permission to use it beyond describing where the
work comes from ([`NOTICE`](NOTICE), and § 6 of the licence). What SWCraft did not write, and what a
release archive bundles beside it, is named in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
