"""Guard tests for the VS Code "SWCraft Architect" agent file and ``.github/copilot-instructions.md``.

The profile (``.github/agents/swcraft-architect.agent.md``) must grant the ``swcraft/*`` MCP wildcard
and no file-edit or terminal tool that would let an agent bypass the MCP-only mutation boundary by
editing YAML directly. The instructions file, which applies to any Copilot agent, must state the same
boundary. Both documents must name the protocol tools and no removed tool, and the profile must state
the review boundary the host actually offers: running a tool is not reviewing the change, only the
review question of the computed delta can commit, a weakening asks a second independent question, and
``Allow in this Session`` replaces neither answer.

``vscode_extension/fixtures/agents/`` stays byte-identical to its template sources; the Node side
pins it too (``packageVsix.test.ts``), and pinning it here lets the Python gate alone catch the drift.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
import yaml
from decision_table import load_projections
from swcraft_mcp.server import find, impact_of, resource_report
from swcraft_tools.authorization import SafetyChangeKind

AGENT_FILE_RELATIVE_PATH = "examples/basic/.github/agents/swcraft-architect.agent.md"
COPILOT_INSTRUCTIONS_RELATIVE_PATH = "examples/basic/.github/copilot-instructions.md"
EXAMPLE_HOOK_RELATIVE_PATH = "examples/basic/.github/hooks/swcraft.json"
TEMPLATE_AGENT_FILE_RELATIVE_PATH = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/agents/swcraft-architect.agent.md"
)
TEMPLATE_COPILOT_INSTRUCTIONS_RELATIVE_PATH = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/copilot-instructions.md"
)
TEMPLATE_HOOK_RELATIVE_PATH = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/hooks/swcraft.json"
)
TEMPLATE_CODEX_HOOK_RELATIVE_PATH = "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.codex/hooks.json"
TEMPLATE_ROOT_RELATIVE_PATH = "packages/tools/src/swcraft_tools/templates/vehicle-architecture"

#: The bundled copies inside the extension, keyed by the template source they must equal byte for byte:
#: *Onboard Project* writes the bundle into a user's project. An asset is named apart from its
#: destination where two destinations would collide in one folder.
BUNDLED_AGENT_ASSETS = {
    TEMPLATE_AGENT_FILE_RELATIVE_PATH: "vscode_extension/fixtures/agents/swcraft-architect.agent.md",
    TEMPLATE_COPILOT_INSTRUCTIONS_RELATIVE_PATH: "vscode_extension/fixtures/agents/copilot-instructions.md",
    TEMPLATE_HOOK_RELATIVE_PATH: "vscode_extension/fixtures/agents/swcraft.hooks.json",
    TEMPLATE_CODEX_HOOK_RELATIVE_PATH: "vscode_extension/fixtures/agents/swcraft.codex-hooks.json",
    # The files Codex and Claude Code read. CLAUDE.md's asset is ``swcraft.claude.md`` so a Claude Code
    # session reading ``fixtures/agents/`` loads no CLAUDE.md that imports the architect profile.
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.mcp.json": "vscode_extension/fixtures/agents/swcraft.mcp.json",
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.claude/settings.json": (
        "vscode_extension/fixtures/agents/swcraft.claude-settings.json"
    ),
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.codex/config.toml": "vscode_extension/fixtures/agents/swcraft.codex-config.toml",
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/AGENTS.md": "vscode_extension/fixtures/agents/AGENTS.md",
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/CLAUDE.md": "vscode_extension/fixtures/agents/swcraft.claude.md",
    # Kiro's steering file, rendered by the same two commands as AGENTS.md.
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.kiro/steering/swcraft.md": (
        "vscode_extension/fixtures/agents/swcraft.kiro-steering.md"
    ),
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.kiro/settings/mcp.json": "vscode_extension/fixtures/agents/swcraft.kiro-mcp.json",
    f"{TEMPLATE_ROOT_RELATIVE_PATH}/.kiro/hooks/swcraft-guard.json": (
        "vscode_extension/fixtures/agents/swcraft.kiro-hook.json"
    ),
    # The client table: ``swcraft init --client`` reads the package's copy, Onboard Project this one.
    # It sits beside the template, so ``swcraft init`` never copies it into a project.
    "packages/tools/src/swcraft_tools/templates/clients.json": "vscode_extension/fixtures/agents/clients.json",
}

#: Every agent document a reader is sent to: the example's pair and the template's pair.
AGENT_DOCUMENTS = (
    AGENT_FILE_RELATIVE_PATH,
    COPILOT_INSTRUCTIONS_RELATIVE_PATH,
    TEMPLATE_AGENT_FILE_RELATIVE_PATH,
    TEMPLATE_COPILOT_INSTRUCTIONS_RELATIVE_PATH,
)

#: The editing protocol's three tools: the whole authoring path; `discover` alone gives the baseline token.
PROTOCOL_TOOLS = ("discover", "read", "change_set")

# Substrings of the boundary statement: tolerant of minor rewording, not of removal.
COPILOT_INSTRUCTIONS_KEY_PHRASES = (
    "AI-assisted changes to the architecture model must go through",
    "never through direct edits",
    "model_root",
    "SWCRAFT_MANIFEST",
    "SWCRAFT_REVIEW",
    "change_set",
    "computed delta",
)

#: The clauses of the review boundary, each matched whitespace-normalized and each a separate claim:
#: dropping any one leaves a weaker paragraph that still reads plausibly.
REVIEW_BOUNDARY_CLAUSES = (
    # 1. the tool-run permission is not the review
    "permission is about *executing a tool*; it is **not** the review of the change",
    # 2. only the review question of the computed delta can commit, asked in the review tab
    "Only the review question displays the computed delta, and only its answer can commit it.",
    "It is asked in a SWCraft review tab that VS Code opens, where it waits until the user answers it there;",
    "nobody can answer, so say where the question is and that the change set waits.",
    # 3. a weakening asks a second, independent question
    "A weakened check asks a second, independent question.",
    # 4. `Allow in this Session` is about execution only
    "`Allow in this Session` affects tool execution only.",
    "it never answers the delta question or the safety question and can never stand in for either",
    # ... and the honest non-claim: there is no host-confirmed fallback
    "There is no mode in which the tool-permission dialog substitutes for the delta review",
    # 5. a declined safety answer ends that change set
    "A declined safety answer is final for that change set.",
)


def _normalized(text: str) -> str:
    """Collapse every whitespace run, so a line wrap cannot hide a claim."""
    return " ".join(text.split())


def _repo_root() -> Path:
    """Walk up from this test file to the repository root (contains .git)."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError("could not locate repository root from test file location")


