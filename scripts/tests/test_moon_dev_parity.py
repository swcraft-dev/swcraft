"""What the deleted Moon tasks became, held to it permanently.

* :data:`REPLACED_MOON_TASKS` is a frozen inventory of what each removed Moon task
  became. Its keys are historical labels; every replacement is checked against the
  live surfaces (``dev.py``'s commands, ``package.json``'s scripts, the ``swcraft``
  CLI's parser), and a ``dev`` entry must really plan a step in every directory it
  claims to cover.
* :data:`_COMMANDS_WITHOUT_A_MOON_PREDECESSOR` closes the other direction: every
  ``dev.py`` command is a replacement or recorded as new, so a command added later
  fails until classified.
* :data:`_VERIFY_FAN_OUT` / :data:`_LINT_FAN_OUT` are the unit lists the deleted
  sequences ran, so ``dev.py`` keeps the same fan-out.

Each rule is also shown to fire against a mutated copy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from conftest import REPO_ROOT, dev_context, dev_module, parser_subcommand_options
from swcraft_tools.cli.dispatch import build_parser

_DEV = dev_module()


# ---------------------------------------------------------------------------
# The frozen inventory
# ---------------------------------------------------------------------------

#: The three kinds of replacement. ``cli`` is for ``mcp_server:serve`` alone, a
#: developer convenience whose native form is a ``swcraft`` subcommand.
_KINDS = ("dev", "npm", "cli")


@dataclass(frozen=True)
class Replacement:
    """What replaced one Moon task when the Moon workspace was deleted."""

    kind: str
    target: str
    note: str
    #: Directories (relative to the repo root, ``"."`` for the root itself) in
    #: which the mapped ``dev.py`` command must really plan a step. Empty for the
    #: ``npm``/``cli`` kinds, which are checked against their own surfaces.
    covers: tuple[str, ...] = field(default=())


#: Frozen from the five ``moon.yml`` files; the values, and so the directories, are today's.
REPLACED_MOON_TASKS: dict[str, Replacement] = {
    # -- root: the pipeline project, whose tasks fanned out through
    #    scripts/moon_run_sequence.py; dev.py performs that fan-out itself.
    "root:build": Replacement("dev", "build", "the reference-model chain against examples/basic", (".",)),
    "root:install": Replacement(
        "dev",
        "install",
        "creates .venv and installs every project into it",
        (".", "packages/tools", "packages/mcp", "packages/agent", "vscode_extension"),
    ),
    "root:test": Replacement(
        "dev",
        "test",
        "the fast gate over every project",
        ("packages/tools", "packages/mcp", "packages/agent", "vscode_extension"),
    ),
    "root:lint": Replacement(
        "dev",
        "lint",
        "ruff over every Python project plus scripts/, then Biome",
        (".", "packages/tools", "packages/mcp", "packages/agent", "vscode_extension"),
    ),
    "root:lint-scripts": Replacement("dev", "lint", "repo-root scripts/ is a step of dev.py lint", (".",)),
    # -- tools: the generation chain is dev.py build; the rest are per-project stages.
    "tools:install": Replacement("dev", "install", "editable install into the repo-local .venv", ("packages/tools",)),
    "tools:test": Replacement("dev", "test", "pytest with the staged markers deselected", ("packages/tools",)),
    "tools:coverage": Replacement("dev", "coverage", "report-only, on demand", ("packages/tools",)),
    "tools:benchmark": Replacement("dev", "benchmark", "report-only, on demand", ("packages/tools",)),
    "tools:lint": Replacement("dev", "lint", "the shared no-shell ruff runner", ("packages/tools",)),
    "tools:download-schema": Replacement("dev", "build", "the optional licensed-XSD provisioning step", (".",)),
    "tools:generate-arxml": Replacement("dev", "build", "generation step of the reference chain", (".",)),
    "tools:generate-c4": Replacement("dev", "build", "generation step of the reference chain", (".",)),
    "tools:generate-rst": Replacement("dev", "build", "generation step of the reference chain", (".",)),
    "tools:validate-arxml": Replacement("dev", "build", "the ARXML validation step of the reference chain", (".",)),
    "tools:validate-model": Replacement("dev", "build", "check-model, the chain's first step", (".",)),
    # -- mcp_server
    "mcp_server:install": Replacement(
        "dev", "install", "editable install into the repo-local .venv", ("packages/mcp",)
    ),
    "mcp_server:test": Replacement("dev", "test", "pytest with the staged markers deselected", ("packages/mcp",)),
    "mcp_server:coverage": Replacement("dev", "coverage", "report-only, on demand", ("packages/mcp",)),
    "mcp_server:lint": Replacement("dev", "lint", "the shared no-shell ruff runner", ("packages/mcp",)),
    "mcp_server:serve": Replacement(
        "cli",
        "mcp-serve",
        "a developer convenience with no gate to preserve: the native form is "
        "`swcraft mcp-serve --project examples/basic/swcraft.yaml`, which is what "
        "the VS Code extension's registered server definition already runs",
    ),
    # -- agent
    "agent:install": Replacement("dev", "install", "editable install into the repo-local .venv", ("packages/agent",)),
    "agent:test": Replacement("dev", "test", "pytest with the staged markers deselected", ("packages/agent",)),
    "agent:test-slow": Replacement("dev", "test-slow", "the only stage that runs the slow suite", ("packages/agent",)),
    "agent:coverage": Replacement("dev", "coverage", "report-only, on demand", ("packages/agent",)),
    "agent:lint": Replacement("dev", "lint", "the shared no-shell ruff runner", ("packages/agent",)),
    # -- vscode_extension: driven through its npm scripts.
    "vscode_extension:install": Replacement(
        "dev", "install", "the npm ci step of dev.py install", ("vscode_extension",)
    ),
    "vscode_extension:build": Replacement(
        "npm",
        "build",
        "no dev.py command of its own: the bundle is built by `npm run package` "
        "(and by scripts/build_artifacts.py's four-step npm sequence) rather than "
        "as a gate in its own right",
    ),
    "vscode_extension:test": Replacement("dev", "test", "the npm run test step of dev.py test", ("vscode_extension",)),
    "vscode_extension:lint": Replacement("dev", "lint", "the npm run lint step of dev.py lint", ("vscode_extension",)),
    "vscode_extension:package": Replacement("dev", "package", "npm run package", ("vscode_extension",)),
    "vscode_extension:test-integration": Replacement(
        "dev", "test-integration", "on demand only -- network and display", ("vscode_extension",)
    ),
}

#: ``dev.py`` commands that replace no Moon task. With the inventory's ``dev`` targets
#: they must account for every command.
_COMMANDS_WITHOUT_A_MOON_PREDECESSOR = {
    "wheelhouse": "populates dist/wheelhouse/ -- Moon had no offline install source",
    "artifacts": "builds the versioned local wheels and .vsix -- nothing produced them before",
    "test-artifacts": "the only stage running the new `artifact` marker",
    "test-model-integration": "the new mandatory standalone-architecture acceptance",
    "verify": "the aggregate gate; root:test only ever ran the fast half",
    "test-xsd": (
        "the XSD-conformance gate: re-runs packages/tools/tests/test_arxml_xsd_conformance.py "
        "with --require-autosar-xsd, so an absent licensed schema fails instead of skipping. "
        "Moon never validated against a licensed XSD -- `validate-arxml` ran the structural "
        "fallback, which is exactly why the non-conformance it would have caught survived"
    ),
    "schemas": (
        "the editor-schema generator: writes the JSON Schemas Red Hat YAML reads "
        "into vscode_extension/schemas/. Moon never had such a task -- the schemas postdate Moon"
    ),
    "docs": (
        "the user-guide site build: renders docs/guide/ into dist/docs/site/ with "
        "mkdocs --strict. Moon never built a guide -- the docs/guide/ tree and the "
        "documentation site postdate Moon"
    ),
    "workstation-archive": (
        "the workstation archive: composes scripts/workstation/, dist/vscode/'s .vsix, "
        "the whole of dist/wheelhouse/ and dist/runtime/'s payloads into "
        "dist/workstation/swcraft-<v>-<platform>.zip. "
        "Moon never had such a task -- the archive, the installer and the wrappers postdate "
        "Moon, and the two stages it composes (`artifacts`, `wheelhouse`) "
        "are themselves recorded above as having no Moon predecessor"
    ),
    "runtime": (
        "the runtime payloads: fetches the pinned Node.js and CPython releases and the "
        "companion extensions into dist/runtime/, which workstation-archive composes in. Moon never "
        "had such a task -- the archive it feeds postdates Moon, as recorded above"
    ),
    "update-pins": (
        "the pin minting: rewrites scripts/workstation/pins/runtime-pins.json from the "
        "publishers -- the Node.js and CPython digests and the companion extensions' newest "
        "Marketplace releases -- for a maintainer to review. Moon never had such a task -- the "
        "pins file and the `runtime` stage that reads it postdate Moon, as recorded above"
    ),
    "update-dev-lock": (
        "The CI-on-the-PC plan's lock: rewrites constraints/dev.txt with `uv pip compile` for a "
        "maintainer to review. Moon never had such a task -- it installed nothing against a lock"
    ),
    "docs-reference": (
        "the guide reference generator: writes docs/guide/reference/ from the model "
        "contract, the rule declarations and the CLI parser, and rewrites the nav section in "
        "mkdocs.yml that names those pages. Moon never had such a task -- the pages postdate "
        "Moon, as does the other generator (`schemas`)"
    ),
    "test-large-bench": (
        "the large-bench tier: the only stage running the `large_bench`-marked functional "
        "scenarios on the generated large bench. Moon never had such a task -- the functional "
        "scenarios and the bench postdate Moon"
    ),
    "test-real-models": (
        "the calibration of the passport chain on real exports, the only stage running the "
        "`real_models` marker, on demand and outside verify. Moon never had such a task -- the "
        "passport and its chain postdate Moon"
    ),
    "test-internal": (
        "The open-copy plan's record-consistency stage: ruff and pytest over internal/, the tests "
        "that read the work repository's closed files, outside verify. Moon never had such a task "
        "-- internal/ and the public tree it is kept out of postdate Moon"
    ),
}

#: The unit lists the deleted sequences ran, by today's project labels; ``scripts``
#: is repo-root ``scripts/``, which had no Moon project of its own.
_VERIFY_FAN_OUT = ["tools", "mcp", "agent", "vscode_extension"]
_LINT_FAN_OUT = {"tools", "mcp", "agent", "vscode_extension", "scripts"}
_BOOTSTRAP_FAN_OUT = {"tools", "mcp", "agent", "vscode_extension"}

#: Projects that joined ``dev.py`` after Moon's deletion; :func:`plan_units` leaves them out,
#: so the comparison still proves no Moon-era unit was dropped.
_POST_MOON_PROJECTS = frozenset({"specimens"})

#: Test-only projects that joined after Moon's deletion; their pytest step is left out, while
#: ``lint``'s repository-root ``scripts/`` unit is kept.
_POST_MOON_TEST_PROJECTS = frozenset({"scripts"})


# ---------------------------------------------------------------------------
# The native surfaces the inventory is checked against
# ---------------------------------------------------------------------------


def npm_scripts(repo_root: Path) -> frozenset[str]:
    """The extension's declared npm scripts."""
    manifest = json.loads((repo_root / _DEV.VSCODE_PROJECT / "package.json").read_text(encoding="utf-8"))
    return frozenset(manifest.get("scripts", {}))


