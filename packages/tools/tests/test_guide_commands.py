"""The command gate for the user guide's pages: every quoted invocation is one the product provides.

Two strengths:

1. **Parsed.** Every ``swcraft …`` and ``swcraft-agent …`` line in a fenced shell block is checked
   against the live parsers (:func:`swcraft_tools.cli.dispatch.build_parser`,
   :func:`swcraft_agent.cli._build_parser`); the surface is derived from them, never transcribed.
2. **Executed.** The read-only commands (:data:`READ_ONLY_COMMANDS`) are also run, against a
   ``tmp_path`` copy of ``examples/basic`` and through ``sys.executable -m swcraft_tools.cli``.
   The exit code must be 0, or the line's ``# exits N``.

Everything else is parse-checked only: :data:`PARSE_ONLY_COMMANDS`, with a reason per entry, and
the two sets must partition the parser's subcommands, so a new subcommand needs a visible decision
here. The extraction is :mod:`test_onboarding_guide`'s, imported. Every rule is a pure function over
Markdown text, shown to fire on a mutated page.

**What this gate does not reach.**

- Whether the page tells the truth about the command's effect.
- Commands outside a fenced ``sh``/``bash``/``shell``/``console``/``zsh`` block.
- Every other program a page quotes (``git``, ``pip``, ...); the stricter rule is
  ``test_onboarding_guide.py``'s.
- What the write commands do: they are parsed and never run.
- Output: only the exit status is compared.
- Revisions and elements the fixture does not have: the copy has exactly one commit, so a page
  quoting ``HEAD~1`` states its own exit code; ``impact`` exits 0 for an element that does not
  exist, so a misspelled element is not a finding.
- Other platforms: the executions run on the host that runs them; Windows is unmeasured.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, make_git_repository, output_text, parser_subcommand_options, run_captured
from guide_fixtures import GUIDE_DIR, committed_pages
from swcraft_agent import cli as swcraft_agent_cli
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.guide_reference import cli as guide_reference_cli
from test_onboarding_guide import _cli_violation, _program, command_lines

EXAMPLE_PROJECT = REPO_ROOT / "examples" / "basic"

#: The subcommand surface of each product CLI, read from its live parser. ``swcraft_agent`` is
#: imported although ``swcraft_tools`` may not import it: this tests a document that quotes both.
_SWCRAFT_SUBCOMMANDS = parser_subcommand_options(build_parser())
_AGENT_SUBCOMMANDS = parser_subcommand_options(swcraft_agent_cli._build_parser())

#: The programs this gate is responsible for, mapped to their subcommand surface.
_PRODUCT_PROGRAMS = {
    "swcraft": _SWCRAFT_SUBCOMMANDS,
    "swcraft-agent": _AGENT_SUBCOMMANDS,
}

#: The commands that read a project and write nothing, and so can be run against a throwaway copy:
#: the generator's classification, made once (``WRITES_ONLY_WITH_REPORT`` is run without its flag).
READ_ONLY_COMMANDS = frozenset(guide_reference_cli.WRITES_NOTHING + guide_reference_cli.WRITES_ONLY_WITH_REPORT)

#: The subject a read-only command needs before it can be run, for the two that take one: a bare
#: ``impact`` or ``query`` is a usage error, which the executed direction must not read as a failure.
READ_ONLY_ARGUMENTS: dict[str, str] = {"impact": " ISpeed", "query": " swc"}

#: Everything else, with the reason it is parsed and never run. ``docs`` is here although it writes
#: nothing: a command that serves until interrupted must never be run from a test.
PARSE_ONLY_COMMANDS: dict[str, str] = {
    "init": "scaffolds a project tree, so running it would create files the page never mentioned",
    "generate": "writes the ARXML, the diagrams or the RST under the project's dist root",
    "validate": "generates the ARXML it then checks, so it writes before it grades",
    "import": "writes model files into the project, in the model, the component and the requirements modes",
    "mcp-serve": "execs a server that does not return, and whose whole purpose is mutation",
    "docs": "starts a server that does not return",
    "agent": "execs the assistant, which reads a conversation from stdin and does not return",
    "clean-outputs": "deletes the project's two generated directories, which is a write by removal",
    "experiment": "writes its report under the dist root, or where its configuration's output names, the report and "
    "the result of the experiment it runs, and for the trace kind its proposals file; and `trace --apply` writes "
    "the decided links into the model",
    "guard": "reads a hook payload on stdin; nothing for the executed gate to answer",
    "change-report": "writes its report, the Markdown, the JSON and the overview pictures, under the dist root",
    "passport": "writes the model's passport, to stdout or to the file its --output names",
}

#: A line may state the exit code it expects with a trailing ``# exits N``; without one it is 0. Kept
#: although no committed page uses it, so its non-zero branch is witnessed only by this module's fixtures:
#: ``diff`` outside a git repository exits non-zero, and a page may show it.
_EXIT_ANNOTATION = re.compile(r"\s*#\s*exits\s+(\d+)\s*$")

_PROJECT_OPTION = "--project"


# Reading the pages


def guide_pages() -> tuple[Path, ...]:
    """Every Markdown page of the guide, generated ones included (``version.md`` when a build left it)."""
    return tuple(committed_pages(pattern="*.md").values())


def stated_exit_code(line: str) -> tuple[str, int]:
    """Split *line* into the command and the exit code the page states for it."""
    match = _EXIT_ANNOTATION.search(line)
    if match is None:
        return line, 0
    return line[: match.start()].rstrip(), int(match.group(1))


def _tokenise(line: str) -> list[str] | None:
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    return tokens


def product_invocation(line: str) -> tuple[str, list[str]] | None:
    """``(program, arguments)`` if *line* invokes a product CLI (``python -m swcraft_tools.cli`` too), else ``None``."""
    tokens = _tokenise(line)
    if not tokens:
        return None
    program = _program(tokens[0])
    if program in _PRODUCT_PROGRAMS:
        return program, tokens[1:]
    if program in {"python", "python3"} and tokens[1:3] == ["-m", "swcraft_tools.cli"]:
        return "swcraft", tokens[3:]
    return None


def _names_a_product_program(command: str) -> bool:
    """Whether *command* looks like a product invocation without tokenising it (for an unbalanced quote)."""
    words = [word for word in command.split() if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word)]
    if not words:
        return False
    if _program(words[0]) in _PRODUCT_PROGRAMS:
        return True
    return _program(words[0]) in {"python", "python3"} and "swcraft_tools.cli" in words[1:3]


def invocation_violation(line: str) -> str | None:
    """Explain why *line* is not an invocation the product accepts, or ``None``."""
    command, _ = stated_exit_code(line)
    if _tokenise(command) is None:
        if not _names_a_product_program(command):
            return None
        return f"{command}: cannot be read as a shell command (unbalanced quoting)"
    invocation = product_invocation(command)
    if invocation is None:
        return None
    program, arguments = invocation
    return _cli_violation(command, program, _PRODUCT_PROGRAMS[program], arguments)


def invocation_violations(text: str) -> list[str]:
    """Every way *text* quotes a product command the product does not provide."""
    return [violation for line in command_lines(text) if (violation := invocation_violation(line))]


def quoted_product_lines(text: str) -> list[str]:
    """The product invocations *text* quotes, annotations included."""
    return [line for line in command_lines(text) if product_invocation(stated_exit_code(line)[0]) is not None]


def subcommand_of(line: str) -> str | None:
    """The ``swcraft`` subcommand *line* invokes, or ``None`` for anything else."""
    command, _ = stated_exit_code(line)
    invocation = product_invocation(command)
    if invocation is None or invocation[0] != "swcraft":
        return None
    positional = [token for token in invocation[1] if not token.startswith("-")]
    return positional[0] if positional else None


# Running the read-only ones


def copy_example_project(destination: Path) -> Path:
    """Copy ``examples/basic`` to *destination* and return the copied manifest."""
    shutil.copytree(
        EXAMPLE_PROJECT,
        destination,
        ignore=shutil.ignore_patterns("dist", "views", "__pycache__", ".git"),
    )
    return destination / "swcraft.yaml"


def execution_argv(line: str, manifest: Path) -> list[str]:
    """The argv that runs *line* against the project at *manifest*.

    The page's own ``--project`` value is replaced, or one appended; the interpreter is ``sys.executable``.
    """
    command, _ = stated_exit_code(line)
    invocation = product_invocation(command)
    assert invocation is not None, f"not a product invocation: {line}"
    _, arguments = invocation
    rewritten: list[str] = []
    replaced = False
    skip_next = False
    for index, token in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if token == _PROJECT_OPTION:
            rewritten.extend([_PROJECT_OPTION, str(manifest)])
            replaced = True
            skip_next = index + 1 < len(arguments)
            continue
        if token.startswith(f"{_PROJECT_OPTION}="):
            rewritten.append(f"{_PROJECT_OPTION}={manifest}")
            replaced = True
            continue
        rewritten.append(token)
    if not replaced:
        rewritten.extend([_PROJECT_OPTION, str(manifest)])
    return [sys.executable, "-m", "swcraft_tools.cli", *rewritten]


#: A hang guard, not a performance assertion: a fenced command that never returns is a named failure.
_INVOCATION_TIMEOUT_SECONDS = 120


def run_invocation(line: str, manifest: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run *line* against the copied project, capturing both streams."""
    return run_captured(
        execution_argv(line, manifest),
        cwd=manifest.parent,
        env=env,
        timeout=_INVOCATION_TIMEOUT_SECONDS,
    )