def _agent_file_path() -> Path:
    return _repo_root() / AGENT_FILE_RELATIVE_PATH


def _copilot_instructions_path() -> Path:
    return _repo_root() / COPILOT_INSTRUCTIONS_RELATIVE_PATH


def _removed_tool_names() -> tuple[str, ...]:
    """Every tool name the decision table removes or withdraws, read from it so the guard cannot drift."""
    return tuple(load_projections()["removed"])


def _load_frontmatter(text: str) -> dict:
    """Split ``---``-delimited YAML frontmatter from the top of a markdown file."""
    assert text.startswith("---\n"), "agent file must start with a YAML frontmatter block (---)"
    _, _, rest = text.partition("---\n")
    frontmatter_text, separator, _body = rest.partition("\n---")
    assert separator == "\n---", "agent file frontmatter block must be closed with ---"
    data = yaml.safe_load(frontmatter_text)
    assert isinstance(data, dict), "frontmatter must parse to a YAML mapping"
    return data


class TestSwcraftArchitectAgentFile:
    def test_agent_file_exists_at_guarded_path(self):
        """VS Code scans .github/agents/ in the workspace root for custom agents."""
        path = _agent_file_path()
        assert path.is_file(), f"expected custom agent file at {path}"

    def test_frontmatter_parses_as_yaml(self):
        path = _agent_file_path()
        text = path.read_text(encoding="utf-8")
        frontmatter = _load_frontmatter(text)
        assert frontmatter  # non-empty mapping

    def test_frontmatter_has_name_and_description(self):
        frontmatter = _load_frontmatter(_agent_file_path().read_text(encoding="utf-8"))
        assert isinstance(frontmatter.get("name"), str) and frontmatter["name"]
        assert isinstance(frontmatter.get("description"), str) and frontmatter["description"]

    def test_swcraft_wildcard_tool_grant_present(self):
        """The tools: list must grant swcraft/* (all tools of the swcraft MCP server)."""
        frontmatter = _load_frontmatter(_agent_file_path().read_text(encoding="utf-8"))
        tools = frontmatter.get("tools")
        assert isinstance(tools, list), "tools: must be a YAML list"
        assert "swcraft/*" in tools, f"expected 'swcraft/*' in tools:, got {tools!r}"

    def test_tools_grant_the_wildcard_and_view_beside_the_hook(self):
        """The profile grants exactly `swcraft/*` and the read-only `view`, with the hook bounding `view` beside it.

        Exact match: any other tool, a file-edit one in particular, fails here.
        """
        frontmatter = _load_frontmatter(_agent_file_path().read_text(encoding="utf-8"))
        tools = frontmatter.get("tools")
        assert tools == ["swcraft/*", "view"], f"expected tools: to be exactly ['swcraft/*', 'view'], got {tools!r}"
        assert (_repo_root() / EXAMPLE_HOOK_RELATIVE_PATH).is_file(), "the hook that bounds `view` is missing"

    def test_body_states_fallback_rule(self):
        """The body must document the fallback rule: ask the user rather than edit YAML."""
        text = _agent_file_path().read_text(encoding="utf-8")
        _, _, body = text.partition("\n---\n")
        lower_body = body.lower()
        assert "ask the user" in lower_body
        assert "yaml" in lower_body


