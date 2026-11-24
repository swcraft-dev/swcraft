"""The command and version readings of the onboarding documents, and the README's links.

A command quoted in ``docs/guide/architecture-repository.md`` or the README drifts
when a ``dev.py`` command is renamed or a CLI flag dropped. :func:`command_violation`
re-derives the valid surface from ``swcraft_tools.cli``'s parser, ``scripts/dev.py``'s
dispatch table and ``scripts/workstation/install.py``'s parser -- never from a
hand-written list. A project installs nothing, so ``venv``, ``pip`` and
``bootstrap.py`` are *named* violations. :func:`version_literals` finds a product
version written where a version is acted on.

This module holds the guide page to these readings and every ``dev.py`` command the
README names to the dispatch table, shows each reading fire against a mutated line,
and checks that every relative link in ``README.md`` resolves.

**What this does not reach**: the command rule reads fenced shell blocks only, so the
same words in prose, an inline code span or a ``text`` block are invisible to it; no
test checks that the page still documents each step (the toolchain install, the pin,
the moon prerequisite), that it delimits the downstream-integration boundary or that
it carries no pipeline definition; a ``dev.py`` command named behind a global flag
(``dev.py --skip-node install``) is not read; the README's quick start is not held to
the command readings here; and an absolute URL in the README is not fetched.
"""

from __future__ import annotations

import re
import shlex

import pytest
from conftest import (
    REPO_ROOT,
    dev_module,
    parser_option_strings,
    parser_subcommand_options,
    workstation_install_module,
)
from swcraft_agent import cli as swcraft_agent_cli
from swcraft_tools.cli.dispatch import build_parser

_README_PATH = REPO_ROOT / "README.md"
_ARCHITECTURE_REPOSITORY_PAGE = REPO_ROOT / "docs" / "guide" / "architecture-repository.md"


_DEV = dev_module()
_INSTALL = workstation_install_module()

#: Fenced-block languages whose lines are read as runnable commands. A block with
#: no info string is prose in a box, not an instruction.
_SHELL_LANGUAGES = frozenset({"sh", "bash", "shell", "console", "zsh"})

#: Programs the guide may name that this repository does not provide: the build
#: orchestrator, the toolchain installer, the editor, and shell built-ins.
_EXTERNAL_PROGRAMS = frozenset({"moon", "proto", "code", "git", "cd"})

#: The only ``python -m`` modules the guide may quote. ``venv`` and ``pip`` are
#: absent on purpose: SWCraft is one installation per machine, so a document
#: quoting either describes a retired shape and is reported, not validated.
_PYTHON_MODULES = frozenset(
    {
        "swcraft_tools.cli",
        "swcraft_tools.arxml_validation",
        "swcraft_mcp.server",
    }
)

#: The flags the workstation archive's installer accepts, read from its own
#: parser, so a flag renamed in ``install.py`` turns this gate red.
_INSTALLER_FLAGS = parser_option_strings(_INSTALL.build_parser())

#: The archive's two entry points: wrappers that hand every argument to
#: ``install.py``, so both accept exactly :data:`_INSTALLER_FLAGS`.
_INSTALLER_WRAPPERS = frozenset({"swcraft_install.sh", "swcraft_install.cmd"})

#: Repository scripts the guide may invoke as ``python <script>``, with the flags
#: each one accepts. ``dev.py``'s *commands* are read from its dispatch table.
#: ``bootstrap.py`` is absent on purpose: no scaffold writes it, so quoting it is a
#: finding (a case of
#: :meth:`TestQuotedCommandsExist.test_the_guard_fires_on_a_command_that_does_not_exist`).
_SCRIPT_FLAGS = {
    "dev.py": frozenset({"--repo-root", "--skip-node", "--help"}),
    "install.py": _INSTALLER_FLAGS,
    "build_artifacts.py": frozenset({"--repo-root", "--wheelhouse", "--help"}),
}

