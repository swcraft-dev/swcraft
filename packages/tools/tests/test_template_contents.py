"""Guard tests for the ``vehicle-architecture`` project template.

The template is the single editable copy of everything a standalone architecture project needs: its Moon build
graph, its ``.prototools`` and ``.moon/`` pins, its ``swcraft.yaml`` (with the ``toolchain_version`` pin), the editor
and agent instructions, and its README. ``swcraft init --integration moon`` copies it out of the installed wheel, so
these files are shipped code; :data:`EXPECTED_FILES` is the authority on the file set.

1. **Build ordering is part of the contract.** ``clean-outputs`` runs first and every generator depends on
   ``check-model``, so a build failing at ``check-model`` leaves no generated output but ``model_old.arxml``; later
   failures: :class:`TestTheFailedBuildRestatement`.
2. **``clean-outputs`` must disable caching**: it deletes files and declares no outputs, so a cache hit could skip
   the deletion (:func:`cache_violations`).
3. **The project installs nothing**: every command-bearing task runs the bare ``swcraft`` and the template ships no
   executable file; :data:`FORBIDDEN_PROJECT_TOOLCHAIN` names the spellings a regression would reintroduce.
4. **The project pins a version, not a place**: nothing committed records where wheels, an installation or a
   checkout live.
5. **No pipeline definition of any kind**, and the template's README says so.

The rule checks are pure functions over the parsed YAML, so each can be shown to fire against a mutated copy.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, TEMPLATE_ROOT, dev_module, parser_subcommand_options
from swcraft_tools import __version__
from swcraft_tools import cli as swcraft_cli
from swcraft_tools.agent_clients import load_client_table
from swcraft_tools.agents_md import (
    HOST_PREAMBLE,
    KIRO_PREAMBLE,
    MARKDOWN_MARKER,
    TOML_MARKER,
    profile_body,
    render_agents_md,
    render_kiro_steering,
)
from swcraft_tools.build_report import BUILD_REPORT_FILE
from swcraft_tools.cli.build_commands import _clean_output_problems, _clean_root_problems
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.cli.init_command import _default_file_plan
from swcraft_tools.copilot_guard import CLAUDE_DENIED_TOOLS, CLAUDE_SHELL_TOOLS, CLAUDE_WRITE_TOOLS
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_compositions

#: The template files outside every row of the client table: ``swcraft init --integration moon``
#: writes them whichever clients are chosen.
SHARED_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        ".moon/toolchain.yml",
        ".moon/workspace.yml",
        ".prototools",
        ".vscode/settings.json",
        "README.md",
        "model/.gitkeep",
        "model/compositions/Root.yaml",
        "model/hardware/CORE0.yaml",
        "model/hardware/CPU.yaml",
        "model/hardware/PARTITION0.yaml",
        "moon.yml",
        "swcraft.yaml",
    }
)


def expected_files(clients: tuple[str, ...]) -> frozenset[str]:
    """The files ``swcraft init --integration moon`` writes for *clients*, read from the client table.

    :class:`TestClientSets` pins each set against a literal of its own, so a row of the table that
    moves a file fails there and not only here.
    """
    return SHARED_FILES | load_client_table().files_for(clients)


#: Every file the template ships, relative to its root: the shared files and every client's. A set
#: rather than a "these must exist" list: an unexpected extra file (a stray ``__pycache__``, a
#: pipeline definition, a developer's scratch note) fails just as loudly as a missing one.
EXPECTED_FILES = expected_files(load_client_table().names())

#: The model elements ``swcraft init`` scaffolds, derived from the file plan: every
#: plan entry under ``model/`` is one, so a new one is compared with no edit here.
SCAFFOLDED_MODEL_ELEMENTS: tuple[str, ...] = tuple(
    sorted(relative for relative in _default_file_plan("proj") if relative.startswith("model/"))
)

#: The template's agent instructions: template-authored, held to every
#: authored-content rule below; the example's copies are `swcraft init`'s output
#: of them, byte for byte (`test_example_is_init_output.py`).
AGENT_INSTRUCTION_FILES = (
    ".github/agents/swcraft-architect.agent.md",
    ".github/copilot-instructions.md",
)


#: Sentences that must read identically in the repository's Copilot instructions
#: and the template's. The prose around them legitimately differs, which is why
#: these files are compared by sentence and not byte for byte.
SHARED_AGENT_STATEMENTS = (
    "AI-assisted changes to the architecture model must go through the `swcraft` MCP "
    "server's read/write/validate/build tools — never through direct edits to files under `model/`.",
    "**A change to the model is reviewed as a computed delta before it is applied.**",
    "**Only the later review question displays that computed delta, and only an "
    "affirmative answer to it commits the change.**",
    "`Allow in this Session` affects tool execution only and never replaces either review answer.",
    "there is no delete tool and no confirmation token",
    # Pass D refuses a created component without it, and a Copilot host reads these
    # files and never the standalone agent's system prompt.
    "A component a change set creates carries `handle_termination_and_restart` — "
    "`no_support` unless the user states otherwise — and the user is told which value was chosen.",
)

#: Sentences that must read identically in the repository's architect profile and
#: the template's. They forbid resubmitting a declined weakening with `intent`
#: rewritten to claim the user authorized it. The rules are prose-only by
#: construction (an approval binds to one change-set id, so nothing can detect the
#: resubmission), so they are pinned in the documents, in both roots.
SHARED_PROFILE_STATEMENTS = (
    # The fifth weakening direction: `packages/mcp/tests/test_vscode_agent_profile.py`
    # pins the `.github/` copy alone, so these rows keep the scaffolded prompt from
    # drifting back to four directions.
    "**Admitting** a core to a memory's",
    "**Withdrawing** a core from the list is the inverse and is not a weakening",
    "**A declined safety answer is final for that change set.**",
    "Do not resubmit the same weakening, do not reword it",
    "**Nothing enforces this for you.**",
    "the server cannot know a previous change set was declined",
    "**`intent` says what the change set is *for*, and never who agreed to it:**",
    "a false `intent` is a false statement made to the person deciding",
    # A Copilot host loads this profile and no system prompt, so without the line
    # its first `create` of a component is refused once by pass D.
    "A component you create carries `handle_termination_and_restart` — `no_support` "
    "unless the user states otherwise — and you tell the user which value you chose.",
)

#: The bounded-read guidance, in both roots: a `read` at a layer or a collection
#: answers a `summary`, and a response over the wire budget is refused with
#: `over-budget` rather than truncated. Without it an agent asks for a full layer
#: and retries a refusal it has no rule for; a Copilot host reads this profile and
#: never the agent package's system prompt.
BOUNDED_READ_STATEMENTS = (
    "**The `form` says how wide the answer is — never how deep, and nothing is ever truncated.**",
    "a layer and a collection answer `summary`, an element and a field answer `full`",
    "**Read narrow to wide, and prefer a slice to a whole set: the filtered or paged "
    "summary first, then the element, then the field.**",
    "A layer read in `full` is something you ask for knowing the count",
    "**`over-budget` is an answer about size, not a failure to retry around.**",
    "replay `hints[0]` as it stands",
    "**No `hints` key means the chain is exhausted**",
)

#: The model-query guidance, in both roots: a `read` takes `where`,
#: `limit`/`after` and `members`; an undeclared field is `unknown-field`, never an
#: empty answer; a refusal at a set address ends with a page of that address, and
#: descending into a collection is the agent's own move.
#:
#: These are statements about what the server does; nothing here proves it does
#: it. `packages/tools/tests/test_editing_read.py`, `test_editing_budget.py` and
#: `packages/tools/tests/test_discover_budget.py` are that oracle, and
#: `packages/agent/tests/test_acceptance.py` holds the same guidance in the standalone
#: agent's system prompt.
# No `discover` summary sentence: the template's profile carries the schema and
# asks `discover` for the baseline alone, so the two profiles differ there.
MODEL_QUERY_STATEMENTS = (
    "**Ask for a slice, not a layer.**",
    "**`where`** filters by declared fields",
    "an undeclared name is answered `unknown-field`, never an empty result",
    "**`limit` and `after`** page the answer",
    "**`members`** answers **one collection across a whole layer**",
    "the chain's last step is **the first page of that same address**",
    "**Descending into a collection is your move, never a hint.**",
)

#: The rules of the template's "Questions about the model" section. The Copilot
#: instructions are the one file Copilot reads in every mode, the general Agent
#: mode included, which has a terminal and may otherwise parse the model's YAML
#: with a script. These sentences are guidance only; what
#: enforces anything is the SWCraft Architect agent's `tools:` list, which
#: :meth:`TestAgentInstructions.test_the_custom_agent_grants_the_tools_its_root_declares`
#: pins, and the template's `.github/hooks/swcraft.json`, which runs `swcraft guard`
#: before each tool call (:meth:`TestAgentInstructions.test_the_hook_file_runs_the_guard_before_each_tool_call`).
#:
#: What this does not reach: whether a model obeys them (the manual Copilot
#: protocol is the only check of that), whether the host honours the hook (the
#: same manual check), and the repository's own `.github/` pair, which serves
#: SWCraft's development and does not carry the section.
MODEL_QUESTION_RULES = (
    "These rules hold in every Copilot mode, the general Agent mode included",
    "**Answer a question about the model through the SWCraft tools** — `discover`, `read` and `impact_of`.",
    "**Never read or parse the model's files with a script, the terminal or a file tool** "
    "to answer one, not even when the answer is larger than one reply.",
    "**Page a large answer with `limit` and `after`.** For a whole layer table in `index` or `full` form, "
    "give the user the `swcraft query --request FILE --project PATH` command instead (PATH is the project's "
    "`swcraft.yaml`)",
    "**Read a SWCraft answer saved to a file from that file.**",
    "never read the model's files directly instead.",
    "The project's hook, `.github/hooks/swcraft.json`, runs `swcraft guard` before each tool call and denies "
    "a read or search by the file tools it knows when it reaches the model directory, in every mode; it does not "
    "judge a terminal command, and the user guide lists its other limits.",
    "**Use the SWCraft Architect agent for model work.**",
    "what enforces anything is that agent's `tools:` list, which grants the SWCraft tools and `view` for a "
    "saved answer and no terminal or file edit, and the hook above, which keeps `view` off the model "
    "directory only: viewing nothing but a saved answer is this instruction's, not the hook's.",
)

#: The heading of the profile section that teaches the saved answer. The template's profile and the extension's
#: bundled copy carry it; the repository's own profile and the standalone
#: assistant's system prompt do not, because neither grants `view`.
SAVED_ANSWER_HEADING = "## A SWCraft answer saved to a file"

#: The exact `tools:` grant of each architect profile, keyed by its root.
ARCHITECT_TOOL_GRANTS = {
    # `view` reads a SWCraft answer Copilot saved to a file; the hook shipped
    # beside the profile denies a read of the model directory.
    "template": ["swcraft/*", "view"],
    # The example's profile is what `swcraft init` writes.
    "example": ["swcraft/*", "view"],
}

#: The generic editing protocol's four tools. The template's agent profile has to
#: name all four — they are the whole authoring path.
PROTOCOL_TOOLS = ("discover", "read", "find", "change_set")

#: The name sets of the frozen tool decision table. Its `replaced` set holds the retired
#: tools: the template's agent instructions may not teach any of those names.
TOOL_PROJECTIONS = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol" / "tool-projections.json"
)

#: Tasks that only ever *read* — they mutate nothing, so caching them cannot
#: leave stale state behind and they are exempt from :func:`cache_violations`.
#: Adding a task means either declaring `outputs`, declaring `cache: false`, or
#: adding it here with a reason; the rule forces that decision rather than
#: letting a mutating task default into a cache. ``validate-arxml`` writes the
#: ARXML and the build report, so its ``cache: false`` exempts it instead.
READ_ONLY_TASKS = frozenset({"check-model", "build"})

#: The glob every task that regenerates an artifact must declare, so a change to
#: any model layer invalidates its cache (an output can read layers beyond the
#: obvious one: needs.json reads the linked layers).
MODEL_INPUT_GLOB = "model/**/*.yaml"

#: The one file that carries the toolchain version. Every task moon may serve from
#: a cache must declare it: without it, bumping the pin and installing the new
#: archive leaves the whole graph cached, so moon hydrates the previous version's
#: artifacts and exits 0.
PIN_INPUT = "swcraft.yaml"

#: The exact command every command-bearing ``moon.yml`` task must run. Not a path:
#: the workstation archive puts one ``swcraft`` per machine on ``PATH``, and a
#: path would name where the toolchain lives. ``build`` runs nothing, so the rule
#: covers the tasks that carry a ``command`` — six of the seven.
#: See :func:`TestMoonBuildGraph.test_every_moon_task_disables_moons_own_shell`
#: for the other half of the Windows spelling.
PATH_COMMAND = "swcraft"

#: Spellings that would put a toolchain back inside the project: each is a shape a
#: regression has to reintroduce, so the rule does not depend on files that exist.
FORBIDDEN_PROJECT_TOOLCHAIN = (
    ".venv",
    "bootstrap.py",
    "requirements-swcraft.txt",
    "clean_outputs.py",
    "python -m swcraft",
    "--find-links",
    "pip install",
)

#: Paths that would tie the template to a SWCraft source checkout.
FORBIDDEN_CHECKOUT_PATHS = (
    "packages/tools/src",
    "packages/mcp/src",
    "packages/agent/src",
    "vscode_extension/",
    "examples/basic",
    "scripts/dev.py",
    "scripts/build_artifacts.py",
    "../",
)

#: Paths that would hardcode *where the wheels came from* into a committed file.
FORBIDDEN_ARTIFACT_PATHS = ("dist/python", "dist/vscode", "dist/wheelhouse")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def template_files() -> dict[str, Path]:
    """Return ``{relative posix path: absolute path}`` for every template file."""
    found: dict[str, Path] = {}
    for path in sorted(TEMPLATE_ROOT.rglob("*"), key=lambda path: path.as_posix()):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        found[path.relative_to(TEMPLATE_ROOT).as_posix()] = path
    return found


def normalized(text: str) -> str:
    """Whitespace-collapsed text, so a line wrap cannot hide a prose claim."""
    return " ".join(text.split())


def _assert_in_both_architect_profiles(statement: str) -> None:
    """*statement* is present in the example's and the template's agent profile."""
    for root, whose in ((REPO_ROOT / "examples" / "basic", "the example's"), (TEMPLATE_ROOT, "the template's")):
        text = normalized((root / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8"))
        assert normalized(statement) in text, f"{whose} architect profile lost: {statement!r}"


def _saved_answer_section(path: Path) -> str | None:
    """The :data:`SAVED_ANSWER_HEADING` section of *path*, up to the next ``## `` heading.

    ``None`` when the file has no such heading."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\n") == SAVED_ANSWER_HEADING]
    if not starts:
        return None
    assert len(starts) == 1, f"{path.name} carries {SAVED_ANSWER_HEADING!r} {len(starts)} times"
    body = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "".join(body)