class TestCopilotInstructionsFile:
    """Guard tests for .github/copilot-instructions.md: the MCP-only boundary for any Copilot agent."""

    def test_copilot_instructions_file_exists(self):
        path = _copilot_instructions_path()
        assert path.is_file(), f"expected copilot instructions file at {path}"

    def test_boundary_statement_key_phrases_present(self):
        """All key phrases of the MCP-only boundary statement are present."""
        text = _copilot_instructions_path().read_text(encoding="utf-8")
        missing = [phrase for phrase in COPILOT_INSTRUCTIONS_KEY_PHRASES if phrase not in text]
        assert not missing, f"copilot-instructions.md is missing expected phrases: {missing}"

    def test_boundary_statement_forbids_direct_yaml_edits(self):
        """The file explicitly forbids direct YAML edits, even if other phrases survive a rewrite."""
        text = _copilot_instructions_path().read_text(encoding="utf-8")
        lower_text = text.lower()
        assert "never through direct edits to files" in lower_text
        assert "model/" in text

    def test_states_the_change_review_expectation(self):
        """The reviewed thing is the computed delta of a change set; there is no delete token."""
        text = _normalized(_copilot_instructions_path().read_text(encoding="utf-8"))
        assert "A change to the model is reviewed as a computed delta before it is applied." in text
        assert "only an affirmative answer to it commits the change" in text
        assert "there is no delete tool and no confirmation token" in text

    def test_documents_the_review_mode_environment_channel(self):
        """The instructions name `SWCRAFT_REVIEW=panel` and the fail-closed `stage-only` default.

        Otherwise a hand-made config's change sets never commit and nothing points at the cause.
        """
        text = _normalized(_copilot_instructions_path().read_text(encoding="utf-8"))
        assert "`SWCRAFT_REVIEW=panel`" in text
        assert "waits until the user answers it there" in text
        assert "nobody can answer, so say where the question is and that the change set waits." in text
        assert "stage-only" in text


class TestTheStatedWaitHasNoDeadline:
    """The panel wait has no deadline; the guide's Codex stanza names Codex's own limit, one day."""

    def test_the_guides_codex_stanza_names_one_day(self) -> None:
        guide = (_repo_root() / "docs" / "guide" / "vscode.md").read_text(encoding="utf-8")
        limits = [int(value) for value in re.findall(r"^tool_timeout_sec = (\d+)$", guide, flags=re.MULTILINE)]
        assert limits == [86400], limits