def cli_subcommands() -> frozenset[str]:
    """The ``swcraft`` CLI's subcommands, read from its real parser."""
    return frozenset(parser_subcommand_options(build_parser()))


def planned_directories(command_name: str, repo_root: Path) -> set[str]:
    """The project directories a ``dev.py`` command really plans a step in.

    ``"."`` is the repo root itself (the reference-model chain and the ``scripts/``
    lint step both run there).
    """
    command = _DEV.COMMANDS[command_name]
    steps = (
        _DEV.plan_stages(dev_context(repo_root), command.stages)
        if command.stages
        else command.plan(dev_context(repo_root))
    )
    return {"." if step.cwd == repo_root else step.cwd.relative_to(repo_root).as_posix() for step in steps}


def inventory_violations(
    table: dict[str, Replacement],
    *,
    dev_commands: frozenset[str],
    scripts: frozenset[str],
    subcommands: frozenset[str],
    repo_root: Path,
) -> list[str]:
    """Everything wrong with the inventory's replacement side, as readable lines."""
    violations: list[str] = []
    for target in sorted(table):
        replacement = table[target]
        if replacement.kind not in _KINDS:
            violations.append(f"{target}: unknown replacement kind {replacement.kind!r} (expected one of {_KINDS})")
            continue
        if not replacement.note.strip():
            violations.append(f"{target}: replacement has no note saying how the behaviour is preserved")
        if replacement.kind == "dev":
            if replacement.target not in dev_commands:
                violations.append(f"{target}: maps to dev.py command {replacement.target!r}, which does not exist")
                continue
            if not replacement.covers:
                violations.append(f"{target}: a dev.py replacement must declare where it works (covers=...)")
                continue
            planned = planned_directories(replacement.target, repo_root)
            for directory in replacement.covers:
                if directory not in planned:
                    violations.append(
                        f"{target}: `dev.py {replacement.target}` plans no step in {directory!r} "
                        f"(it plans {sorted(planned)})"
                    )
        elif replacement.kind == "npm":
            if replacement.target not in scripts:
                violations.append(
                    f"{target}: maps to npm script {replacement.target!r}, which package.json does not declare"
                )
            if replacement.covers:
                violations.append(f"{target}: an npm replacement is checked against package.json, not against covers=")
        elif replacement.target not in subcommands:
            violations.append(f"{target}: maps to `swcraft {replacement.target}`, which the CLI does not provide")
    return violations