def execution_violations(text: str, manifest: Path) -> list[str]:
    """Every read-only command *text* quotes whose exit code is not the stated one."""
    violations: list[str] = []
    for line in quoted_product_lines(text):
        subcommand = subcommand_of(line)
        if subcommand not in READ_ONLY_COMMANDS:
            continue
        command, expected = stated_exit_code(line)
        completed = run_invocation(line, manifest)
        if completed.returncode != expected:
            violations.append(
                f"{command}: exited {completed.returncode}, the page states {expected}"
                f" — {output_text(completed).strip()}"
            )
    return violations


# Fixtures


@pytest.fixture(scope="module")
def example_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of ``examples/basic`` with one commit, and its manifest path.

    The history is a precondition: ``swcraft diff`` exits 1 without one (:class:`TestMeasuredExitCodes`).
    """
    manifest = copy_example_project(tmp_path_factory.mktemp("guide-commands") / "basic")
    make_git_repository(manifest.parent)
    return manifest


@pytest.fixture
def bare_copy(tmp_path: Path) -> Path:
    """The same copy with no repository at all."""
    return copy_example_project(tmp_path / "basic")


def _page(tmp_path: Path, body: str) -> str:
    """Write *body* as a guide page and read it back, so a gate reads a file."""
    page = tmp_path / "mutated.md"
    page.write_text(body, encoding="utf-8")
    return page.read_text(encoding="utf-8")


# The classification


class TestCommandClassification:
    def test_every_subcommand_is_classified(self) -> None:
        """The two sets partition the parser's subcommands — derived, not copied."""
        classified = READ_ONLY_COMMANDS | set(PARSE_ONLY_COMMANDS)
        declared = set(_SWCRAFT_SUBCOMMANDS)
        assert classified == declared, (
            "every `swcraft` subcommand must be either executed by this gate or held in "
            f"PARSE_ONLY_COMMANDS with a reason: unclassified {sorted(declared - classified)}, "
            f"classified but gone from the parser {sorted(classified - declared)}"
        )

    def test_the_two_sets_do_not_overlap(self) -> None:
        assert not READ_ONLY_COMMANDS & set(PARSE_ONLY_COMMANDS)

    def test_docs_is_parse_only_because_a_fence_quotes_it(self) -> None:
        """The membership that keeps `docs/guide/cli.md`'s `swcraft docs` fence safe.

        The partition test would accept `docs` in `WRITES_NOTHING`, and this gate would then execute a server.
        What it does not reach: which pages quote the command; it passes if that fence is deleted.
        """
        assert "docs" in PARSE_ONLY_COMMANDS
        assert "docs" not in READ_ONLY_COMMANDS

    def test_every_parse_only_command_states_why(self) -> None:
        for command, reason in PARSE_ONLY_COMMANDS.items():
            assert reason.strip(), f"{command} is parse-only with no reason"

    def test_the_agent_surface_is_read_from_its_parser(self) -> None:
        """A guard over an empty surface passes vacuously — pin that it is not empty."""
        assert "chat" in _AGENT_SUBCOMMANDS
        assert "--project" in _AGENT_SUBCOMMANDS["chat"]