def moon_yml() -> dict:
    return yaml.safe_load((TEMPLATE_ROOT / "moon.yml").read_text(encoding="utf-8"))


def tasks() -> dict[str, dict]:
    return moon_yml()["tasks"]


def file_groups() -> dict[str, list[str]]:
    """The project-level ``fileGroups`` every task's ``inputs`` reference."""
    return moon_yml().get("fileGroups", {})


def resolved_inputs(task: dict, groups: dict[str, list[str]] | None = None) -> list[str]:
    """A task's ``inputs`` with every ``@group(name)`` token expanded: what moon actually hashes."""
    groups = file_groups() if groups is None else groups
    expanded: list[str] = []
    for entry in task.get("inputs") or []:
        match = re.fullmatch(r"@group\((\w+)\)", entry)
        if match is None:
            expanded.append(entry)
            continue
        assert match.group(1) in groups, f"inputs reference an undeclared file group: {entry}"
        expanded.extend(groups[match.group(1)])
    return expanded


def transitive_deps(all_tasks: dict[str, dict], name: str) -> set[str]:
    """Every task reachable through ``deps`` from *name*."""
    seen: set[str] = set()
    pending = list(all_tasks[name].get("deps") or [])
    while pending:
        dep = pending.pop()
        if dep in seen:
            continue
        seen.add(dep)
        pending.extend(all_tasks[dep].get("deps") or [])
    return seen


def cache_violations(all_tasks: dict[str, dict]) -> list[str]:
    """Tasks moon may skip via a cache hit while they still needed to run.

    A task that declares ``outputs`` is safe (moon hydrates them from the cache
    on a hit). A task that declares ``options.cache: false`` is safe (it always
    runs). Anything else that is not a declared read-only task can be skipped
    while leaving the filesystem in whatever state the previous run left it.
    """
    violations = []
    for name, task in sorted(all_tasks.items()):
        if name in READ_ONLY_TASKS or task.get("outputs"):
            continue
        if (task.get("options") or {}).get("cache") is False:
            continue
        violations.append(name)
    return violations


def pin_input_violations(all_tasks: dict[str, dict]) -> list[str]:
    """Cacheable tasks whose ``inputs`` do not cover the toolchain pin.

    A task with ``inputs: []`` is exempt: it has no inputs and always runs
    (``clean-outputs``, the ``build`` aggregate). Asserted over every task with
    inputs, not only the artifact-producing ones: ``validate-arxml`` produces
    none and would otherwise be served from its cache after a pin bump.
    """
    return [
        name
        for name, task in sorted(all_tasks.items())
        if task.get("inputs") and PIN_INPUT not in resolved_inputs(task)
    ]


def model_input_violations(all_tasks: dict[str, dict]) -> list[str]:
    """Artifact-producing tasks whose ``inputs`` do not cover the whole model."""
    return [
        name
        for name, task in sorted(all_tasks.items())
        if task.get("outputs") and MODEL_INPUT_GLOB not in resolved_inputs(task)
    ]


# ---------------------------------------------------------------------------
# File set
# ---------------------------------------------------------------------------


class TestTemplateFileSet:
    """Every file the template ships, and nothing else."""

    def test_template_ships_exactly_the_expected_files(self) -> None:
        assert set(template_files()) == set(EXPECTED_FILES)

    def test_the_template_ships_nothing_executable(self) -> None:
        """A project that installs nothing ships no ``.py`` script under any name.

        Does not reach a shell script, a ``.cmd`` or an executable bit; the file
        set above is what would notice one.
        """
        scripts = sorted(name for name in template_files() if name.endswith(".py"))
        assert scripts == [], f"the template ships executable scripts again: {scripts}"

    def test_the_scaffolded_model_elements_are_byte_identical_to_the_default_plan(self) -> None:
        """``swcraft init`` with and without ``--integration moon`` scaffolds the same model bytes.

        Bytes rather than parsed documents: a difference in spelling or line ending
        is one a user would see in `git diff`.
        """
        plan = _default_file_plan("proj")

        for relative in SCAFFOLDED_MODEL_ELEMENTS:
            assert (TEMPLATE_ROOT / relative).read_text(encoding="utf-8") == plan[relative]

    def test_the_scaffolded_model_elements_carry_no_project_name_placeholder(self) -> None:
        """The scaffolded model elements carry no ``{{PROJECT_NAME}}``.

        The profile-less path writes them verbatim with no substitution, so a
        placeholder would make the two paths diverge.
        """
        for relative in SCAFFOLDED_MODEL_ELEMENTS:
            assert "{{PROJECT_NAME}}" not in (TEMPLATE_ROOT / relative).read_text(encoding="utf-8")

    def test_the_scaffolded_root_composition_is_the_only_root_and_declares_no_member(self) -> None:
        """A fresh project carries exactly one root composition, titled, with no member.

        No member: a member instantiates a declared component type and a fresh
        project has none.
        """
        declared = read_compositions(str(TEMPLATE_ROOT / "model"))

        assert [composition.name for composition in declared] == ["Root"]
        assert declared[0].root is True
        assert declared[0].title == "Root composition"
        assert declared[0].members == []
        assert declared[0].ports == []
        assert declared[0].delegations == []

    def test_the_reader_facing_documents_name_the_scaffolded_root_composition(self) -> None:
        """The template's README and Copilot instructions name the scaffolded root composition.

        It is the one scaffolded element whose absence the model gate refuses
        outright, so a reader must be able to find it in the project's documents.
        The path is derived from the scaffold plan.

        Does not reach: whether what either document says about it is true; the
        hardware elements (named in prose, not by path); the rule the file
        satisfies, which `test_validator_compositions.py` asserts.
        """
        root_relative = next(
            relative for relative in SCAFFOLDED_MODEL_ELEMENTS if relative.startswith("model/compositions/")
        )
        # `Root.yaml` alone would also match another layer's file of that name.
        named = root_relative.removeprefix("model/")

        for relative in (".github/copilot-instructions.md", "README.md"):
            text = (TEMPLATE_ROOT / relative).read_text(encoding="utf-8")
            assert named in text, (
                f"the template's {relative} never names {named}, the one scaffolded element "
                "whose absence the model gate refuses outright"
            )

    def test_template_directory_lives_inside_the_package(self) -> None:
        """The template is package data, resolvable through importlib.resources from an install or a wheel."""
        assert TEMPLATE_ROOT.is_dir()
        assert TEMPLATE_ROOT.parent.parent.name == "swcraft_tools"


