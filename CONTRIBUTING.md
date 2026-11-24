# Contributing to SWCraft

Help is welcome. This page says how a change gets into SWCraft and how its author is credited.

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

Your individual commits stay in the work repository; GitHub shows one commit per release.

## The licence of a contribution

SWCraft is licensed under the Apache License, Version 2.0 ([`LICENSE`](LICENSE)). A contribution you
submit is licensed under the same terms, as section 5 of the licence says. There is no separate
agreement to sign.

## Building and running the gates

Everything runs through `scripts/dev.py`, which needs only a Python 3.11 to start. The prerequisites
(moon, Python 3.11, Node.js) are in the README's *Prerequisites*.

```sh
python scripts/dev.py install    # the repo-local .venv, the Python projects editable, npm ci (network)
python scripts/dev.py lint       # ruff, the specimen generator's strict type check, the import contracts,
                                 #   the quality ratchets (after an improvement:
                                 #   python scripts/quality_ratchet.py update), Biome
python scripts/dev.py test       # the fast gate
python scripts/dev.py verify     # the full gate: build, artifacts, the archive and every marker stage
```

`dev.py test-internal` belongs to the maintainers' work repository: here it prints that its tests are not
part of this repository and exits 0. It knows this copy by the `.swcraft-open-copy` file at the root,
which the snapshot that produced this copy writes; keep that file.

Links in Markdown files must resolve, and no file may carry a private address, a home path or an
e-mail address: `packages/tools/tests/test_public_tree_hygiene.py`, in the fast gate, checks all four.

**Platforms.** Linux (x64) and Windows (x64) run the full gate on every change; macOS (Apple silicon) is
a development platform and is built and tested for every release. The release archives are built for
those three.

**Trying the MCP server from this checkout.** The extension registers the installed toolchain's server;
to try this checkout's instead, add the stanza of the guide's
[*Another chat client*](docs/guide/vscode.md#another-chat-client) to your own VS Code user
configuration (*MCP: Open User Configuration*), with the checkout's `.venv/bin/swcraft`
(`.venv\Scripts\swcraft.exe` on Windows) as its `command` in place of `swcraft`.

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