# The gate over the committed pages


class TestQuotedCommandsParse:
    def test_the_guide_has_pages(self) -> None:
        assert guide_pages(), f"no guide page under {GUIDE_DIR}"

    def test_the_guide_quotes_product_commands_for_the_gate_to_read(self) -> None:
        """Both directions of this module are per-line, so an empty corpus passes every one of them.

        Matching ``fenced_blocks`` on the whole info string would drop a ``sh title="…"`` fence from both gates.
        """
        quoted = [line for page in guide_pages() for line in quoted_product_lines(page.read_text(encoding="utf-8"))]
        assert quoted, f"no guide page under {GUIDE_DIR} quotes a product command, so every gate here is vacuous"

    def test_the_guide_quotes_commands_the_executed_direction_runs(self) -> None:
        """The executed half has a narrower corpus again: only the read-only commands are run."""
        executed = [
            line
            for page in guide_pages()
            for line in quoted_product_lines(page.read_text(encoding="utf-8"))
            if subcommand_of(line) in READ_ONLY_COMMANDS
        ]
        assert executed, (
            "no guide page quotes any of the read-only commands, so `TestExecutedPages` runs nothing "
            f"against the corpus: {sorted(READ_ONLY_COMMANDS)}"
        )

    @pytest.mark.parametrize("page", guide_pages(), ids=lambda path: path.relative_to(REPO_ROOT).as_posix())
    def test_every_quoted_product_command_parses(self, page: Path) -> None:
        violations = invocation_violations(page.read_text(encoding="utf-8"))
        assert not violations, (
            f"{page.relative_to(REPO_ROOT)} quotes commands the product does not provide:\n"
            + "\n".join(f"  - {violation}" for violation in violations)
        )

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("swcraft frobnicate --project swcraft.yaml", "is not a subcommand"),
            ("swcraft validate --project swcraft.yaml --nonsense", "has no option"),
            ("swcraft list --project swcraft.yaml --json", "has no option"),
            ("swcraft-agent talk --project swcraft.yaml", "is not a subcommand"),
            ("swcraft-agent chat --project swcraft.yaml --loud", "has no option"),
            ("python -m swcraft_tools.cli nope --project swcraft.yaml", "is not a subcommand"),
            ('swcraft list --project "unbalanced', "unbalanced quoting"),
        ],
    )
    def test_the_gate_fires_on_an_invocation_that_does_not_exist(self, line: str, expected: str) -> None:
        violation = invocation_violation(line)
        assert violation is not None and expected in violation, violation

    @pytest.mark.parametrize(
        "line",
        [
            "swcraft init ./my-architecture --name my-architecture",
            "swcraft validate --project swcraft.yaml",
            "swcraft generate arxml --project swcraft.yaml --arxml-version 4.3.0",
            "swcraft list swc --project swcraft.yaml --format json",
            "swcraft diff --project swcraft.yaml HEAD~1 HEAD  # exits 1",
            "swcraft import model --source export.arxml --project swcraft.yaml --dry-run",
            "swcraft-agent chat --project swcraft.yaml --scoped",
            "python -m swcraft_tools.cli check-model --project swcraft.yaml",
            "git init",
            "code --install-extension swcraft-vscode.vsix",
            "git commit -m \"an unbalanced quote in somebody else's command",
        ],
    )
    def test_an_invocation_the_product_accepts_passes(self, line: str) -> None:
        assert invocation_violation(line) is None

    def test_a_page_quoting_an_unknown_option_fails_the_gate(self, tmp_path: Path) -> None:
        body = "# Getting started\n\n```sh\nswcraft validate --project swcraft.yaml --strict\n```\n"
        violations = invocation_violations(_page(tmp_path, body))
        assert len(violations) == 1 and "--strict" in violations[0], violations

    def test_a_page_quoting_a_command_the_product_has_passes_the_gate(self, tmp_path: Path) -> None:
        body = "# Getting started\n\n```sh\nswcraft validate --project swcraft.yaml\n```\n"
        assert invocation_violations(_page(tmp_path, body)) == []

    def test_prose_outside_a_shell_block_is_not_read(self, tmp_path: Path) -> None:
        """A limitation of the gate, pinned so it stays one."""
        body = "Run `swcraft frobnicate` to see the model.\n\n```yaml\nswcraft: frobnicate\n```\n"
        assert invocation_violations(_page(tmp_path, body)) == []