class TestClientSets:
    """What ``swcraft init --integration moon`` writes per chosen client set.

    Each client's files are restated as a literal, independently of the client table, and each set
    is scaffolded for real and compared as a set. Does not reach Onboard Project's choice (the
    extension's tests).
    """

    #: Each client's files, restated independently of the client table.
    COPILOT = frozenset(
        {".github/agents/swcraft-architect.agent.md", ".github/copilot-instructions.md", ".github/hooks/swcraft.json"}
    )
    CODEX = frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"})
    CLAUDE = frozenset({"CLAUDE.md", "AGENTS.md", ".mcp.json", ".claude/settings.json"})
    KIRO = frozenset({".kiro/settings/mcp.json", ".kiro/steering/swcraft.md", ".kiro/hooks/swcraft-guard.json"})
    COMPANION = {"chat.useAgentsMdFile": False, "chat.useClaudeMdFile": False}

    CASES = (
        pytest.param((), COPILOT, False, id="default"),
        pytest.param(("copilot",), COPILOT, False, id="copilot"),
        pytest.param(("codex",), CODEX, False, id="codex"),
        pytest.param(("claude",), CLAUDE, False, id="claude"),
        pytest.param(("copilot", "codex", "claude"), COPILOT | CODEX | CLAUDE, True, id="all-three"),
        pytest.param(("copilot", "codex"), COPILOT | CODEX, True, id="copilot-codex"),
        # Kiro: no AGENTS.md of its own, and no companion key with Copilot (Kiro reads no `.vscode/`,
        # Copilot no `.kiro/`).
        pytest.param(("kiro",), KIRO, False, id="kiro"),
        pytest.param(("kiro", "copilot"), KIRO | COPILOT, False, id="kiro-copilot"),
        pytest.param(("kiro", "codex"), KIRO | CODEX, False, id="kiro-codex"),
        pytest.param(("kiro", "claude"), KIRO | CLAUDE, False, id="kiro-claude"),
        pytest.param(("copilot", "codex", "claude", "kiro"), COPILOT | CODEX | CLAUDE | KIRO, True, id="all-four"),
    )

    def test_the_table_names_the_four_clients_with_copilot_the_default(self) -> None:
        table = load_client_table()
        assert table.names() == ("copilot", "codex", "claude", "kiro")
        assert table.default == ("copilot",)

    @pytest.mark.parametrize(("clients", "client_files", "companion"), CASES)
    def test_init_writes_the_shared_files_and_the_chosen_clients(
        self,
        clients: tuple[str, ...],
        client_files: frozenset[str],
        companion: bool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "project"
        words = [word for name in clients for word in ("--client", name)]
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), "--integration", "moon", *words])
        swcraft_cli.main()

        written = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
        assert written == SHARED_FILES | client_files
        assert written == expected_files(clients or load_client_table().default)
        settings = json.loads((target / ".vscode" / "settings.json").read_text(encoding="utf-8"))
        held = {key: settings[key] for key in self.COMPANION if key in settings}
        assert held == (self.COMPANION if companion else {})

    def test_every_client_file_is_a_template_file_and_no_shared_file_is_a_client_s(self) -> None:
        table = load_client_table()
        assert table.all_client_files() <= set(template_files())
        assert not table.all_client_files() & SHARED_FILES
        assert set(template_files()) == SHARED_FILES | table.all_client_files()

    def test_each_hook_and_server_file_is_its_clients_own(self) -> None:
        table = load_client_table()
        assert {client.name: (client.hook, client.server) for client in table.clients} == {
            "copilot": (".github/hooks/swcraft.json", None),
            "codex": (".codex/hooks.json", ".codex/config.toml"),
            "claude": (".claude/settings.json", ".mcp.json"),
            "kiro": (".kiro/hooks/swcraft-guard.json", ".kiro/settings/mcp.json"),
        }

    def test_the_companion_file_is_the_one_derived_write(self) -> None:
        assert load_client_table().companion.file == ".vscode/settings.json"
        assert load_client_table().companion.settings == self.COMPANION

    def test_the_readme_names_each_client_and_its_files(self) -> None:
        """The template README's per-client table: every client's flag value and each of its files."""
        prose = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")
        for client in load_client_table().clients:
            row = next((line for line in prose.splitlines() if line.startswith(f"| `{client.name}`")), None)
            assert row is not None, f"the README's client table has no row for {client.name}"
            for relative in client.files:
                assert f"`{relative}`" in row, f"the README's {client.name} row does not name {relative}"


# ---------------------------------------------------------------------------
# moon.yml — the build graph
# ---------------------------------------------------------------------------