class TestProtocolToolVocabulary:
    """Neither document may teach a tool the server dropped.

    Matching is on identifier boundaries, so the generic `read` does not match a longer removed name.
    """

    @pytest.mark.parametrize("path_getter", [_agent_file_path, _copilot_instructions_path], ids=["profile", "copilot"])
    def test_names_no_removed_tool(self, path_getter):
        text = path_getter().read_text(encoding="utf-8")
        named = [tool for tool in _removed_tool_names() if re.search(rf"(?<![\w]){re.escape(tool)}(?![\w])", text)]
        assert not named, (
            f"{path_getter().name} still names tools the server no longer has: {named} — the generic protocol "
            "replaces them"
        )

    def test_the_decision_table_actually_lists_removed_tools(self):
        """Guards the guard: without listed removals the test above would pass vacuously."""
        removed = _removed_tool_names()
        assert len(removed) == 32, f"expected the 29 schema-coupled tools and the 3 withdrawn, got {len(removed)}"
        assert "delete_element" in removed
        assert "read_swc" in removed
        assert "check_port_compatibility" in removed

    @pytest.mark.parametrize("tool", PROTOCOL_TOOLS)
    def test_the_profile_names_each_protocol_tool(self, tool: str):
        text = _agent_file_path().read_text(encoding="utf-8")
        assert re.search(rf"`{re.escape(tool)}`", text), (
            f"the custom agent profile never mentions `{tool}`, one of the three tools "
            "that are now the whole authoring path"
        )

    def test_the_profile_keeps_the_retained_specialized_rules(self):
        """`import_component` is planning-only, and "do these ports fit" is the staged change set's verdict."""
        text = _normalized(_agent_file_path().read_text(encoding="utf-8"))
        assert "When the user asks only whether two ports fit, the verdict is the server's" in text
        assert "offer to stage that connector in a `change_set`" in text
        assert "Reading both ports' interfaces shows what they declare and is advisory only." in text
        assert "**`import_component` writes nothing — it plans.**" in text
        assert "a ready-to-submit `change_set` request that you submit yourself" in text


class TestReviewBoundaryStatement:
    """The profile must state the boundary the host actually offers.

    Every clause is a separate claim because each one, dropped alone, leaves a
    paragraph that still reads plausibly while describing a weaker boundary.
    """

    @pytest.mark.parametrize("clause", REVIEW_BOUNDARY_CLAUSES)
    def test_clause_present(self, clause: str):
        text = _normalized(_agent_file_path().read_text(encoding="utf-8"))
        assert _normalized(clause) in text, f"the review boundary paragraph lost: {clause!r}"

    def test_the_weakening_directions_survive_the_rewrite(self):
        """The budget weakening directions and their inverses stay stated in full.

        Admitting a core to an ``accessible`` list is listed apart from the four ``size x threshold``
        directions because it weakens a different check.

        What this does not reach: it reads the ``.github/`` profile alone, never the template's copy.
        """
        text = _normalized(_agent_file_path().read_text(encoding="utf-8"))
        assert "A resource's effective limit is `size` x `threshold`" in text
        for direction in (
            "**raising** a `threshold`",
            "**enlarging** a memory's `size`",
            "switching `enforcement` from `strict` to `advisory`",
            "**deleting** the resource, which removes its check altogether",
            "**Admitting** a core to a memory's `accessible` list",
        ):
            assert direction in text, f"weakening direction lost: {direction}"
        assert (
            "The inverses (lowering a `threshold`, shrinking a memory, `advisory` -> `strict`) tighten the check"
            in text
        )
        assert "**Withdrawing** a core from the list is the inverse and is not a weakening" in text

    def test_approving_the_delta_is_not_approving_the_weakening(self):
        text = _normalized(_agent_file_path().read_text(encoding="utf-8"))
        assert "**approving the delta is not approving the weakening**" in text