# The executions


class TestExecutionArgv:
    def test_the_page_project_is_replaced_by_the_copy(self, tmp_path: Path) -> None:
        manifest = tmp_path / "copy" / "swcraft.yaml"
        argv = execution_argv("swcraft list --project ./my-architecture/swcraft.yaml", manifest)
        assert argv[:3] == [sys.executable, "-m", "swcraft_tools.cli"]
        assert argv[3:] == ["list", "--project", str(manifest)]

    def test_the_equals_form_is_replaced_too(self, tmp_path: Path) -> None:
        manifest = tmp_path / "copy" / "swcraft.yaml"
        argv = execution_argv("swcraft list --project=./elsewhere.yaml", manifest)
        assert argv[3:] == ["list", f"--project={manifest}"]

    def test_a_line_without_a_project_gets_one(self, tmp_path: Path) -> None:
        manifest = tmp_path / "copy" / "swcraft.yaml"
        assert execution_argv("swcraft list", manifest)[3:] == ["list", "--project", str(manifest)]

    def test_the_annotation_is_not_passed_to_the_command(self, tmp_path: Path) -> None:
        manifest = tmp_path / "copy" / "swcraft.yaml"
        argv = execution_argv("swcraft diff --project swcraft.yaml  # exits 1", manifest)
        assert argv[3:] == ["diff", "--project", str(manifest)]


