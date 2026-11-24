# Contributing to SWCraft

Help is welcome. This page says how a change gets into SWCraft and how its author is credited, and it
holds everything a developer needs to work on SWCraft from a checkout of this repository. Using
SWCraft is the user guide's subject: [`docs/guide/index.md`](docs/guide/index.md).

## Pull requests are off, Issues are open

This repository on GitHub is a **one-way copy of each release**: the work happens in a separate work
repository, and every release reaches GitHub as one commit. A pull request would have nowhere to land,
so pull requests are switched off.

**Issues are open** for everything else:

- **a bug**: what you ran, what you expected, what happened, the SWCraft version (`swcraft --version`)
  and the platform;
- **a suggestion**: what you want to do and what stops you today;
- **an offer to help**: if you want to work on something, say so in an Issue and describe the change.
  It is agreed there before any work starts, so that nobody spends time on a change that cannot be taken.

## How an agreed change gets in

1. You send the change as a patch, or as a branch in your own fork, linked from the Issue.
2. The maintainer applies it in the work repository as one commit or more, **with you as its author**
   (`git commit --author`), keeping your wording where it is right. The Issue asks which address to use;
   GitHub's no-reply address for your account is the usual one.
3. It goes through the same gate as every other change: the full `verify` on Linux and Windows.
4. The release note of the release that carries it names you in its *Contributors* section.
5. The release commit on GitHub names you as a co-author (`Co-authored-by:`), so GitHub shows you on the
   commit and in the repository's contributors.

Your individual commits stay in the work repository; GitHub shows one commit per release, plus an
occasional commit that updates only this page, the README and its pictures between releases.

## The licence of a contribution

SWCraft is licensed under the Apache License, Version 2.0 ([`LICENSE`](LICENSE)). A contribution you
submit is licensed under the same terms, as section 5 of the licence says. There is no separate
agreement to sign.

## The repository

**`packages/tools/` is the SWCraft core**: the model API, the generators, the ARXML import and export, the
validation and the `swcraft` command line. The MCP server, the agent and the extension build on it;
the specimen generator imports none of SWCraft, on purpose, so that it can be an independent oracle.
All five projects carry one version, sourced from `packages/tools/src/swcraft_tools/_version.py`:

| Path | Project | What it is |
|---|---|---|
| `packages/tools/` | `swcraft-tools` | The core: model API, generators, import/export, ARXML validation, the `swcraft` CLI |
| `packages/mcp/` | `swcraft-mcp` | The MCP server: the model's tools for an assistant |
| `packages/agent/` | `swcraft-agent` | The chat agent, an LLM host around the MCP server |
| `packages/specimens/` | `swcraft-specimens` | Synthetic models from a passport, an independent oracle for the tests, started as a process |
| `vscode_extension/` | `swcraft-vscode` | The VS Code extension |

Beside them: `examples/basic/` (an example project to open in VS Code: manifest, YAML model,
generated outputs, and the Copilot files `swcraft init` writes; the SWCraft extension registers its MCP
server as for any project), `scripts/` (`dev.py`, the developer gate, `build_artifacts.py` and the
workstation installer, with their tests in `scripts/tests/`) and `docs/` (the user guide and the
release notes).

The product's own CLIs — `swcraft` and `swcraft-agent` — are documented for their users in
[`docs/guide/cli.md`](docs/guide/cli.md), and every command, option and default is generated
into [`docs/guide/reference/cli.md`](docs/guide/reference/cli.md). What the VS Code extension
contributes is in [`docs/guide/vscode.md`](docs/guide/vscode.md) and
[`vscode_extension/README.md`](vscode_extension/README.md); the MCP tools an assistant can
call are in [`docs/guide/reference/tools.md`](docs/guide/reference/tools.md).

## Setting up a checkout

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
`npm ci` for the extension. Every other `dev.py` command resolves that venv's interpreter and fails
with an actionable message when it is absent — it never falls back to whatever `python` happens to
be on PATH.

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

## The commands

Everything runs through `scripts/dev.py`, out of the repo-local `.venv` that `install` creates.
`python scripts/dev.py --help` lists every command with what it does; the main ones:

```sh
python scripts/dev.py install    # the repo-local .venv, the Python projects editable, elkjs, npm ci (network)
python scripts/dev.py lint       # ruff, mypy, the import contracts, the quality ratchets (after an
                                 #   improvement: python scripts/quality_ratchet.py update), Biome
python scripts/dev.py test       # the fast gate; deselects the six marker-gated stages
python scripts/dev.py build      # clean -> check-model -> generate ARXML/diagrams/needs -> validate
python scripts/dev.py verify     # the full gate: build, artifacts, the archive and every marker stage
```

Each marker the fast gate deselects runs in a stage of its own (*Test rules*, rule 6 below).

`dev.py test-internal` belongs to the maintainers' work repository: here it prints that its tests are not
part of this repository and exits 0. It knows this copy by the `.swcraft-open-copy` file at the root,
which the snapshot that produced this copy writes; keep that file.

Links in Markdown files must resolve, and no file may carry a private address, a home path or an
e-mail address: `packages/tools/tests/test_public_tree_hygiene.py`, in the fast gate, checks all four.

## Files you regenerate and commit

Two commands rewrite committed files; run them, then commit the result. The drift tests inside `test`
are the check, so neither has a `--check` mode:

```sh
python scripts/dev.py schemas         # vscode_extension/schemas/ + package.json's yamlValidation
python scripts/dev.py docs-reference  # docs/guide/reference/ + mkdocs.yml's reference nav
                                      #   + the agent profiles' guide index
```

