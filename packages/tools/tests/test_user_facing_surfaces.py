"""The roadmap stays inside the repository: no user-reaching surface names it.

A file, a help string or a tool description that reaches a SWCraft user must be usable without knowing how SWCraft
is built. Every offender is reported with ``file:line``. :data:`PHASE_PATTERN` matches ``Phase`` followed by a
number and :data:`PLAN_PATH_PATTERN` a path into the plans or checklists directory; both are deliberately narrow, so
the word "plan" in a sentence about planning passes. The scanned set is a constant, each entry with the reason it
reaches a user; four surfaces are not files: the CLI's help, the MCP tool descriptions, the model contract's
``description`` strings and the extension manifest's contributed strings.

**What this gate does not reach:**

* repository-internal prose (comments, docstrings, records, tests, the agent guide, ``README.md``);
* release notes older than :data:`OLDEST_SCANNED_RELEASE` that the guide does not carry;
* the showcase repository, the wheel's own metadata and the generated ``dist/`` payloads;
* the truth of the reworded prose;
* an overridden ``SWCRAFT_AGENT_SYSTEM_PROMPT`` (only the shipped prompt is read);
* a message a command prints at run time.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import build_artifacts_module
from swcraft_tools.cli.dispatch import build_parser

REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``Phase`` followed by a number — the roadmap's own vocabulary. ``\b`` on the
#: left so that a word ending in "phase" does not match, and ``\s+`` so that a
#: line wrap between the word and the number is caught.
PHASE_PATTERN = re.compile(r"\bPhase\s+\d")

#: A path into either roadmap directory. Both are repository-internal; neither is
#: shipped in any artifact, so a citation of one is a dead reference for every
#: reader of the surfaces below.
PLAN_PATH_PATTERN = re.compile(r"docs/(?:plans|checklists)/")

PATTERNS = (PHASE_PATTERN, PLAN_PATH_PATTERN)

#: The oldest release note this gate reads; earlier notes are published history.
OLDEST_SCANNED_RELEASE = (0, 9, 0)

#: Individual files that reach a user, each with the reason it does.
USER_FACING_FILES: tuple[tuple[str, str], ...] = (
    ("scripts/workstation/install.py", "shipped in the workstation archive and opened by its reader"),
    ("scripts/workstation/swcraft_install.cmd", "the archive's Windows entry point"),
    ("scripts/workstation/swcraft_install.sh", "the archive's macOS/Linux entry point"),
    ("scripts/workstation/README.md", "the archive's own README, unpacked beside the installer"),
    (
        "packages/agent/src/swcraft_agent/agent-system-prompt.md",
        "shipped in the agent wheel; the prompt a user may point at or edit",
    ),
    ("vscode_extension/README.md", "the extension's marketplace page"),
)

#: Directory trees every file of which reaches a user, with the reason.
USER_FACING_TREES: tuple[tuple[str, str, str], ...] = (
    (
        "packages/tools/src/swcraft_tools/templates",
        "*",
        "scaffolded verbatim into the user's project by `swcraft init`",
    ),
    ("docs/guide", "*.md", "rendered into the user guide site"),
)


def _release_note_version(path: Path) -> tuple[int, ...] | None:
    """``(0, 9, 0)`` for ``0.9.0.md``; ``None`` for a filename that is not one."""
    parts = path.stem.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def scanned_release_notes() -> tuple[Path, ...]:
    """Every release note at or above :data:`OLDEST_SCANNED_RELEASE`, and every older one the guide carries.

    Parsed from the filenames, so a new note is scanned with no edit here; the
    guide's half is the site build's own selection (``guide_release_notes``).
    """
    notes = REPO_ROOT / "docs" / "release-notes"
    selected = {
        path
        for path in notes.glob("*.md")
        if (version := _release_note_version(path)) is not None and version >= OLDEST_SCANNED_RELEASE
    }
    selected.update(build_artifacts_module().guide_release_notes(REPO_ROOT))
    return tuple(sorted(selected, key=lambda path: path.as_posix()))


def scanned_files() -> tuple[Path, ...]:
    """The whole file set, deduplicated and in one stable POSIX-sorted order."""
    paths: set[Path] = set()
    for relative, _reason in USER_FACING_FILES:
        candidate = REPO_ROOT / relative
        if candidate.exists():
            paths.add(candidate)
    for relative, glob, _reason in USER_FACING_TREES:
        for path in (REPO_ROOT / relative).rglob(glob):
            if path.is_file() and "__pycache__" not in path.parts:
                paths.add(path)
    paths.update(scanned_release_notes())
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def offenders_in_text(text: str, label: str) -> Iterator[str]:
    """Every match in *text*, as ``label:line — the matched text in its line``."""
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in PATTERNS:
            match = pattern.search(line)
            if match is not None:
                yield f"{label}:{number} — {line.strip()[:160]}"
                break


def _iter_strings(node: object, path: str) -> Iterator[tuple[str, str]]:
    """Every string in a JSON tree, with the dotted path that reaches it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _iter_subparsers(parser: object, name: str) -> Iterator[tuple[str, object]]:
    """*parser* and every subparser below it, with the command line that reaches it."""
    yield name, parser
    for action in getattr(parser, "_actions", ()):
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for command, subparser in choices.items():
                if hasattr(subparser, "format_help"):
                    yield from _iter_subparsers(subparser, f"{name} {command}")