class TestDeclinedSafetyAnswerAndIntentHonesty:
    """A declined safety answer is final, and `intent` never claims an approval that was not given.

    An agent in this host once resubmitted a declined change set with `intent` rewritten to claim the
    user had authorized the weakening. Both rules are prose-only and the profile must say so: an
    approval binds to one change-set id, so the server cannot know a previous one was declined.
    """

    def _flat(self) -> str:
        return _normalized(_agent_file_path().read_text(encoding="utf-8"))

    def test_the_decline_is_final_and_the_retry_is_named(self):
        text = self._flat()
        assert "**A declined safety answer is final for that change set.**" in text
        assert "Do not resubmit the same weakening" in text, (
            "the act the smoke observed must be forbidden by name, not merely implied"
        )
        assert "do not reword it" in text

    def test_the_profile_says_plainly_that_nothing_enforces_it(self):
        text = self._flat()
        assert "**Nothing enforces this for you.**" in text
        assert "the server cannot know a previous change set was declined" in text, (
            "the profile must give the reason — approvals bind to one change-set id — not just the rule"
        )

    def test_the_profile_claims_no_mechanism_it_does_not_have(self):
        """Nothing in the engine remembers a declined change set, so no sentence may promise it."""
        text = self._flat()
        for false_claim in (
            "the server refuses a resubmitted",
            "the server remembers",
            "a declined change set cannot be resubmitted",
            "resubmission is blocked",
        ):
            assert false_claim not in text, f"the profile claims a mechanism that does not exist: {false_claim!r}"

    def test_intent_may_not_assert_an_approval_that_was_not_given(self):
        text = self._flat()
        assert "never who agreed to it" in text
        assert "a false `intent` is a false statement made to the person deciding" in text
        assert "never restate a claim they declined as though they had made it" in text

    def test_the_older_intent_rule_is_extended_not_replaced(self):
        """The sentence about the operations and the one about attributed approval both survive."""
        text = self._flat()
        assert "an `intent` that claims a change the operations do not make is simply false" in text
        assert "What that person approves is the **delta**" in text


class TestBundledExtensionCopies:
    """The Python half of the two-sided byte-sync guard over the `.vsix`'s onboarding assets."""

    @pytest.mark.parametrize(
        ("source", "bundled"),
        sorted(BUNDLED_AGENT_ASSETS.items()),
        ids=lambda value: value.rsplit("/", 1)[-1],
    )
    def test_bundled_copy_is_byte_identical(self, source: str, bundled: str) -> None:
        root = _repo_root()
        source_bytes = (root / source).read_bytes()
        bundled_path = root / bundled
        assert bundled_path.is_file(), f"expected the bundled copy at {bundled_path}"
        assert bundled_path.read_bytes() == source_bytes, f"{bundled} has drifted from {source} — re-copy the file"


#: The three homes of the profile: the template, the bundle, the example's. Their whole files are held
#: byte-equal elsewhere; the regions are compared here so a drift names the region.
PROFILE_HOMES = (
    TEMPLATE_AGENT_FILE_RELATIVE_PATH,
    BUNDLED_AGENT_ASSETS[TEMPLATE_AGENT_FILE_RELATIVE_PATH],
    AGENT_FILE_RELATIVE_PATH,
)

#: The shared regions, each from its opening line up to (not including) its closing line; the
#: generated schema section includes its markers.
SHARED_PROFILE_REGIONS = {
    "persona": ("## Persona\n", "## Tool Usage Rules\n"),
    "rules": ("## Tool Usage Rules\n", "## Fallback Rule"),
    "fallback": ("## Fallback Rule", "<!-- BEGIN GENERATED: model schema -->"),
    "schema": ("<!-- BEGIN GENERATED: model schema -->", "<!-- END GENERATED: model schema -->\n"),
}


def _profile_region(text: str, region: str) -> str:
    start, end = SHARED_PROFILE_REGIONS[region]
    assert text.count(start) == 1, f"the region {region!r} must open exactly once with {start!r}"
    head = text.index(start)
    tail = text.index(end, head)
    return text[head : tail + (len(end) if region == "schema" else 0)]