## The AUTOSAR schemas

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
reads the schema from `SWCRAFT_AUTOSAR_XSD_PATH` (the guide's
[Getting started](docs/guide/getting-started.md), *Validate — the gate*).

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

## Building the artifacts

`python scripts/dev.py artifacts` builds the shipped artifacts; `python scripts/dev.py runtime`
fetches the archive's payloads (network; every download pinned in
`scripts/workstation/pins/runtime-pins.json`, which `python scripts/dev.py update-pins` rewrites for
review); `python scripts/dev.py wheelhouse` populates the offline installation source on the CPython
`runtime` unpacked; and `python scripts/dev.py workstation-archive` composes the one file a
workstation is handed, downloading nothing:

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

The `guide_site/` tree is a build product: gitignored, rewritten from scratch by every
`dev.py artifacts`, and removed by **no** clean step — `dist/` is what the clean step empties.
It costs about 3.3 MiB in a working tree and stays there until the next artifact build replaces
it or you delete it (`rm -rf packages/tools/src/swcraft_tools/guide_site`).

What the manifest records under `docs/` is the **archive**, not the unpacked `site/`: one hash
per file is what keeps `sha256sum -c` from `dist/` a complete check of a release. The site is
built to be **served, not opened**: over `file://` its directory-style links render as directory
listings and its search cannot load its index. Unpack it and point any static file server at it
(`python -m http.server` is enough).

With SWCraft installed, `swcraft docs` serves the guide that shipped with the installed release on
`127.0.0.1` and opens a browser, with no network involved. Two things override which guide it
serves: `SWCRAFT_GUIDE_SITE` points wherever you say, including an older release's unpacked archive,
and an editable development install resolves the package data to the live checkout, so one
`dev.py artifacts` leaves a payload there that is served until it is rebuilt.
`SWCRAFT_GUIDE_SITE=dist/docs/site swcraft docs` serves a working tree's own render, which is what
a documentation change is previewed with. The archive stays the artifact for everyone else: a
machine with no SWCraft on it, a review copy, an intranet mirror.

The wheelhouse is what makes the workstation install run with no index and no network, and
`dist/runtime/` is what makes it need no Python, no Node.js and no Marketplace. Both, and the
archive that wraps them, are machine-local (one platform, one Python version) and are
deliberately not part of the manifest: they are provisioning, not release artifacts. `dist/` is
gitignored in full.

## The VS Code extension from a checkout

```sh
cd vscode_extension && npm ci      # or `python scripts/dev.py install` from the repo root
npm run package                    # build, then package -> swcraft-vscode-<version>.vsix
code --install-extension vscode_extension/swcraft-vscode-<version>.vsix
```

Then run **SWCraft: Onboard Project** from the Command Palette. Pressing F5 (Run Extension)
against the committed `.vscode/launch.json` is a *developer*-only workflow for iterating on the
extension itself, not the documented end-user install path — that one is
[`docs/guide/vscode.md`](docs/guide/vscode.md).

## Trying the MCP server from this checkout

The extension registers the installed toolchain's server; to try this checkout's instead, add the
stanza of the section *Another chat client* of the guide's [In VS Code](docs/guide/vscode.md) page to
your own VS Code user configuration (*MCP: Open User Configuration*), with the checkout's
`.venv/bin/swcraft` (`.venv\Scripts\swcraft.exe` on Windows) as its `command` in place of `swcraft`.

## Platforms

Linux (x64) and Windows (x64) run the full gate on every change; macOS (Apple silicon) is a
development platform and is built and tested for every release. The release archives are built for
those three.

## Test rules

1. **A test asserts behaviour, not phrasing.** It pins no line number and requires no exact phrase of
   documentation, a docstring, a comment or a source. A check of how the code is built (imports, the
   syntax tree) and a ban on tokens in user-facing files are allowed; `lint`'s quality ratchet fails a
   new phrase requirement and a written `file.py:NNN` citation.
2. **No test asserts wall-clock speed.** A complexity claim is a growth ratio
   (`packages/tools/tests/support/growth.py`), a claim about a budget an operation count, a hang guard a
   `<WHAT>_HANG_SECONDS` constant with its margin; `scripts/tests/test_no_wall_clock_assertions.py`
   holds it.
3. **A scratch git repository goes through the shared helper.** Call `quiet_maintenance` of
   `scratch_git.py` (`packages/tools/tests/support/`; `packages/agent/tests/support/` for `agent`) right
   after its `git init` or `git clone` and before the first commit, so no background maintenance runs
   after a command returns.
4. **New tests go in new files; a giant file never grows.** A test file is held to 3,000 lines, and a
   file already over it (listed in `scripts/quality_baseline.json`) may only shrink.
5. **Every text read and write names `encoding=`, every text write `newline=`;** a path that will be
   compared is spelled `as_posix()`. `packages/tools/tests/test_platform_io.py` scans for it.
6. **A marker has exactly one stage.** `dev.py test` deselects six markers, and each runs in a stage of
   its own: `slow` in `dev.py test-slow`, `artifact` in `test-artifacts`, `integration` in
   `test-model-integration`, `large_bench` in `test-large-bench`, `benchmark` in `benchmark` (report-only)
   and `real_models` in `test-real-models`. A new deselected marker without its stage would run nowhere.
7. **A test writes under `tmp_path`,** never into `examples/basic/model/`, and a test that needs a
   generated model copies a specimen rather than running the generator.