# ---------------------------------------------------------------------------
# Pure markdown helpers
# ---------------------------------------------------------------------------


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(language, body)`` for every fenced code block.

    The language is the **first word** of the info string: Markdown allows
    attributes after it (``sh title="a caption"``), and matching the whole string
    would let such a block fall out of every gate built on this.
    """
    blocks: list[tuple[str, str]] = []
    language: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            if language is None:
                language = line[3:].strip().lower().split(" ", 1)[0]
                body = []
            else:
                blocks.append((language, "\n".join(body)))
                language = None
            continue
        if language is not None:
            body.append(line)
    return blocks


def command_lines(text: str) -> list[str]:
    """Runnable command lines quoted in the guide's shell blocks.

    Comments and blanks are dropped; a line continued with a trailing backslash
    is joined with the next one, so a wrapped command is validated whole.
    """
    lines: list[str] = []
    for language, body in fenced_blocks(text):
        if language not in _SHELL_LANGUAGES:
            continue
        pending = ""
        for raw in body.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                pending += stripped[:-1].strip() + " "
                continue
            lines.append((pending + stripped).strip())
            pending = ""
        if pending:
            lines.append(pending.strip())
    return lines


# ---------------------------------------------------------------------------
# The command surface this repository actually provides
# ---------------------------------------------------------------------------


_SWCRAFT_SUBCOMMANDS = parser_subcommand_options(build_parser())
_DEV_COMMANDS = frozenset(_DEV.COMMANDS)

#: Subcommands whose arguments are **another program's** command line, mapped to
#: the label and the surface that command line is judged against.
#:
#: `swcraft agent` passes everything after it (``argparse.REMAINDER``) unchanged to
#: `swcraft_agent.cli`, so its remainder is judged against that parser: this one
#: declares no ``--project`` or ``--scoped`` and would accept any nonsense.
_REMAINDER_SUBCOMMANDS = {
    "agent": ("swcraft-agent", parser_subcommand_options(swcraft_agent_cli._build_parser())),
}


def _program(token: str) -> str:
    name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


def _unknown_flags(tokens: list[str], allowed: frozenset[str]) -> list[str]:
    unknown = []
    for token in tokens:
        if not token.startswith("--"):
            continue
        flag = token.split("=", 1)[0]
        if flag not in allowed:
            unknown.append(flag)
    return unknown


def _cli_violation(line: str, program: str, surface: dict[str, frozenset[str]], rest: list[str]) -> str | None:
    positional = [token for token in rest if not token.startswith("-")]
    if not positional:
        unknown = _unknown_flags(rest, frozenset({"--version", "--help"}))
        return f"{line}: `{program}` has no option {', '.join(unknown)}" if unknown else None
    subcommand = positional[0]
    if subcommand not in surface:
        return f"{line}: `{program} {subcommand}` is not a subcommand ({', '.join(sorted(surface))})"
    handed_over = _REMAINDER_SUBCOMMANDS.get(subcommand) if program == "swcraft" else None
    if handed_over is not None:
        label, remainder_surface = handed_over
        return _cli_violation(line, label, remainder_surface, rest[rest.index(subcommand) + 1 :])
    unknown = _unknown_flags(rest, surface[subcommand])
    if unknown:
        return f"{line}: `{program} {subcommand}` has no option {', '.join(unknown)}"
    return None


def _python_violation(line: str, rest: list[str]) -> str | None:
    if not rest:
        return f"{line}: bare `python` invocation"
    if rest[0] == "-m":
        if len(rest) < 2:
            return f"{line}: `python -m` without a module"
        module = rest[1]
        if module not in _PYTHON_MODULES:
            return f"{line}: `python -m {module}` is not part of the documented surface"
        if module == "swcraft_tools.cli":
            return _cli_violation(line, "swcraft", _SWCRAFT_SUBCOMMANDS, rest[2:])
        return None
    script = _program(rest[0])
    if script not in _SCRIPT_FLAGS:
        return f"{line}: `{script}` is not a script this repository provides"
    unknown = _unknown_flags(rest[1:], _SCRIPT_FLAGS[script])
    if unknown:
        return f"{line}: `{script}` has no option {', '.join(unknown)}"
    if script == "dev.py":
        commands = [token for token in rest[1:] if not token.startswith("-")]
        if not commands:
            return f"{line}: `dev.py` without a command"
        if commands[0] not in _DEV_COMMANDS:
            return f"{line}: `dev.py {commands[0]}` is not a command ({', '.join(sorted(_DEV_COMMANDS))})"
    return None


def command_violation(line: str) -> str | None:
    """Explain why *line* is not a command this repository provides, or ``None``."""
    tokens = shlex.split(line)
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return f"{line}: no command"
    program = _program(tokens[0])
    rest = tokens[1:]
    if program in {"python", "python3"}:
        return _python_violation(line, rest)
    if program == "swcraft":
        return _cli_violation(line, "swcraft", _SWCRAFT_SUBCOMMANDS, rest)
    if program in _INSTALLER_WRAPPERS:
        unknown = _unknown_flags(rest, _INSTALLER_FLAGS)
        return f"{line}: `{program}` has no option {', '.join(unknown)}" if unknown else None
    if program in _EXTERNAL_PROGRAMS:
        return None
    return f"{line}: `{program}` is neither provided by this repository nor a documented external tool"


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


#: A product version written where a version is *acted on*: a package pin, the
#: extension's packaged filename, the workstation archive's filename. Any number
#: matches, so a pin to an old release is caught, not only the current version.
_VERSION_LITERAL = re.compile(
    r"swcraft-(?:tools|mcp|agent)==\d+\.\d+\.\d+"
    r"|swcraft-vscode-\d+\.\d+\.\d+\.vsix"
    r"|swcraft-\d+\.\d+\.\d+-[a-z0-9-]+\.zip"
)


def version_literals(text: str) -> list[str]:
    """Every product-version literal *text* quotes in an install-shaped context."""
    return _VERSION_LITERAL.findall(text)


# ---------------------------------------------------------------------------
# The readings fire
# ---------------------------------------------------------------------------


class TestQuotedCommandsExist:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("swcraft frobnicate --project x", "is not a subcommand"),
            ("swcraft init ~/x --integrate moon", "has no option"),
            ("python scripts/dev.py buidl", "is not a command"),
            ("python scripts/dev.py test --nonsense", "has no option"),
            ("python scripts/nope.py", "is not a script this repository provides"),
            ("python -m swcraft_tools.nope", "not part of the documented surface"),
            ("python -m venv .venv", "not part of the documented surface"),
            ("python -m pip install swcraft-tools", "not part of the documented surface"),
            ("pip install --no-index swcraft-tools", "neither provided by this repository"),
            ("python bootstrap.py --artifacts /wheels", "is not a script this repository provides"),
            ("./swcraft_install.sh --artifacts /wheels", "has no option"),
            ("kubectl apply -f x.yaml", "neither provided by this repository"),
        ],
    )
    def test_the_guard_fires_on_a_command_that_does_not_exist(self, line: str, expected: str) -> None:
        violation = command_violation(line)
        assert violation is not None and expected in violation, violation

    @pytest.mark.parametrize(
        "line",
        [
            "python scripts/dev.py test-model-integration",
            "python scripts/dev.py workstation-archive",
            "./swcraft_install.sh",
            "swcraft_install.cmd --home C:/tmp/home --no-path --no-vsix",
            "python install.py --index-url https://example.invalid/simple",
            "swcraft init ~/projects/x --integration moon --name x",
            "moon run :build",
            "code --install-extension /tmp/swcraft-vscode-0.1.0.vsix",
        ],
    )
    def test_valid_commands_pass(self, line: str) -> None:
        assert command_violation(line) is None


class TestVersionLiterals:
    """Neither document quotes a version: a literal is a finding, so a bump edits neither.

    The toolchain arrives as one file whose *name* carries the version, and both
    documents write that name as a placeholder.

    **What this does not reach**: an install-shaped literal is matched, a version in
    ordinary prose ("0.6.0 introduced …") is not, since these documents legitimately
    name AUTOSAR ``4.2.2`` and moon ``2.3.5``.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "pip install swcraft-tools==0.0.1",
            "code --install-extension swcraft-vscode-0.0.1.vsix",
            "unzip swcraft-0.0.1-windows-x64.zip",
        ],
    )
    def test_the_rule_fires_on_each_shape(self, line: str) -> None:
        """One case per alternative, so no branch of the pattern is untested."""
        assert version_literals(line) == [line.split()[-1].rstrip(".")]

    def test_a_placeholder_is_not_a_literal(self) -> None:
        text = "Unpack `swcraft-<version>-<platform>.zip` and install `swcraft-vscode-<version>.vsix`."
        assert version_literals(text) == []


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def readme() -> str:
    return _README_PATH.read_text(encoding="utf-8")