class TestMeasuredExitCodes:
    """The facts the executed direction rests on, each measured rather than assumed."""

    @pytest.mark.parametrize("command", sorted(READ_ONLY_COMMANDS))
    def test_every_read_only_command_exits_zero_against_the_copy(self, command: str, example_copy: Path) -> None:
        argument = READ_ONLY_ARGUMENTS.get(command, "")
        completed = run_invocation(f"swcraft {command}{argument} --project swcraft.yaml", example_copy)
        assert completed.returncode == 0, output_text(completed)

    def test_diff_exits_one_without_a_repository(self, bare_copy: Path) -> None:
        completed = run_invocation("swcraft diff --project swcraft.yaml", bare_copy, env={**os.environ, "LC_ALL": "C"})
        assert completed.returncode == 1, output_text(completed)
        # The terminal is the user's own: git's text stays, the shared sentence is a reply's.
        assert "fatal: not a git repository" in output_text(completed)
        assert "the model folder is in no git repository" not in output_text(completed)

    def test_impact_exits_zero_for_an_element_that_does_not_exist(self, example_copy: Path) -> None:
        """Which is why a misspelled element name in a page is not a finding here."""
        completed = run_invocation("swcraft impact NoSuchElement --project swcraft.yaml", example_copy)
        assert completed.returncode == 0, output_text(completed)

    def test_no_read_only_command_writes_into_the_copy(self, example_copy: Path) -> None:
        """Being read-only is the reason they may be run at all — so check it."""
        before = sorted(path.relative_to(example_copy.parent).as_posix() for path in example_copy.parent.rglob("*"))
        for command in sorted(READ_ONLY_COMMANDS):
            argument = READ_ONLY_ARGUMENTS.get(command, "")
            run_invocation(f"swcraft {command}{argument} --project swcraft.yaml", example_copy)
        after = sorted(path.relative_to(example_copy.parent).as_posix() for path in example_copy.parent.rglob("*"))
        assert after == before, f"a read-only command changed the tree: {sorted(set(after) ^ set(before))}"


class TestExecutedPages:
    def test_every_read_only_command_the_guide_quotes_exits_as_stated(self, example_copy: Path) -> None:
        for page in guide_pages():
            violations = execution_violations(page.read_text(encoding="utf-8"), example_copy)
            assert not violations, f"{page.relative_to(REPO_ROOT)}:\n" + "\n".join(f"  - {v}" for v in violations)

    def test_a_page_stating_the_wrong_exit_code_fails_the_gate(self, tmp_path: Path, example_copy: Path) -> None:
        body = "```sh\nswcraft list --project swcraft.yaml  # exits 3\n```\n"
        violations = execution_violations(_page(tmp_path, body), example_copy)
        assert len(violations) == 1 and "exited 0, the page states 3" in violations[0], violations

    def test_a_page_stating_the_right_exit_code_passes_the_gate(self, tmp_path: Path, example_copy: Path) -> None:
        body = "```sh\nswcraft list --project swcraft.yaml\nswcraft check-model --project swcraft.yaml\n```\n"
        assert execution_violations(_page(tmp_path, body), example_copy) == []

    def test_a_page_quoting_a_writing_command_is_not_executed(self, tmp_path: Path) -> None:
        """``validate`` writes the ARXML it grades; the gate must only parse it.

        A fresh copy of its own: on the shared copy "nothing was written" would depend on file order.
        """
        untouched = copy_example_project(tmp_path / "untouched")
        body = "```sh\nswcraft validate --project swcraft.yaml\n```\n"
        assert execution_violations(_page(tmp_path, body), untouched) == []
        assert not (untouched.parent / "dist").exists()