def unclassified_commands(
    table: dict[str, Replacement],
    *,
    dev_commands: frozenset[str],
    new_commands: set[str],
) -> set[str]:
    """``dev.py`` commands that neither replace a Moon task nor are recorded as new."""
    replaced = {replacement.target for replacement in table.values() if replacement.kind == "dev"}
    return set(dev_commands) - replaced - new_commands


def _violations(table=None) -> list[str]:
    """Run :func:`inventory_violations` against the live surfaces."""
    return inventory_violations(
        REPLACED_MOON_TASKS if table is None else table,
        dev_commands=frozenset(_DEV.COMMANDS),
        scripts=npm_scripts(REPO_ROOT),
        subcommands=cli_subcommands(),
        repo_root=REPO_ROOT,
    )


class TestEveryReplacementIsReal:
    def test_the_inventory_maps_only_onto_surfaces_that_exist(self):
        assert _violations() == []

    def test_every_dev_command_is_classified(self):
        """Neither a replacement nor recorded as new is an unclassified command."""
        assert (
            unclassified_commands(
                REPLACED_MOON_TASKS,
                dev_commands=frozenset(_DEV.COMMANDS),
                new_commands=set(_COMMANDS_WITHOUT_A_MOON_PREDECESSOR),
            )
            == set()
        )

    def test_nothing_is_recorded_as_new_that_is_also_a_replacement(self):
        replaced = {replacement.target for replacement in REPLACED_MOON_TASKS.values() if replacement.kind == "dev"}
        assert replaced & set(_COMMANDS_WITHOUT_A_MOON_PREDECESSOR) == set()

    def test_every_recorded_new_command_exists_and_is_explained(self):
        for name, reason in _COMMANDS_WITHOUT_A_MOON_PREDECESSOR.items():
            assert name in _DEV.COMMANDS, f"{name} is recorded as new but is not a dev.py command"
            assert reason.strip(), f"{name} is recorded as new with no reason"

    def test_only_the_serve_convenience_falls_outside_dev_py_and_npm(self):
        """Everything that was a *gate* has a ``dev.py`` or npm counterpart."""
        cli_backed = {target for target, replacement in REPLACED_MOON_TASKS.items() if replacement.kind == "cli"}
        assert cli_backed == {"mcp_server:serve"}