class TestTemplateGitignore:
    """What the scaffolded project's ``.gitignore`` excludes, and what it deliberately does not.

    Does not reach a tool the user adds (a LikeC4 extension installed by hand brings its cache
    back, uncovered).
    """

    @pytest.mark.parametrize("entry", ["dist/", "views/", ".moon/cache/", ".env"])
    def test_the_template_ignores_the_entry(self, entry: str) -> None:
        """The generated and tool output directories, and ``.env``, which can hold secrets (the Azure keys)."""
        lines = [line.strip() for line in (TEMPLATE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        assert entry in lines, f"the template's .gitignore does not exclude {entry!r}"

    def test_the_experiment_configurations_are_the_users_to_commit(self) -> None:
        """No experiment entry is ignored: a configuration is the user's to commit; reports go under ``dist/``.

        Asserted as an absence: an absence nothing asserts is an entry the next merge restores.
        """
        lines = [line.strip() for line in (TEMPLATE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        assert "experiments/" not in lines
        assert not any("experiment" in line.lower() for line in lines)
        assert "dist/" in lines
        assert not (TEMPLATE_ROOT / "experiments").exists()

    def test_the_likec4_cache_is_not_ignored_because_no_pack_member_creates_it(self) -> None:
        """``.likec4/`` is not ignored: no member of the extension pack creates it."""
        lines = [line.strip() for line in (TEMPLATE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        assert ".likec4/" not in lines and ".likec4" not in lines, (
            "the template's .gitignore excludes .likec4/ again — no member of SWCraft's extension pack creates it"
        )

    def test_the_project_venv_is_not_ignored_because_there_is_none(self) -> None:
        """``.venv/`` is not ignored: the project installs nothing, so a reader must not be told to create one."""
        lines = [line.strip() for line in (TEMPLATE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        assert ".venv/" not in lines and ".venv" not in lines, (
            "the template's .gitignore excludes .venv/ again — a scaffolded project creates none"
        )


class TestTemplateGitattributes:
    """A scaffolded project inherits the LF rule.

    SWCraft writes LF everywhere; without this file a Windows checkout with
    ``core.autocrlf=true`` turns every later SWCraft write into a whole-file diff.

    Does not check whether Git honours the file (it has no effect until the
    directory is `git init`-ed).
    """

    def test_the_template_pins_lf_in_the_working_tree(self) -> None:
        lines = [line.strip() for line in (TEMPLATE_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()]
        assert "* text=auto eol=lf" in lines, (
            "the template's .gitattributes must pin the working tree to LF; `eol=lf` is the half "
            "that overrides core.autocrlf, and `text=auto` is the half that leaves binary "
            "detection in place"
        )

    def test_it_carries_the_reason_and_not_only_the_rule(self) -> None:
        """The file names what ``eol=lf`` overrides and why, so the next author does not delete it."""
        text = (TEMPLATE_ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert "core.autocrlf" in text, "the comment must name what eol=lf overrides"
        assert "SWCraft writes LF" in text, "the comment must say why the project's own writes depend on it"


class TestMoonBuildGraph:
    """The scaffolded project's build graph: ordering, caching, inputs."""

    def test_moon_yml_parses_and_declares_the_expected_tasks(self) -> None:
        document = moon_yml()
        assert document["$schema"].endswith("project.json")
        assert set(document["tasks"]) == {
            "clean-outputs",
            "check-model",
            "generate-arxml",
            "generate-diagrams",
            "generate-needs",
            "validate-arxml",
            "build",
        }

    def test_build_is_the_entry_point_and_reaches_every_task(self) -> None:
        """`build` runs no command of its own and reaches every other task through deps."""
        all_tasks = tasks()
        assert "command" not in all_tasks["build"]
        reachable = transitive_deps(all_tasks, "build")
        assert reachable == set(all_tasks) - {"build"}

    @pytest.mark.parametrize(
        ("earlier", "later"),
        [
            ("clean-outputs", "check-model"),
            ("check-model", "generate-arxml"),
            ("check-model", "generate-diagrams"),
            ("check-model", "generate-needs"),
            ("generate-arxml", "validate-arxml"),
            ("clean-outputs", "validate-arxml"),
        ],
    )
    def test_chain_order_is_declared_through_deps(self, earlier: str, later: str) -> None:
        assert earlier in transitive_deps(tasks(), later), (
            f"'{earlier}' must run before '{later}' — the ordering is what keeps a failed build from leaving "
            "outputs, not an optimization"
        )

    @pytest.mark.parametrize("generator", ["generate-arxml", "generate-diagrams", "generate-needs"])
    def test_every_generator_depends_on_check_model(self, generator: str) -> None:
        """Directly, not transitively, so re-pointing an intermediate task cannot drop the gate."""
        assert tasks()[generator].get("deps") == ["check-model"]

    def test_the_gate_waits_for_every_generator_writes_the_report_and_is_never_cached(self) -> None:
        """`validate-arxml` runs after every generator, writes the report and is never cached.

        Last, so the report never reads outputs still being written. `cache: false` because
        the verdict and the report depend on the licensed schema and on `dist/model_old.arxml`,
        which no input declares."""
        task = tasks()["validate-arxml"]
        assert task["args"] == ["validate", "--project", "swcraft.yaml", "--report"]
        assert sorted(task["deps"]) == ["generate-arxml", "generate-diagrams", "generate-needs"]
        assert task["outputs"] == ["dist/model.arxml", f"dist/{BUILD_REPORT_FILE}"]
        assert task["options"].get("cache") is False
        assert "validate-arxml" not in READ_ONLY_TASKS

    def test_clean_outputs_disables_caching(self) -> None:
        task = tasks()["clean-outputs"]
        assert task.get("options", {}).get("cache") is False, (
            "clean-outputs deletes files and declares no outputs: with caching on, moon can "
            "serve a hit from the previous successful build and skip the deletion, leaving that "
            "run's artifacts behind after a failing rebuild"
        )

    def test_no_task_can_be_cache_skipped_while_still_needing_to_run(self) -> None:
        assert cache_violations(tasks()) == []

    def test_cache_rule_fires_on_a_mutated_copy(self) -> None:
        mutated = tasks()
        del mutated["clean-outputs"]["options"]
        assert cache_violations(mutated) == ["clean-outputs"]

    def test_every_generation_task_inputs_cover_the_whole_model(self) -> None:
        assert model_input_violations(tasks()) == []

    def test_model_input_rule_fires_on_a_mutated_copy(self) -> None:
        mutated = tasks()
        mutated["generate-needs"]["inputs"] = ["model/requirements/**/*.yaml"]
        assert model_input_violations(mutated) == ["generate-needs"]

    def test_every_cacheable_task_invalidates_on_a_toolchain_upgrade(self) -> None:
        """A new `toolchain_version` in `swcraft.yaml` must not be served from the cache."""
        assert pin_input_violations(tasks()) == []

    def test_pin_input_rule_fires_on_a_mutated_copy(self) -> None:
        mutated = tasks()
        mutated["generate-arxml"]["inputs"] = ["model/**/*.yaml"]
        assert pin_input_violations(mutated) == ["generate-arxml"]

    def test_the_build_graph_and_the_manifest_agree_on_where_artifacts_land(self) -> None:
        """`moon.yml`'s `outputs:` and `swcraft.yaml`'s roots agree; nothing at runtime reconciles them.

        `clean-outputs` reads `dist_root`/`views_root` from the manifest, so its task passes no
        root. Its own refusals are `test_cli.py`'s."""
        manifest = yaml.safe_load(
            (TEMPLATE_ROOT / "swcraft.yaml").read_text(encoding="utf-8").replace("{{PROJECT_NAME}}", "placeholder")
        )
        roots = {manifest["dist_root"], manifest["views_root"]}
        all_tasks = tasks()

        clean_args = all_tasks["clean-outputs"]["args"]
        assert clean_args[0] == "clean-outputs", "the clean step goes through the product subcommand"
        assert not roots & set(clean_args), (
            f"clean-outputs names an output root in its args ({clean_args}); it reads them from the manifest"
        )

        # The diagrams have no `outputs` key: `views_root` is their one address. The build report is
        # no manifest output: its name is fixed, in `dist_root`, which `clean-outputs` empties.
        report = f"{manifest['dist_root']}/{BUILD_REPORT_FILE}"
        declared = {manifest["outputs"]["arxml"], manifest["outputs"]["needs"], f"{manifest['views_root']}/", report}
        produced = {output for task in all_tasks.values() for output in (task.get("outputs") or [])}
        assert produced == declared, "every moon `outputs:` entry must be an artifact the manifest declares"
        assert all_tasks["generate-diagrams"]["outputs"] == [f"{manifest['views_root']}/"]

    def test_the_shipped_manifest_generates_only_into_the_roots_clean_outputs_removes(self, tmp_path: Path) -> None:
        """`clean-outputs` accepts the template's own manifest.

        It refuses an `outputs:` entry outside `dist_root` and `views_root`, and every other case
        uses a synthetic manifest, so this is the rule's oracle on the shipped file. Reaches the two
        refusals `cmd_clean_outputs` composes and nothing else.
        """
        project_dir = tmp_path / "scaffolded"
        (project_dir / "model").mkdir(parents=True)
        (project_dir / "swcraft.yaml").write_text(
            (TEMPLATE_ROOT / "swcraft.yaml").read_text(encoding="utf-8").replace("{{PROJECT_NAME}}", "placeholder"),
            encoding="utf-8",
            newline="\n",
        )
        project = require_project(str(project_dir / "swcraft.yaml"))

        assert _clean_output_problems(project) == []
        assert _clean_root_problems(project) == []

    def test_every_referenced_file_group_is_declared(self) -> None:
        """`model` is the one file group, and every `@group(...)` reference resolves."""
        groups = file_groups()
        assert set(groups) == {"model"}, groups
        for task in tasks().values():
            resolved_inputs(task, groups)

    def test_every_task_declares_inputs(self) -> None:
        """Every task declares `inputs`: without it moon hashes every file in the project.

        `.moon/cache/`, `dist/` and `views/` would be hashed, and an unbounded input
        set makes a cache key mean nothing. A task with none declares `inputs: []`."""
        for name, task in tasks().items():
            assert "inputs" in task, f"task '{name}' must declare inputs (use [] when it truly has none)"

    def test_every_command_bearing_task_runs_the_installed_swcraft(self) -> None:
        """Each of the six command-bearing tasks runs exactly the bare `swcraft` from `PATH`.

        Exact equality: never a path, an interpreter plus `-m` or a `.venv` spelling.

        Does not reach whether moon resolves a bare `swcraft` from `PATH` with
        `options.shell: false` on Windows; nothing in this repository measures that."""
        commanded = {name: task["command"] for name, task in tasks().items() if "command" in task}
        assert len(commanded) == 6, f"expected six command-bearing tasks, found {sorted(commanded)}"
        assert "build" not in commanded, "`build` is the aggregate — it must run no command of its own"
        for name, command in sorted(commanded.items()):
            assert command == PATH_COMMAND, (
                f"task '{name}' must run the installed toolchain as the bare {PATH_COMMAND!r} — "
                f"not a path, not an interpreter, not another spelling: {command!r}"
            )

    def test_every_moon_task_disables_moons_own_shell(self) -> None:
        """Every command-bearing task declares `options.shell: false`.

        moon's `shell` defaults to true on Windows and false elsewhere; on Windows
        PowerShell parses the composed command line and can reject it (a statement
        opening with a quoted path is parsed in expression mode). `shell: false`
        removes the parser and keeps one spelling for every platform.

        Does not reach whether a shell-less spawn resolves a bare name from `PATH`
        on Windows; that is unmeasured here."""
        for name, task in tasks().items():
            if "command" not in task:  # the `build` aggregate runs nothing itself
                continue
            assert (task.get("options") or {}).get("shell") is False, (
                f"task '{name}' must declare `options.shell: false` — on Windows moon's default "
                f"shell is PowerShell, and moon's composed command line is what this option removes "
                f"from its reach"
            )


# ---------------------------------------------------------------------------
# MCP configuration
# ---------------------------------------------------------------------------


class TestMcpConfiguration:
    """The scaffolded project carries exactly three server lines, one per client that reads one.

    The SWCraft extension registers the server for Copilot itself, so ``.vscode/``
    carries no server file. ``.mcp.json`` (Claude Code), ``.codex/config.toml``
    (Codex) and ``.kiro/settings/mcp.json`` (Kiro) name the bare ``swcraft`` from
    ``PATH`` and the manifest relative to the project, so nothing in them is
    per-machine; the clients start the server in the session's folder.

    Does not cover the clients' own reading of the files, their trust and approval
    prompts (no test runs a client), and another MCP host, configured by hand.
    """

    #: The three files the template ships for clients that read a server line.
    SERVER_FILES = (".codex/config.toml", ".kiro/settings/mcp.json", ".mcp.json")

    def test_the_template_ships_exactly_the_three_client_server_files(self) -> None:
        named = sorted(
            relative
            for relative in template_files()
            if "mcp" in Path(relative).name.lower() or relative.endswith("config.toml")
        )
        assert named == sorted(self.SERVER_FILES)

    def test_no_server_file_sits_in_the_editor_folder(self) -> None:
        assert set(name for name in template_files() if name.startswith(".vscode/")) == {".vscode/settings.json"}

    def test_the_readme_names_both_files_and_no_editor_server_file(self) -> None:
        prose = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")
        for relative in self.SERVER_FILES:
            assert f"`{relative}`" in prose, f"the README does not name {relative}"
        lowered = " ".join(prose.lower().split())
        assert ".vscode/mcp.json" not in lowered
        assert "repair mcp config" not in lowered


# ---------------------------------------------------------------------------
# The client files: Codex and Claude Code
# ---------------------------------------------------------------------------


def _claude_expand(text: str, environment: dict[str, str]) -> str:
    """*text* with ``${VAR}`` and ``${VAR:-default}`` expanded by Claude Code's documented rule.

    ``${VAR}`` is the variable's value; ``${VAR:-default}`` is the value when the variable is
    set and not empty, else the default. An unset ``${VAR}`` with no default is an error in
    Claude Code, and one here.
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(3)
        value = environment.get(name, "")
        if value:
            return value
        if default is None:
            raise KeyError(name)
        return default

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}", replace, text)


def _manifest_argument(args: list[str]) -> str:
    assert args[:2] == ["mcp-serve", "--project"] and len(args) == 3, args
    return args[2]


class TestClientFiles:
    """The Codex and Claude Code files, with their markers, read the way each client reads them.

    Does not reach: a client starting the server
    (``packages/mcp/tests/test_client_stanzas.py``), a client running the hook, whether a client
    honours ``enabledMcpjsonServers`` or ``default_tools_approval_mode``, and the onboarding assets'
    copies of the four files other than ``AGENTS.md`` (the extension's own sync test).
    """

    @staticmethod
    def _mcp_json() -> dict:
        return json.loads((TEMPLATE_ROOT / ".mcp.json").read_text(encoding="utf-8"))

    @staticmethod
    def _codex_config() -> dict:
        return tomllib.loads((TEMPLATE_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))

    @staticmethod
    def _claude_settings() -> dict:
        return json.loads((TEMPLATE_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_the_claude_code_server_line_is_exact(self) -> None:
        assert self._mcp_json() == {
            "mcpServers": {
                "swcraft": {
                    "command": "swcraft",
                    "args": ["mcp-serve", "--project", "${CLAUDE_PROJECT_DIR:-.}/swcraft.yaml"],
                    "env": {"SWCRAFT_REVIEW": "panel"},
                }
            }
        }

    def test_the_codex_server_line_is_exact_under_its_marker(self) -> None:
        text = (TEMPLATE_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert text.splitlines()[0] == TOML_MARKER
        assert self._codex_config() == {
            "project_doc_max_bytes": 131072,
            "mcp_servers": {
                "swcraft": {
                    "command": "swcraft",
                    "args": ["mcp-serve", "--project", "swcraft.yaml"],
                    "env": {"SWCRAFT_REVIEW": "panel"},
                    "tool_timeout_sec": 86400,
                    "default_tools_approval_mode": "approve",
                }
            },
        }

    def test_the_claude_code_settings_are_exactly_three_keys(self) -> None:
        settings = self._claude_settings()
        assert settings == {
            "env": {"CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0"},
            "enabledMcpjsonServers": ["swcraft"],
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^(Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|Bash|Monitor|PowerShell)$",
                        "hooks": [{"type": "command", "command": "swcraft guard --host claude", "timeout": 10}],
                    }
                ]
            },
        }

    def test_the_settings_approve_the_server_the_server_file_names(self) -> None:
        assert self._claude_settings()["enabledMcpjsonServers"] == list(self._mcp_json()["mcpServers"])

    def test_the_claude_hook_matcher_names_every_tool_the_claude_guard_judges(self) -> None:
        """Every tool the guard judges for Claude Code is matched; a SWCraft call and the subagent tool are not.

        ``Read``, ``Grep`` and ``Glob`` are named here: the guard judges them by the Copilot rules,
        whose tables also hold other hosts' names.
        """
        matcher = re.compile(self._claude_settings()["hooks"]["PreToolUse"][0]["matcher"])
        judged = {"Read", "Grep", "Glob"} | CLAUDE_WRITE_TOOLS | CLAUDE_SHELL_TOOLS | CLAUDE_DENIED_TOOLS
        assert sorted(tool for tool in judged if not matcher.fullmatch(tool)) == []
        for other in ("mcp__swcraft__read", "Agent", "WebFetch", "ReadX", "xRead"):
            assert matcher.search(other) is None, other

    def test_the_claude_hook_command_is_this_swcraft_s_guard_with_its_host_option(self) -> None:
        command = self._claude_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"].split()
        assert command[:2] == ["swcraft", "guard"]
        guard = build_parser().parse_args(command[1:])
        assert guard.host == "claude"

    def test_the_two_server_lines_agree_in_command_and_environment(self) -> None:
        claude = self._mcp_json()["mcpServers"]["swcraft"]
        codex = self._codex_config()["mcp_servers"]["swcraft"]
        assert claude["command"] == codex["command"] == "swcraft"
        assert claude["env"] == codex["env"] == {"SWCRAFT_REVIEW": "panel"}
        assert claude["args"][:2] == codex["args"][:2] == ["mcp-serve", "--project"]

    @pytest.mark.parametrize("project_dir_set", [True, False], ids=["CLAUDE_PROJECT_DIR set", "unset"])
    def test_the_two_server_lines_resolve_to_the_same_manifest(self, project_dir_set: bool, tmp_path: Path) -> None:
        """Compared by the manifest each line resolves to, never by the arguments' text.

        Claude Code's line is expanded by its documented rule, ``CLAUDE_PROJECT_DIR`` set and unset,
        and both lines resolve against the project folder, the server's working directory.
        """
        project = tmp_path / "project"
        project.mkdir()
        environment = {"CLAUDE_PROJECT_DIR": str(project)} if project_dir_set else {}
        claude = _claude_expand(_manifest_argument(self._mcp_json()["mcpServers"]["swcraft"]["args"]), environment)
        codex = _manifest_argument(self._codex_config()["mcp_servers"]["swcraft"]["args"])
        resolved = {(project / claude).resolve().as_posix(), (project / codex).resolve().as_posix()}
        assert resolved == {(project / "swcraft.yaml").resolve().as_posix()}

    def test_the_expansion_rule_reads_an_empty_variable_as_unset(self) -> None:
        line = "${CLAUDE_PROJECT_DIR:-.}/swcraft.yaml"
        assert _claude_expand(line, {"CLAUDE_PROJECT_DIR": ""}) == "./swcraft.yaml"
        assert _claude_expand(line, {"CLAUDE_PROJECT_DIR": "/p"}) == "/p/swcraft.yaml"
        with pytest.raises(KeyError):
            _claude_expand("${UNSET_VARIABLE}", {})

    def test_claude_md_is_the_marker_and_the_agents_md_import(self) -> None:
        assert (TEMPLATE_ROOT / "CLAUDE.md").read_text(encoding="utf-8") == f"{MARKDOWN_MARKER}\n\n@AGENTS.md\n"

    def test_agents_md_is_the_marker_the_preamble_and_the_profile_body(self) -> None:
        text = (TEMPLATE_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        profile = (TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8")
        assert text.splitlines()[0] == MARKDOWN_MARKER
        assert HOST_PREAMBLE in text
        assert text.endswith(profile_body(profile))
        assert "tools: [" not in text.split("# SWCraft Architect", 1)[0], "the frontmatter was not dropped"

    def test_codex_reads_all_of_agents_md_with_headroom(self) -> None:
        """``project_doc_max_bytes`` leaves the default 32 KiB beyond the rendered ``AGENTS.md``.

        Codex reads ``AGENTS.md`` only up to that key (32 KiB by default) and the rendered file is
        larger; the headroom covers growth and any other ``AGENTS.md`` read into the same budget.
        Does not reach whether a later Codex still honours the key at the project level, or a
        project whose own config changes it.
        """
        default_budget = 32 * 1024
        limit = self._codex_config()["project_doc_max_bytes"]
        profile = (REPO_ROOT / dev_module().AGENTS_MD_PROFILE).read_text(encoding="utf-8")
        size = len(render_agents_md(profile).encode("utf-8"))
        assert size + default_budget <= limit, (
            f"the rendered AGENTS.md is {size} bytes; with the {default_budget}-byte headroom it exceeds "
            f"project_doc_max_bytes = {limit} in the template's .codex/config.toml, so Codex would cut it -- "
            "raise the key in the template's .codex/config.toml and in "
            "vscode_extension/fixtures/agents/swcraft.codex-config.toml (byte-equal), or shorten the profile"
        )

    @pytest.mark.parametrize("relative", dev_module().AGENTS_MD_FILES)
    def test_every_agents_md_copy_is_a_fresh_rendering(self, relative: str) -> None:
        """Both copies -- the template's and the onboarding assets' -- equal a fresh rendering."""
        profile = (REPO_ROOT / dev_module().AGENTS_MD_PROFILE).read_text(encoding="utf-8")
        committed = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert committed == render_agents_md(profile), (
            f"{relative} is stale against the template profile -- run `python scripts/dev.py schemas` "
            "or `python scripts/dev.py docs-reference` (both render it) and commit the result"
        )

    def test_the_rendering_reads_the_template_profile_into_both_homes(self) -> None:
        dev = dev_module()
        assert dev.AGENTS_MD_PROFILE == dev.AGENT_PROFILES[0]
        assert (REPO_ROOT / dev.AGENTS_MD_PROFILE).resolve() == (
            TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md"
        ).resolve()
        assert (REPO_ROOT / dev.AGENTS_MD_FILES[0]).resolve() == (TEMPLATE_ROOT / "AGENTS.md").resolve()
        assert dev.AGENTS_MD_FILES[1] == "vscode_extension/fixtures/agents/AGENTS.md"

    def test_the_drift_rule_fires_on_a_stale_copy(self) -> None:
        profile = (TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8")
        edited = profile.replace("\n# SWCraft Architect\n", "\n# SWCraft Architect, edited\n", 1)
        assert edited != profile
        committed = (TEMPLATE_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        assert render_agents_md(edited) != committed, "an edit of the profile's body leaves AGENTS.md fresh"

    def test_the_module_form_writes_every_output(self, tmp_path: Path) -> None:
        from swcraft_tools import agents_md

        profile = tmp_path / "profile.agent.md"
        profile.write_text("---\nname: X\ntools: ['a']\n---\n\n# Body\n\ntext\n", encoding="utf-8", newline="\n")
        outputs = [tmp_path / "a" / "AGENTS.md", tmp_path / "b.md"]
        outputs[0].parent.mkdir()
        outputs[1].write_text("old\n", encoding="utf-8", newline="\n")

        assert agents_md.main(["--profile", str(profile), *map(str, outputs)]) == 0

        for path in outputs:
            assert path.read_bytes() == f"{MARKDOWN_MARKER}\n\n{HOST_PREAMBLE}\n# Body\n\ntext\n".encode()

    def test_the_renderer_imports_the_standard_library_alone(self) -> None:
        """What its inventory row and the generic-core exemption rest on."""
        import ast
        import sys

        from swcraft_tools import agents_md

        tree = ast.parse(Path(agents_md.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert imported, "no import read; the check would assert nothing"
        assert sorted(name for name in imported if name not in sys.stdlib_module_names) == []

    def test_a_profile_whose_frontmatter_never_closes_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never closes"):
            profile_body("---\nname: X\n# Body\n")

    def test_a_profile_without_frontmatter_is_taken_whole(self) -> None:
        assert profile_body("# Body\n") == "# Body\n"

    def test_the_three_comment_capable_files_open_with_the_one_marker_sentence(self) -> None:
        first_lines = {
            relative: (TEMPLATE_ROOT / relative).read_text(encoding="utf-8").splitlines()[0]
            for relative in ("AGENTS.md", "CLAUDE.md", ".codex/config.toml")
        }
        assert first_lines == {
            "AGENTS.md": MARKDOWN_MARKER,
            "CLAUDE.md": MARKDOWN_MARKER,
            ".codex/config.toml": TOML_MARKER,
        }
        assert MARKDOWN_MARKER == (
            "<!-- Written by SWCraft's Onboard Project. Edit freely: once this line is gone, "
            "Onboard Project keeps the file. -->"
        )


class TestKiroFiles:
    """Kiro's three files.

    No ``timeout`` key in the server file (Kiro's MCP configuration documents none), no front matter in
    the steering file (none means always included), the hook's ``timeout`` 10 seconds (the documented
    unit) and its matcher ``*``. Does not reach Kiro reading the files, whether Kiro starts the server
    in the project folder, and the onboarding assets' copies of the two files other than the steering
    file (the extension's own sync test).
    """

    @staticmethod
    def _json(relative: str) -> dict:
        return json.loads((TEMPLATE_ROOT / relative).read_text(encoding="utf-8"))

    def test_the_kiro_server_line_is_exact(self) -> None:
        assert self._json(".kiro/settings/mcp.json") == {
            "mcpServers": {
                "swcraft": {
                    "command": "swcraft",
                    "args": ["mcp-serve", "--project", "swcraft.yaml"],
                    "env": {"SWCRAFT_REVIEW": "panel"},
                }
            }
        }

    def test_the_kiro_server_line_agrees_with_codex_s(self) -> None:
        kiro = self._json(".kiro/settings/mcp.json")["mcpServers"]["swcraft"]
        codex = tomllib.loads((TEMPLATE_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))
        codex_server = codex["mcp_servers"]["swcraft"]
        assert (kiro["command"], kiro["args"], kiro["env"]) == (
            codex_server["command"],
            codex_server["args"],
            codex_server["env"],
        )

    def test_the_kiro_hook_is_exact(self) -> None:
        assert self._json(".kiro/hooks/swcraft-guard.json") == {
            "version": "v1",
            "hooks": [
                {
                    "name": "SWCraft model guard",
                    "trigger": "PreToolUse",
                    "matcher": "*",
                    "action": {"type": "command", "command": "swcraft guard --host kiro"},
                    "timeout": 10,
                }
            ],
        }

    def test_the_kiro_hook_command_is_this_swcraft_s_guard_with_its_host_option(self) -> None:
        command = self._json(".kiro/hooks/swcraft-guard.json")["hooks"][0]["action"]["command"].split()
        assert command[:2] == ["swcraft", "guard"]
        assert build_parser().parse_args(command[1:]).host == "kiro"

    def test_the_steering_file_is_the_marker_the_kiro_preamble_and_the_profile_body(self) -> None:
        text = (TEMPLATE_ROOT / ".kiro" / "steering" / "swcraft.md").read_text(encoding="utf-8")
        profile = (TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8")
        assert text.splitlines()[0] == MARKDOWN_MARKER, "no front matter: the marker is the first line"
        assert KIRO_PREAMBLE in text
        assert HOST_PREAMBLE not in text
        assert text.endswith(profile_body(profile))
        assert "tool_load" in KIRO_PREAMBLE

    def test_the_kiro_preamble_sends_a_swcraft_question_to_guide(self) -> None:
        """The Kiro preamble sends a question about SWCraft itself to the `guide` tool before Kiro's own tools."""
        (sentence,) = [part for part in re.split(r"(?<=\.)\s", KIRO_PREAMBLE) if "`guide`" in part]
        assert {"`guide`", "before", "documentation"} <= set(sentence.split())

    @pytest.mark.parametrize("relative", dev_module().KIRO_STEERING_FILES)
    def test_every_steering_copy_is_a_fresh_rendering(self, relative: str) -> None:
        """Both copies -- the template's and the onboarding assets' -- equal a fresh rendering."""
        profile = (REPO_ROOT / dev_module().AGENTS_MD_PROFILE).read_text(encoding="utf-8")
        committed = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert committed == render_kiro_steering(profile), (
            f"{relative} is stale against the template profile -- run `python scripts/dev.py schemas` "
            "or `python scripts/dev.py docs-reference` (both render it) and commit the result"
        )

    def test_the_rendering_writes_the_template_s_steering_file(self) -> None:
        dev = dev_module()
        assert (REPO_ROOT / dev.KIRO_STEERING_FILES[0]).resolve() == (
            TEMPLATE_ROOT / ".kiro" / "steering" / "swcraft.md"
        ).resolve()
        assert dev.KIRO_STEERING_FILES[1] == "vscode_extension/fixtures/agents/swcraft.kiro-steering.md"

    def test_the_module_form_writes_the_steering_file_beside_agents_md(self, tmp_path: Path) -> None:
        from swcraft_tools import agents_md

        profile = tmp_path / "profile.agent.md"
        profile.write_text("---\nname: X\n---\n\n# Body\n", encoding="utf-8", newline="\n")
        agents = tmp_path / "AGENTS.md"
        steering = tmp_path / "new" / ".kiro" / "steering" / "swcraft.md"

        assert agents_md.main(["--profile", str(profile), "--kiro-steering", str(steering), str(agents)]) == 0

        assert agents.read_bytes() == f"{MARKDOWN_MARKER}\n\n{HOST_PREAMBLE}\n# Body\n".encode()
        assert steering.read_bytes() == f"{MARKDOWN_MARKER}\n\n{KIRO_PREAMBLE}\n# Body\n".encode()


# ---------------------------------------------------------------------------
# Independence from a SWCraft checkout and from an artifact location
# ---------------------------------------------------------------------------


#: Every subcommand ``swcraft`` declares, read off the parser so it cannot fall
#: behind the CLI (a hand-written list once left the guard below blind).
_SWCRAFT_SUBCOMMANDS = tuple(sorted(parser_subcommand_options(build_parser())))
_BARE_INVOCATION = re.compile(
    r"(?<![\w./\\-])swcraft\s+(?:" + "|".join(re.escape(name) for name in _SWCRAFT_SUBCOMMANDS) + r")\b"
)

#: The other spelling of an invocation: `moon.yml` puts the program in
#: ``command:`` and every argument in ``args:``, so :data:`_BARE_INVOCATION`
#: cannot see the six tasks that run SWCraft.
_MOON_COMMAND = re.compile(r"""^\s*command:\s*['"]?(\S*?swcraft)['"]?\s*$""")


#: The rule every client's instructions carry: authored once in the three profiles' shared Fallback
#: Rule region, rendered into both ``AGENTS.md`` and both steering files.
NO_GUARD_BYPASS_RULE = (
    "Never propose disabling, editing or bypassing the model guard or its hook files. When a "
    "direct change is refused, use the SWCraft tools, or tell the user what the tools cannot do."
)


@pytest.mark.parametrize(
    "relative",
    [*dev_module().AGENT_PROFILES, *dev_module().AGENTS_MD_FILES, *dev_module().KIRO_STEERING_FILES],
)
def test_every_client_instruction_file_forbids_proposing_a_guard_bypass(relative: str) -> None:
    """Every copy forbids proposing a guard bypass (an agent refused a direct edit proposed disabling it).

    Does not reach whether a host's model follows the sentence.
    """
    text = " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())
    assert NO_GUARD_BYPASS_RULE in text, f"{relative} lacks the no-guard-bypass rule"


def _is_comment(relative: str, line: str) -> bool:
    """True for a `#` comment line — Markdown excluded, where `#` is a heading."""
    return not relative.endswith(".md") and line.lstrip().startswith("#")


class TestIndependenceFromTheSwcraftCheckout:
    """No template file may tie the project to where SWCraft or its wheels live."""

    def test_every_swcraft_invocation_is_the_bare_command(self) -> None:
        """Every `swcraft` invocation in the template is the bare command, never a path.

        The workstation archive owns one installation per machine and its `PATH`
        entry, so a prefixed command is the defect. Two spellings are scanned: a
        shell/prose line (`swcraft generate arxml`) and `moon.yml`'s `command:` key.
        The floor at the end makes going blind a failure rather than a pass.

        Does not reach: comment lines outside Markdown (`moon.yml` quotes a
        rejected command line); a path to `swcraft` in an `args:` list; an
        invocation assembled at run time from a variable.
        """
        matched: list[str] = []
        for relative, path in template_files().items():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _is_comment(relative, line):
                    continue
                for match in _BARE_INVOCATION.finditer(line):
                    matched.append(f"{relative}:{number}")
                    prefix = line[: match.start()]
                    assert not prefix.rstrip().endswith(("/", "\\")), (
                        f"{relative}:{number} resolves swcraft through a path rather than from PATH: {line.strip()!r}"
                    )
                moon = _MOON_COMMAND.match(line)
                if moon is not None:
                    matched.append(f"{relative}:{number}")
                    assert moon.group(1) == "swcraft", (
                        f"{relative}:{number} runs swcraft through a path rather than from PATH: {line.strip()!r}"
                    )
        # Every assertion above is about a prefix, so a pattern that matched nothing
        # would pass without reading a command line.
        assert len(matched) >= 7, (
            f"this guard has gone blind: {_BARE_INVOCATION.pattern} matched {matched or 'nothing'} "
            "across the template, so it is asserting a prefix rule about no command line"
        )

    def test_no_template_file_puts_a_toolchain_back_inside_the_project(self) -> None:
        """No template line writes a spelling of :data:`FORBIDDEN_PROJECT_TOOLCHAIN`.

        Comment lines are exempt: `moon.yml` quotes the command line PowerShell
        rejected and `.prototools` says which pin left and why, so the next author
        does not reintroduce them. Markdown has no comment syntax here, so every
        line of it is read.

        Does not reach the repository's own documentation, which describes
        SWCraft's developer `.venv`; this reads the shipped template tree only.
        """
        for relative, path in template_files().items():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _is_comment(relative, line):
                    continue
                for forbidden in FORBIDDEN_PROJECT_TOOLCHAIN:
                    assert forbidden not in line, (
                        f"{relative}:{number} puts a project-local toolchain back: {forbidden!r} — "
                        "the workstation archive owns the toolchain and the project owns the pin"
                    )

    def test_no_template_file_references_a_swcraft_checkout(self) -> None:
        for relative, path in template_files().items():
            text = path.read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_CHECKOUT_PATHS:
                assert forbidden not in text, f"{relative} references a SWCraft checkout path: {forbidden!r}"
            assert str(REPO_ROOT) not in text, f"{relative} contains this checkout's absolute path"

    def test_no_template_file_hardcodes_the_local_artifact_directory(self) -> None:
        """The wheels' location is the installer's argument, never committed state."""
        for relative, path in template_files().items():
            text = path.read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_ARTIFACT_PATHS:
                assert forbidden not in text, f"{relative} hardcodes an artifact directory: {forbidden!r}"


# ---------------------------------------------------------------------------
# Agent instructions
# ---------------------------------------------------------------------------


class TestAgentInstructions:
    """Template-authored, tied to the example's copy only where it matters."""

    @pytest.mark.parametrize("statement", SHARED_AGENT_STATEMENTS)
    def test_the_boundary_statement_reads_the_same_in_both_copies(self, statement: str) -> None:
        """Each boundary statement is in the template's and the example's Copilot instructions.

        The copies are byte-equal (`test_example_is_init_output.py`); this names the
        statement that went missing rather than a byte offset.
        """
        template = normalized((TEMPLATE_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8"))
        repository = normalized(
            (REPO_ROOT / "examples" / "basic" / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        )
        assert normalized(statement) in repository, "the repository's own Copilot instructions lost this statement"
        assert normalized(statement) in template, (
            "the template's Copilot instructions must carry the same boundary statement as the repository's"
        )

    @pytest.mark.parametrize("statement", SHARED_PROFILE_STATEMENTS)
    def test_the_declined_weakening_rules_read_the_same_in_both_profiles(self, statement: str) -> None:
        """The declined-weakening rules and the weakening vocabulary read the same in both profiles.

        These sentences are the whole enforcement; no mechanism stands behind them.
        This guard is the only one that reads the template's copy
        (`packages/mcp/tests/test_vscode_agent_profile.py` pins the `.github/` copy).
        """
        _assert_in_both_architect_profiles(statement)

    @pytest.mark.parametrize("statement", BOUNDED_READ_STATEMENTS)
    def test_the_bounded_read_guidance_reads_the_same_in_both_profiles(self, statement: str) -> None:
        """The bounded-read guidance (forms, default table, `over-budget` chain) is in both profiles.

        A host that loads a `.github/agents/` profile loads no system prompt.

        Does not reach the profiles against each other, or whether the server
        enforces the sentences: `packages/tools/tests/test_editing_budget.py` and
        `packages/mcp/tests/test_response_budget.py` are that oracle.
        """
        _assert_in_both_architect_profiles(statement)

    @pytest.mark.parametrize("statement", MODEL_QUERY_STATEMENTS)
    def test_the_model_query_guidance_reads_the_same_in_both_profiles(self, statement: str) -> None:
        """The model-query guidance (request keys, paged chain step) is in both profiles.

        Does not reach the profiles against each other, a third home (the standalone
        agent's prompt is `packages/agent/tests/test_acceptance.py`'s subject), or the
        `impact_of` parameters, which live in the tool's own description.
        """
        _assert_in_both_architect_profiles(statement)

    @pytest.mark.parametrize("rule", MODEL_QUESTION_RULES)
    def test_the_copilot_instructions_route_model_questions_through_the_tools(self, rule: str) -> None:
        """The template's instructions and the extension's bundled copy state each model-question rule.

        The bundled copy is what Onboard Project writes into a project `swcraft init`
        did not create; reading it here names the missing rule rather than a byte offset.
        """
        for path in (
            TEMPLATE_ROOT / ".github" / "copilot-instructions.md",
            REPO_ROOT / "vscode_extension" / "fixtures" / "agents" / "copilot-instructions.md",
        ):
            text = normalized(path.read_text(encoding="utf-8"))
            assert normalized(rule) in text, f"{path.relative_to(REPO_ROOT).as_posix()} lost: {rule!r}"

    @pytest.mark.parametrize("whose", sorted(ARCHITECT_TOOL_GRANTS))
    def test_the_custom_agent_grants_the_tools_its_root_declares(self, whose: str) -> None:
        """The custom agent's frontmatter grants exactly :data:`ARCHITECT_TOOL_GRANTS`, in either root.

        The frontmatter is the boundary; an exact list rather than a denylist, so any
        other grant fails, named or not.
        """
        root = {"template": TEMPLATE_ROOT, "example": REPO_ROOT / "examples" / "basic"}[whose]
        text = (root / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---")[1])
        assert frontmatter["tools"] == ARCHITECT_TOOL_GRANTS[whose], (
            f"{whose}'s profile grants {frontmatter['tools']!r}, not {ARCHITECT_TOOL_GRANTS[whose]!r}"
        )
        assert frontmatter["name"] == "SWCraft Architect"

    def test_the_walkthrough_quotes_the_template_profiles_grant(self) -> None:
        """The walkthrough's agent page quotes the template's grant, `view` included, never `swcraft/*` alone.

        Does not reach a page that describes the grant in other words than the literal.
        """
        text = (REPO_ROOT / "vscode_extension" / "media" / "walkthrough" / "06-agent.md").read_text(encoding="utf-8")
        grant = ", ".join(f"'{tool}'" for tool in ARCHITECT_TOOL_GRANTS["template"])
        assert f"`tools: [{grant}]`" in text
        assert "`tools: ['swcraft/*']`" not in text

    def test_the_hook_file_runs_the_guard_before_each_tool_call(self) -> None:
        """`.github/hooks/swcraft.json` runs the bare `swcraft guard` on `PreToolUse` and nothing else.

        Does not reach whether the host runs it; nothing here can run VS Code's agent host.
        """
        hooks = json.loads((TEMPLATE_ROOT / ".github" / "hooks" / "swcraft.json").read_text(encoding="utf-8"))
        assert set(hooks) == {"hooks"}
        assert set(hooks["hooks"]) == {"PreToolUse"}, "the guard answers PreToolUse and no other event"
        (entry,) = hooks["hooks"]["PreToolUse"]
        assert entry["type"] == "command"
        assert entry["command"] == "swcraft guard"
        assert isinstance(entry.get("timeout"), int) and entry["timeout"] > 0

    def test_the_guard_the_hook_runs_is_a_subcommand_of_this_swcraft(self) -> None:
        """The hook names a subcommand the parser declares."""
        assert "guard" in _SWCRAFT_SUBCOMMANDS

    def test_the_codex_hook_file_runs_the_codex_guard_before_a_patch_or_a_shell_command(self) -> None:
        """`.codex/hooks.json` runs `swcraft guard --host codex` for exactly the tools the Codex dialect judges.

        The file lives at ``CODEX_HOOK_FILE``, where the guard looks for it, and the
        anchored matcher keeps every other tool call (an MCP tool included) from
        starting a process.

        Does not reach whether Codex runs the hook, trusts the project's `.codex/`
        layer, or sends these tool names; nothing here can run Codex's host.
        """
        from swcraft_tools.copilot_guard import CODEX_HOOK_FILE, CODEX_PATCH_TOOLS, CODEX_SHELL_TOOLS

        assert CODEX_HOOK_FILE.as_posix() in EXPECTED_FILES
        hooks = json.loads((TEMPLATE_ROOT / CODEX_HOOK_FILE).read_text(encoding="utf-8"))
        assert set(hooks) == {"hooks"}
        assert set(hooks["hooks"]) == {"PreToolUse"}, "the guard answers PreToolUse and no other event"
        (group,) = hooks["hooks"]["PreToolUse"]
        assert set(group) == {"matcher", "hooks"}
        assert group["matcher"] == "^(Bash|apply_patch|Edit|Write)$"
        matcher = re.compile(group["matcher"])
        for tool in CODEX_PATCH_TOOLS | CODEX_SHELL_TOOLS:
            assert matcher.search(tool), f"the dialect judges {tool!r}, so the hook must match it"
        for tool in ("Read", "view", "WebSearch", "mcp__swcraft__change_set", "xBash", "Bashful", "apply_patch2"):
            assert not matcher.search(tool), f"{tool!r} is not judged by the dialect"
        (handler,) = group["hooks"]
        assert set(handler) == {"type", "command", "timeout", "statusMessage"}
        assert handler["type"] == "command"
        assert handler["command"] == "swcraft guard --host codex"
        assert handler["timeout"] == 10, "Copilot's timeout, against Codex's 600 s default"
        assert handler["statusMessage"].strip()

    def test_the_codex_hook_command_is_this_swcraft_s_guard_with_its_host_option(self) -> None:
        """The Codex hook's words after `swcraft` parse to `guard` with host `codex`."""
        from swcraft_tools.copilot_guard import CODEX_HOOK_FILE

        hooks = json.loads((TEMPLATE_ROOT / CODEX_HOOK_FILE).read_text(encoding="utf-8"))
        (group,) = hooks["hooks"]["PreToolUse"]
        (handler,) = group["hooks"]
        program, *argv = handler["command"].split()
        assert program == "swcraft"
        args = build_parser().parse_args(argv)
        assert args.command == "guard"
        assert args.host == "codex"

    def test_the_saved_answer_section_is_the_same_in_the_template_and_the_bundle(self) -> None:
        """The saved-answer section is byte-equal in the template's and the bundled profile.

        The negative half: the standalone assistant's system prompt, whose host grants no
        `view`, does not carry it.
        """
        template = _saved_answer_section(TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md")
        bundled = _saved_answer_section(
            REPO_ROOT / "vscode_extension" / "fixtures" / "agents" / "swcraft-architect.agent.md"
        )
        assert template is not None, f"the template's profile lost {SAVED_ANSWER_HEADING!r}"
        assert bundled == template, "the bundled profile's saved-answer section differs from the template's"
        assert "`view`" in template and "`view_range`" in template
        without = REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent" / "agent-system-prompt.md"
        assert _saved_answer_section(without) is None, (
            f"{without.relative_to(REPO_ROOT).as_posix()} teaches `view`, which its host does not grant"
        )

    @pytest.mark.parametrize("relative", dev_module().AGENT_PROFILES)
    def test_each_profile_stops_a_request_refused_twice_and_states_the_rule_s_limit(self, relative: str) -> None:
        """Each of `dev.py`'s `AGENT_PROFILES` forbids a third refused request and states the rule's limit.

        The rule does not reach a script the agent writes that loops over the tools,
        so the limit is pinned beside the rule.
        """
        text = normalized((REPO_ROOT / relative).read_text(encoding="utf-8"))
        rules = text.split("## Tool Usage Rules", 1)[1].split("## Recipes for common questions", 1)[0]
        assert (
            "10. **A request refused twice is not sent a third time.** When the server refuses the same "
            "request — the same tool with the same arguments, or a request it refuses with the same code "
            "for the same question — twice, do not send it again: report the refusal, its code and what "
            "you asked to the user, and ask how to go on."
        ) in rules, relative
        assert (
            "it does not reach a script you write that calls the tools in a loop — a script must carry "
            "its own stop on a refusal."
        ) in rules, relative

    def test_the_template_agent_instructions_describe_this_project_not_swcraft(self) -> None:
        """The scaffolded agent files name no path that exists only in the SWCraft repository."""
        for relative in AGENT_INSTRUCTION_FILES:
            text = (TEMPLATE_ROOT / relative).read_text(encoding="utf-8")
            for stray in ("examples/basic", "docs/adr/", "mcp.windows.json", "packages/mcp/src"):
                assert stray not in text, f"{relative} points at {stray!r}, which a scaffolded project does not have"

    @pytest.mark.parametrize("relative", AGENT_INSTRUCTION_FILES)
    def test_the_template_agent_instructions_name_no_removed_tool(self, relative: str) -> None:
        """Neither template document names a tool of the decision table's ``replaced`` set.

        Matching is on identifier boundaries, so the generic ``read`` does not make
        ``read_swc`` look present.
        """
        removed = json.loads(TOOL_PROJECTIONS.read_text(encoding="utf-8"))["projections"]["replaced"]
        assert len(removed) == 29, "the decision table stopped listing the removals this guard needs"
        text = (TEMPLATE_ROOT / relative).read_text(encoding="utf-8")
        named = [tool for tool in removed if re.search(rf"(?<!\w){re.escape(tool)}(?!\w)", text)]
        assert not named, f"{relative} still names removed tools: {named}"

    @pytest.mark.parametrize("tool", PROTOCOL_TOOLS)
    def test_the_template_profile_names_each_protocol_tool(self, tool: str) -> None:
        text = (TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md").read_text(encoding="utf-8")
        assert re.search(rf"`{re.escape(tool)}`", text), (
            f"the scaffolded custom agent profile never mentions `{tool}`, one of the four "
            "tools that are now the whole authoring path"
        )

    def test_no_template_document_names_a_command_the_extension_does_not_have(self) -> None:
        """Every `SWCraft: ...` command a template document names is one `package.json` contributes.

        Guards against a document naming a deleted command with every gate green.
        Does not reach what the documents say about the editor beyond command titles,
        or the guide's pages (`test_guide_surface.py`).
        """
        manifest = json.loads((REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        contributed = {entry["title"] for entry in manifest["contributes"]["commands"]}
        pattern = re.compile(r"SWCraft: (?:[A-Z][A-Za-z0-9]*)(?: [A-Z][A-Za-z0-9]*)*")

        named: set[str] = set()
        for relative in (*AGENT_INSTRUCTION_FILES, "README.md"):
            found = set(pattern.findall((TEMPLATE_ROOT / relative).read_text(encoding="utf-8")))
            unknown = sorted(found - contributed)
            assert unknown == [], f"the template's {relative} names a command the extension lacks: {unknown}"
            named |= found

        assert named, "no template document names a SWCraft command; the extractor has gone blind"

    @pytest.mark.parametrize("relative", AGENT_INSTRUCTION_FILES)
    def test_the_template_agent_instructions_link_a_component_only_when_a_requirement_is_named(
        self, relative: str
    ) -> None:
        """A requirement link the user names is the `satisfies` entry of the SWC's `create` payload.

        No text obliges the assistant to link every new SWC or to ask for a
        requirement the request does not name.
        """
        text = normalized((TEMPLATE_ROOT / relative).read_text(encoding="utf-8"))
        assert "when the user names" in text, f"{relative} must link a component only when the user names one"
        assert '"satisfies": ["REQ_001@1"]' in text, f"{relative} must keep the link's payload form"
        for obligation in ("ask whether one exists", "should be created", "must** be linked"):
            assert obligation not in text, f"{relative} still states the dropped obligation {obligation!r}"

    def test_the_template_copilot_instructions_document_the_review_mode(self) -> None:
        """The Copilot instructions document `SWCRAFT_REVIEW=panel`, its tab and its no-file rule.

        They must say that the tab needs VS Code open on the project and that the
        answer is never a file the assistant writes, or a hand-configured second
        host silently drops the human out of the loop."""
        text = normalized((TEMPLATE_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8"))
        assert "`SWCRAFT_REVIEW=panel`" in text
        assert "`SWCRAFT_REVIEW=elicit`" not in text
        assert "stage-only" in text
        assert "The review tab needs VS Code open on this project with the SWCraft extension." in text
        assert "waits until the user answers it there" in text
        assert "nobody can answer, so say where the question is and that the change set waits." in text
        found = re.findall(r"\b\d+\s*min|\bminutes?\b|\bdeadline\b|\bhours?\b|\babandoned after\b", text, re.I)
        assert not found, f"the template's instructions name a time limit: {found}"
        assert "never write, copy or delete a file under the SWCraft home's `reviews` folder" in text


# ---------------------------------------------------------------------------
# No pipeline definition
# ---------------------------------------------------------------------------


class TestNoPipelineDefinition:
    """The template is a local build contract, never a pipeline."""

    def test_template_contains_no_pipeline_definition(self) -> None:
        for relative in template_files():
            assert not relative.startswith(".github/workflows/"), f"{relative} is a pipeline definition"
            assert "azure-pipelines" not in relative, f"{relative} is a pipeline definition"
            assert not relative.endswith((".gitlab-ci.yml", "Jenkinsfile")), f"{relative} is a pipeline definition"

    def test_readme_names_pipeline_wiring_as_downstream_work(self) -> None:
        readme = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")
        assert "## Downstream integration" in readme
        # Whitespace-normalized, so a line wrap cannot hide a claim.
        prose = " ".join(readme.lower().split())
        assert "no pipeline definition" in prose
        assert "downstream integration work" in prose
        assert "local build contract" in prose


# ---------------------------------------------------------------------------
# What the installer does to PATH, as the scaffolded project states it
# ---------------------------------------------------------------------------


class TestThePathClaim:
    """What the scaffolded files say the installer does to ``PATH``.

    It edits the user ``PATH`` on Windows; on macOS and Linux it prints one
    ``export PATH=`` line and edits no shell file.
    """

    def test_no_template_file_promises_that_the_installer_edits_path_everywhere(self) -> None:
        """No template file promises that the installer puts ``swcraft`` on ``PATH`` everywhere.

        Does not reach a new phrasing of the same claim (it pins the retired ones),
        and cannot tell a correct sentence from a merely different one; the positive
        half below keeps the README's own step honest.
        """
        retired = (
            "put on path by",
            "puts a single `swcraft` command on `path`",
            "installer put on",
            "the `path` entry it added",
        )
        for relative in template_files():
            path = TEMPLATE_ROOT / relative
            if path.suffix not in {".md", ".yml", ".yaml"}:
                continue
            prose = " ".join(path.read_text(encoding="utf-8").lower().split())
            for claim in retired:
                assert claim not in prose, (
                    f"{relative} tells a scaffolded project that the installer puts swcraft on PATH "
                    f"({claim!r}); outside Windows it prints a line and edits nothing"
                )

    def test_the_readme_prerequisite_step_names_both_platforms(self) -> None:
        """The README's prerequisite step names the ``export PATH=`` line and both platforms."""
        prose = " ".join((TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8").lower().split())
        assert "export path=" in prose, "the README must name the line the installer prints"
        assert "shell profile" in prose
        assert "on windows" in prose and "macos and linux" in prose


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


class TestTheFailedBuildRestatement:
    r"""The failed-build rule stated correctly in the README ``swcraft init`` writes.

    The rule is about the writer: nothing is written until a whole document is
    built and validated in memory, and every build's first step moves the previous
    ARXML aside as ``model_old.arxml`` and removes the rest. "A failing build leaves
    no outputs" is false: ``swcraft validate`` writes ``dist/model.arxml``, announces
    it and then checks it, so a failing XSD or structural check exits 1 with the file
    on disk, and ``generate-diagrams`` / ``generate-needs`` depend on
    ``check-model`` alone, so a failing ``moon run :build`` can leave their outputs.
    ``packages/tools/tests/test_cli.py`` pins the behaviour; this pins the prose.

    Does not reach any file but the scaffolded README, and cannot tell a correct
    sentence from a merely different one.
    """

    def test_the_scaffolded_readme_says_what_a_failure_does_leave(self) -> None:
        """The README says a failure is about the previous run's artifacts and `validate` writes before it checks."""
        prose = " ".join(
            (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8").lower().replace("`", "").replace("*", "").split()
        )
        assert "previous" in prose, "the guarantee is about the PREVIOUS run's artifacts"
        assert "then checks it" in prose or "then validates" in prose, (
            "the README must say that `validate` writes the ARXML before it checks it"
        )
        assert "read the exit code" in prose


class TestPins:
    """The toolchain versions the scaffolded project records."""

    def test_the_manifest_pins_the_toolchain_at_the_current_version(self) -> None:
        """The manifest's `toolchain_version` is this release: a declaration every `swcraft` command checks."""
        text = (TEMPLATE_ROOT / "swcraft.yaml").read_text(encoding="utf-8")
        manifest = yaml.safe_load(text.replace("{{PROJECT_NAME}}", "placeholder"))
        assert manifest["toolchain_version"] == __version__, (
            "the scaffolded manifest must pin the version of the toolchain that scaffolds it"
        )

    def test_prototools_pins_moon_and_not_python(self) -> None:
        """proto installs moon and no interpreter.

        The workstation installer chooses the interpreter, so a python pin here
        would name a version nothing reads.
        """
        text = (TEMPLATE_ROOT / ".prototools").read_text(encoding="utf-8")
        pins = dict(
            re.findall(r'^(\w+)\s*=\s*"([^"]+)"', text, flags=re.MULTILINE),
        )
        assert set(pins) == {"moon"}, f"the architecture project pins moon and nothing else; found {sorted(pins)}"

    def test_swcraft_yaml_carries_the_project_name_placeholder(self) -> None:
        text = (TEMPLATE_ROOT / "swcraft.yaml").read_text(encoding="utf-8")
        assert "{{PROJECT_NAME}}" in text
        manifest = text.replace("{{PROJECT_NAME}}", "placeholder")
        document = yaml.safe_load(manifest)
        assert document["model_root"] == "model"
        assert document["outputs"] == {
            "arxml": "dist/model.arxml",
            "needs": "dist/needs.json",
        }

    def test_moon_workspace_declares_the_project_root_only(self) -> None:
        document = yaml.safe_load((TEMPLATE_ROOT / ".moon" / "workspace.yml").read_text(encoding="utf-8"))
        assert list(document["projects"].values()) == ["."]


# ---------------------------------------------------------------------------
# Packaged, not merely present on disk
# ---------------------------------------------------------------------------


class TestPackaging:
    """The template is package data, resolvable from a wheel."""

    def test_template_is_readable_through_importlib_resources(self) -> None:
        """Every expected file is reachable through importlib.resources, as `swcraft init` reads it."""
        root = files("swcraft_tools") / "templates" / "vehicle-architecture"
        for relative in EXPECTED_FILES:
            traversable = root
            for part in relative.split("/"):
                traversable = traversable / part
            assert traversable.is_file(), f"{relative} is not reachable through importlib.resources"
        assert "$schema" in (root / "moon.yml").read_text(encoding="utf-8")