class TestTheThreeHomesShareTheBody:
    """The three homes carry the same persona, rules, fallback rule and generated schema section.

    What it does not reach: whether an agent follows the rules (the benchmark measures that).
    """

    @pytest.mark.parametrize("region", sorted(SHARED_PROFILE_REGIONS))
    @pytest.mark.parametrize("home", PROFILE_HOMES[1:], ids=["bundle", "example"])
    def test_the_region_is_the_template_s(self, home: str, region: str) -> None:
        root = _repo_root()
        template = _profile_region((root / TEMPLATE_AGENT_FILE_RELATIVE_PATH).read_text(encoding="utf-8"), region)
        assert _profile_region((root / home).read_text(encoding="utf-8"), region) == template, (
            f"{home}: the {region} region differs from the template's — copy it from "
            f"{TEMPLATE_AGENT_FILE_RELATIVE_PATH} (the schema section: `python scripts/dev.py schemas`)"
        )

    def test_the_rules_carry_the_long_list_line(self) -> None:
        """A long list is answered by its number, the first twenty names and a pointer to the rest."""
        root = _repo_root()
        for home in PROFILE_HOMES:
            rules = _normalized(_profile_region((root / home).read_text(encoding="utf-8"), "rules"))
            assert (
                "When a list you answer the user with is longer than twenty, answer the number, "
                "the first twenty names and that the rest come by a list request — never a pattern, never a range."
            ) in rules, home


def _named_extension_commands(text: str) -> set[str]:
    """Every ``SWCraft: <Title>`` the *text* tells a reader to run, whatever markup quotes it."""
    return set(re.findall(r"SWCraft: (?:[A-Z][A-Za-z0-9]*)(?: [A-Z][A-Za-z0-9]*)*", text))