class TestTheInventoryCannotGoStale:
    """The guard fires on every kind of bad mapping."""

    def test_a_mapping_to_a_nonexistent_dev_command_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["tools:test"] = Replacement("dev", "test-everything", "renamed away", ("tools",))
        assert any("does not exist" in line for line in _violations(table=table))

    def test_a_mapping_to_a_nonexistent_npm_script_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["vscode_extension:build"] = Replacement("npm", "bundle", "renamed away")
        assert any("package.json does not declare" in line for line in _violations(table=table))

    def test_a_mapping_to_a_nonexistent_cli_subcommand_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["mcp_server:serve"] = Replacement("cli", "serve", "renamed away")
        assert any("the CLI does not provide" in line for line in _violations(table=table))

    def test_a_dev_mapping_that_does_no_work_in_that_project_fails(self):
        """The mapped command must really plan a step there, not merely sound right."""
        table = dict(REPLACED_MOON_TASKS)
        table["mcp_server:coverage"] = Replacement("dev", "benchmark", "wrong command", ("packages/mcp",))
        assert any("plans no step in 'packages/mcp'" in line for line in _violations(table=table))

    def test_a_dev_mapping_with_no_covers_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["agent:test"] = Replacement("dev", "test", "unlocated")
        assert any("must declare where it works" in line for line in _violations(table=table))

    def test_a_replacement_without_a_note_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["agent:lint"] = Replacement("dev", "lint", "   ", ("agent",))
        assert any("no note" in line for line in _violations(table=table))

    def test_an_unknown_kind_fails(self):
        table = dict(REPLACED_MOON_TASKS)
        table["agent:lint"] = Replacement("wishful", "lint", "not a kind", ("agent",))
        assert any("unknown replacement kind" in line for line in _violations(table=table))

    def test_a_brand_new_unclassified_command_fails(self):
        unclassified = unclassified_commands(
            REPLACED_MOON_TASKS,
            dev_commands=frozenset({*_DEV.COMMANDS, "publish"}),
            new_commands=set(_COMMANDS_WITHOUT_A_MOON_PREDECESSOR),
        )
        assert unclassified == {"publish"}