#: A Markdown inline link's target, ignoring pure-anchor links (`(#section)`).
_LINK_PATTERN = re.compile(r"\]\(([^)#][^)]*)\)")


def _readme_relative_links(text: str) -> list[str]:
    """Every repository-relative link target quoted in *text*, in order."""
    return [
        target for target in _LINK_PATTERN.findall(text) if not target.startswith(("http://", "https://", "mailto:"))
    ]


def dangling_readme_links(readme: str) -> list[str]:
    """Every relative link in *readme* that resolves to nothing in the repository."""
    return [target for target in _readme_relative_links(readme) if not (REPO_ROOT / target).exists()]


class TestReadme:
    def test_every_relative_link_in_the_readme_resolves(self, readme: str) -> None:
        """Every relative link in the README resolves; an absolute URL is not fetched (does not reach)."""
        dangling = dangling_readme_links(readme)
        assert not dangling, "README.md links to paths that do not exist:\n" + "\n".join(
            f"  - {target}" for target in dangling
        )

    def test_the_link_gate_reads_a_non_empty_corpus(self, readme: str) -> None:
        """A link gate over a page with no relative links passes vacuously."""
        assert _readme_relative_links(readme), "README.md carries no relative link at all"

    def test_a_dangling_relative_link_is_reported(self, readme: str) -> None:
        """The guard fires against a link that cannot resolve, through the gate's own function."""
        mutated = readme.replace("(docs/guide/index.md)", "(docs/guide/does-not-exist.md)")
        assert mutated != readme, "the docs/guide/index.md link moved — update this mutation"
        assert dangling_readme_links(mutated) == ["docs/guide/does-not-exist.md"]

    def test_every_dev_command_the_readme_names_is_in_the_dispatch_table(self, readme: str) -> None:
        """Every ``dev.py`` command the README names is in the live dispatch table."""
        named = set(_DEV_COMMAND_NAMED.findall(readme))
        assert named, "the README names no dev.py command"
        assert named <= _DEV_COMMANDS, sorted(named - _DEV_COMMANDS)

    def test_a_dev_command_not_in_the_table_is_reported(self) -> None:
        """The reading fires on a command the dispatch table does not hold."""
        named = set(_DEV_COMMAND_NAMED.findall("run `python scripts/dev.py buidl` first"))
        assert named - _DEV_COMMANDS == {"buidl"}