class TestUserFacingFiles:
    """No file a user reads names the roadmap."""

    def test_the_file_set_is_not_empty_and_every_named_file_exists(self) -> None:
        """The scanned set is not empty and every named file exists, so the gate cannot go blind."""
        missing = [relative for relative, _reason in USER_FACING_FILES if not (REPO_ROOT / relative).exists()]
        assert missing == [], f"USER_FACING_FILES names files that do not exist: {missing}"
        assert len(scanned_files()) > 10, "the scanned set collapsed; the globs no longer match"

    def test_the_release_note_boundary_is_the_stated_one(self) -> None:
        """0.9.0 and later are scanned; 0.8.0 and earlier are history."""
        scanned = {path.stem for path in scanned_release_notes()}
        assert "0.9.0" in scanned
        assert "0.8.0" not in scanned
        assert "0.2.0" not in scanned

    def test_an_older_note_the_guide_carries_is_scanned(self) -> None:
        """0.6.0 reaches a user through the guide's Release notes section."""
        assert "0.6.0" in {path.stem for path in scanned_release_notes()}

    def test_the_boundary_is_the_guide_s_floor(self) -> None:
        """One boundary, stated twice: this gate's and the site build's."""
        assert build_artifacts_module().GUIDE_RELEASE_NOTES_FLOOR == OLDEST_SCANNED_RELEASE

    def test_no_user_facing_file_names_the_roadmap(self) -> None:
        offenders: list[str] = []
        for path in scanned_files():
            label = path.relative_to(REPO_ROOT).as_posix()
            offenders.extend(offenders_in_text(path.read_text(encoding="utf-8"), label))
        assert offenders == [], (
            "these user-reaching files name the development roadmap "
            "(a phase number, or a path into docs/plans/ or docs/checklists/); "
            "say what the thing is, not which phase built it:\n" + "\n".join(offenders)
        )


class TestUserFacingStrings:
    """No string a user is shown names the roadmap, read live rather than from a committed rendering."""

    def test_no_cli_help_text_names_the_roadmap(self) -> None:
        """``swcraft --help`` and every subcommand's, off the live parser."""
        offenders: list[str] = []
        for name, parser in _iter_subparsers(build_parser(), "swcraft"):
            offenders.extend(offenders_in_text(parser.format_help(), f"`{name} --help`"))
        assert offenders == [], "CLI help text names the roadmap:\n" + "\n".join(offenders)

    def test_no_mcp_tool_description_names_the_roadmap(self) -> None:
        """The live registration — the descriptions a client actually receives."""
        server = pytest.importorskip(
            "swcraft_mcp.server", reason="the MCP server package is not installed in this environment"
        )
        tools = asyncio.run(server.mcp.list_tools())
        assert tools, "the MCP registration is empty; the scan has gone blind"
        offenders: list[str] = []
        for tool in tools:
            offenders.extend(offenders_in_text(tool.description or "", f"mcp tool `{tool.name}`"))
        assert offenders == [], "MCP tool descriptions name the roadmap:\n" + "\n".join(offenders)

    def test_no_model_contract_description_names_the_roadmap(self) -> None:
        """Every ``description`` in the shipped contract: an editor hover and a guide page."""
        contract = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "model_contract" / "model-contract.json"
        document = json.loads(contract.read_text(encoding="utf-8"))
        offenders = [
            f"model-contract.json {path} — {value.strip()[:160]}"
            for path, value in _iter_strings(document, "$")
            if path.endswith("description") and any(pattern.search(value) for pattern in PATTERNS)
        ]
        assert offenders == [], (
            "the model contract's descriptions name the roadmap; changing one is a prose-only "
            "contract patch (a version bump plus a recorded decision), not an edit:\n" + "\n".join(offenders)
        )

    def test_no_extension_contribution_names_the_roadmap(self) -> None:
        """Command titles, setting descriptions and walkthrough text in the manifest."""
        manifest = REPO_ROOT / "vscode_extension" / "package.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        contributes = document.get("contributes", {})
        assert contributes, "package.json contributes nothing; the scan has gone blind"
        offenders = [
            f"package.json {path} — {value.strip()[:160]}"
            for path, value in _iter_strings(contributes, "contributes")
            if any(pattern.search(value) for pattern in PATTERNS)
        ]
        assert offenders == [], "the extension's contributed strings name the roadmap:\n" + "\n".join(offenders)


class TestTheScanItself:
    """The patterns catch what they are for, and abstain from what they are not."""

    @pytest.mark.parametrize(
        "line",
        [
            "This was added in Phase 17.",
            "the toolchain contract since Phase 18.1: one installation",
            "(Phase 24.1)",
            "tracked in `docs/checklists/20260706-windows-azure-pending-phases.md`",
            "see docs/plans/completed/20260906-phase23-compositions.md",
        ],
    )
    def test_the_scan_reports_a_roadmap_reference(self, line: str) -> None:
        assert list(offenders_in_text(line, "sample")) != [], f"the scan missed {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            "The build has three phases and a validation step.",
            "Planning is separated from doing.",
            "no shell string (D7; the rule dev.py states)",
            "see docs/concepts/arxml-writer.md and docs/adr/012-model-contract.md",
            "a phase 2 rollout",  # lowercase: prose, not the roadmap's own spelling
        ],
    )
    def test_the_scan_abstains_from_ordinary_prose(self, line: str) -> None:
        assert list(offenders_in_text(line, "sample")) == [], f"the scan over-matched {line!r}"