# ---------------------------------------------------------------------------
# Fan-out: dev.py test / lint / install cover what the root sequences covered
# ---------------------------------------------------------------------------


def plan_units(steps, repo_root: Path) -> list[str]:
    """The units a ``dev.py`` plan covers, in order.

    A step in a project directory is that project; a step at the repo root names its
    target as its last argument. Left out: :data:`_POST_MOON_PROJECTS`, the pytest step
    of :data:`_POST_MOON_TEST_PROJECTS`, and ``lint``'s import-graph and quality-ratchet
    steps, which cover no unit.
    """
    units: list[str] = []
    for step in steps:
        if step.label in (_DEV.IMPORT_LINT_LABEL, _DEV.QUALITY_RATCHET_LABEL):
            continue
        if step.cwd == repo_root:
            units.append(Path(str(step.argv[-1])).name)
        elif step.cwd.parent == repo_root and step.cwd.name in _POST_MOON_TEST_PROJECTS:
            continue
        elif step.cwd.name not in _POST_MOON_PROJECTS:
            units.append(step.cwd.name)
    return units


class TestFanOutIsPreserved:
    def test_dev_test_covers_the_same_targets_as_the_verify_sequence(self, tmp_path):
        assert plan_units(_DEV.plan_test(dev_context(tmp_path)), tmp_path) == _VERIFY_FAN_OUT

    def test_dev_lint_covers_the_same_targets_as_the_lint_sequence(self, tmp_path):
        """``dev.py lint`` covers the same targets as the root lint sequence, compared as a set.

        ``dev.py lint`` lints ``scripts/`` before the extension, the root sequence did it after, and neither
        order is part of the contract.
        """
        assert set(plan_units(_DEV.plan_lint(dev_context(tmp_path)), tmp_path)) == _LINT_FAN_OUT

    def test_dev_lint_keeps_the_python_projects_in_dependency_order(self, tmp_path):
        runner = str(tmp_path / "scripts" / "run_python_lint.py")
        units = [step.cwd.name for step in _DEV.plan_lint(dev_context(tmp_path)) if runner in step.argv]
        assert [unit for unit in units if unit in _DEV.PYTHON_PROJECTS] == list(_DEV.PYTHON_PROJECTS)

    def test_every_post_moon_project_is_a_project_of_dev_py(self):
        """The exclusion names real projects, so it cannot hide a Moon-era one by a typo."""
        assert _POST_MOON_PROJECTS <= set(_DEV.PYTHON_PROJECTS)
        assert _POST_MOON_TEST_PROJECTS <= set(_DEV.TEST_ONLY_PROJECTS)
        assert not _POST_MOON_PROJECTS & (_BOOTSTRAP_FAN_OUT | _LINT_FAN_OUT)

    def test_dev_install_covers_the_same_targets_as_the_bootstrap_sequence(self, tmp_path):
        units = set(plan_units(_DEV.plan_install(dev_context(tmp_path)), tmp_path))
        assert _BOOTSTRAP_FAN_OUT <= units, f"dev.py install misses {sorted(_BOOTSTRAP_FAN_OUT - units)}"

    def test_the_comparison_fires_when_a_project_is_dropped(self, tmp_path):
        """A plan missing one project must not compare equal to the frozen fan-out."""
        steps = [step for step in _DEV.plan_test(dev_context(tmp_path)) if step.cwd.name != "agent"]
        assert plan_units(steps, tmp_path) != _VERIFY_FAN_OUT