#: A ``dev.py`` command the README names directly (a global flag before it is not read).
_DEV_COMMAND_NAMED = re.compile(r"dev\.py ([a-z][a-z0-9-]*)")


# ---------------------------------------------------------------------------
# The guide's *An architecture repository* page
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def architecture_repository_page() -> str:
    assert _ARCHITECTURE_REPOSITORY_PAGE.is_file(), f"the page is missing: {_ARCHITECTURE_REPOSITORY_PAGE}"
    return _ARCHITECTURE_REPOSITORY_PAGE.read_text(encoding="utf-8")


class TestArchitectureRepositoryPage:
    """Every command the guide's *An architecture repository* page quotes is one this repository provides."""

    def test_every_quoted_command_is_one_this_repo_provides(self, architecture_repository_page: str) -> None:
        violations = [
            violation for line in command_lines(architecture_repository_page) if (violation := command_violation(line))
        ]
        assert not violations, "the page quotes commands this repository does not provide:\n" + "\n".join(
            f"  - {violation}" for violation in violations
        )

    def test_the_page_reaches_each_program_the_reading_judges(self, architecture_repository_page: str) -> None:
        """The build, the CLI and the installer are each quoted, so the guard is not vacuous."""
        programs = {_program(shlex.split(line)[0]) for line in command_lines(architecture_repository_page)}
        assert {"moon", "swcraft", "swcraft_install.sh"} <= programs, sorted(programs)

    def test_the_page_quotes_no_product_version_literal(self, architecture_repository_page: str) -> None:
        assert version_literals(architecture_repository_page) == []