def _contributed_command_titles() -> set[str]:
    """The command titles the extension actually contributes, read from its manifest."""
    manifest = json.loads((_repo_root() / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
    return {entry["title"] for entry in manifest["contributes"]["commands"]}


class TestTheDocumentsNameNoRetiredCommand:
    """A document may only send a reader to a command the extension still contributes.

    Checked against the extension's manifest, not a list kept here: a deleted command once stayed
    named in the instructions with every gate green.

    What it does not reach: anything else the documents say about the editor, and the guide, the
    walkthrough or the template's own copies.
    """

    @pytest.mark.parametrize("relative", sorted(AGENT_DOCUMENTS))
    def test_every_named_command_is_one_the_extension_contributes(self, relative: str) -> None:
        named = _named_extension_commands((_repo_root() / relative).read_text(encoding="utf-8"))
        unknown = sorted(named - _contributed_command_titles())
        assert unknown == [], (
            f"{relative} tells the reader to run a command the extension does not contribute: {unknown}"
        )

    def test_the_rule_fires_on_the_command_that_was_actually_stale(self) -> None:
        """The stale sentence replayed, so the rule above cannot pass with an extractor matching nothing."""
        stale = 'use the "SWCraft: Repair MCP Config" extension command\n'
        assert _named_extension_commands(stale) == {"SWCraft: Repair MCP Config"}
        assert _named_extension_commands(stale) - _contributed_command_titles() == {"SWCraft: Repair MCP Config"}

    def test_the_documents_name_at_least_one_command_between_them(self) -> None:
        """…and the extractor is reading the real documents, not returning ``set()``."""
        found = {
            command
            for relative in AGENT_DOCUMENTS
            for command in _named_extension_commands((_repo_root() / relative).read_text(encoding="utf-8"))
        }
        assert found, "neither document names a SWCraft command; the extractor has gone blind"


class TestTheProfileEnumeratesEveryKindTheServerClassifies:
    """Keyed by :class:`SafetyChangeKind`, so a kind added without a sentence in the profile fails here.

    A prompt that enumerates fewer weakenings than the server classifies under-states the guard to the
    one reader who cannot check it.

    What this does not reach: it reads the ``.github/`` profile and the template's copy by these phrases
    alone; nothing compares their other sentences.
    """

    #: One phrase per kind, in the profile's own words: a paraphrase would pass over a weaker sentence.
    PHRASES = {
        SafetyChangeKind.RAISE_THRESHOLD: "**raising** a `threshold`",
        SafetyChangeKind.ENLARGE_POOL: "**enlarging** a memory's `size`",
        SafetyChangeKind.WEAKEN_ENFORCEMENT: "switching `enforcement` from `strict` to `advisory`",
        SafetyChangeKind.DELETE_RESOURCE: "**deleting** the resource, which removes its check altogether",
        SafetyChangeKind.WIDEN_ACCESS: "**Admitting** a core to a memory's `accessible` list",
        SafetyChangeKind.DECLARE_MEASURE: "**Declaring** a `measures` entry on a port",
        SafetyChangeKind.DECLARE_COEXISTENCE: "**writing** a task's `coexistence` argument",
        SafetyChangeKind.LOWER_ALLOCATION: "**lowering or removing a declared level**",
        SafetyChangeKind.RAISE_CAPABILITY: "**Raising** a core's or a memory's `asil_capability`",
        SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE: "**Writing** a partition's `coexistence` argument",
        SafetyChangeKind.GRANT_PARTITION_TRUST: "**Making a partition `trusted`**",
        SafetyChangeKind.DECLARE_PARTITION_PROTECTION: "**Confining a `trusted` partition**",
    }

    def test_every_kind_has_a_phrase_here(self):
        assert set(self.PHRASES) == set(SafetyChangeKind)

    @pytest.mark.parametrize("kind", list(SafetyChangeKind), ids=lambda kind: kind.value)
    def test_the_profile_states_it(self, kind):
        text = _normalized(_agent_file_path().read_text(encoding="utf-8"))
        assert _normalized(self.PHRASES[kind]) in text, f"the profile does not state {kind.value}"

    @pytest.mark.parametrize("kind", list(SafetyChangeKind), ids=lambda kind: kind.value)
    def test_the_template_s_copy_states_it(self, kind):
        text = _normalized((_repo_root() / TEMPLATE_AGENT_FILE_RELATIVE_PATH).read_text(encoding="utf-8"))
        assert _normalized(self.PHRASES[kind]) in text, f"the template's profile does not state {kind.value}"

    def test_both_copies_say_a_grant_asks_the_second_question(self):
        sentence = _normalized("When the staged delta weakens a check or grants a partition trust, the server asks")
        for relative in (AGENT_FILE_RELATIVE_PATH, TEMPLATE_AGENT_FILE_RELATIVE_PATH):
            assert sentence in _normalized((_repo_root() / relative).read_text(encoding="utf-8")), relative


class TestRecipeArguments:
    """The recipes' argument names are held to the MCP tools' own signatures, which the kernels do not see.

    Read through ``test_agent_recipes.py``'s loader, so a recipe moved to a guide page is held too.
    """

    @staticmethod
    def _recipe_blocks(tool: str) -> list[dict]:
        import test_agent_recipes as recipes  # noqa: PLC0415 - the tools tree's loader, on the path in the tests

        return [dict(request) for recipe in recipes.all_recipes() if recipe.tool == tool for request in recipe.requests]

    def test_the_impact_recipe_sends_only_arguments_the_impact_of_tool_takes(self) -> None:
        blocks = self._recipe_blocks("impact_of")
        assert [arguments["form"] for arguments in blocks] == ["count", "summary"], blocks
        for arguments in blocks:
            assert set(arguments) <= set(inspect.signature(impact_of).parameters)

    def test_the_resource_recipes_send_only_arguments_the_resource_report_tool_takes(self) -> None:
        blocks = self._recipe_blocks("resource_report")
        assert len(blocks) == 2, blocks
        for arguments in blocks:
            assert set(arguments) <= set(inspect.signature(resource_report).parameters)
            assert len(arguments) == 1, "one question per call"

    def test_the_find_recipes_send_only_arguments_the_find_tool_takes(self) -> None:
        """One recipe by `text`, one by `for_requirement`, never `names` beside either."""
        blocks = self._recipe_blocks("find")
        assert [sorted(arguments) for arguments in blocks] == [["text"], ["candidates", "for_requirement"]], blocks
        for arguments in blocks:
            assert set(arguments) <= set(inspect.signature(find).parameters)
