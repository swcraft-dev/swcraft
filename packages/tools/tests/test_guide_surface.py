"""The guide's surface gates: ids, links, navigation, vocabulary, version, drafts, markers, How-to.

``test_guide_commands.py`` holds the quoted commands to the parsers; this module holds the rest:

1. **VS Code command ids** — every ``swcraft.<id>`` a page names is one ``package.json`` contributes.
2. **Links and navigation**, both directions: a relative link resolves inside ``docs/guide/`` (or
   against the generated ``release-notes/`` section), no page links into the repository host, every
   navigation row names a page and every page is navigated.
3. **Vocabulary** — no page uses :data:`guide_fixtures.FORBIDDEN_VOCABULARY`.
4. **Version** — no committed page carries this product version or pins any product version in an install line.
5. **Drafts** — a report, not a rule: which narrative pages still carry the draft marker.
6. **Authoring markers** — anchors and purpose comments on hand-written pages, every heading id of
   :data:`ANCHOR_RECORD` kept, no comment wrapped in a rendered paragraph.
7. **The How-to section** — :data:`HOW_TO_SHAPE`, the call blocks (a registered tool, a request
   ``protocol.schema.json`` accepts), no bench name ``examples/basic`` lacks.

Excluded: ``version.md`` (written by the site build, :data:`UNCOMMITTED_PAGES`) and the generated
*Release notes* section, read at its source by the vocabulary and bare-step gates, never by the version
gate. Every rule is a pure function over text, shown to fire on a mutated page.

**Not reached:** orphans have no second oracle (``mkdocs build --strict`` only logs them); whether
a link means its label; ``#section`` fragments (stripped); absolute URLs (never fetched); links
inside fences (the vocabulary, version and command-id gates do read fences); whether a VS Code
command works; jargon-free wrong prose; a version named without digits or a stale one in prose (a
bare ``\\d+.\\d+.\\d+`` would reject the AUTOSAR releases); whether a purpose is true or an anchor
apt; whether a How-to call is right (none is run; non-protocol argument values, bench allowances and
guide-question tasks are not read; the large bench's model is generated, not committed, so only the names
its task files carry are held); removed tool names (``test_removed_tool_names.py`` owns them).
Link case *is* reached: each segment is compared with the directory listing.
"""

from __future__ import annotations

import ast
import functools
import json
import posixpath
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import AGENT_BENCH, REPO_ROOT, build_artifacts_module
from guide_fixtures import (
    FORBIDDEN_VOCABULARY,
    GUIDE_DIR,
    MKDOCS_YML,
    ForbiddenTerm,
    committed_pages,
    heading_marker_problems,
    site_html,
    site_toc,
    vocabulary_hits,
)
from guide_fixtures import (
    nav_paths as nav_paths_of,
)
from jsonschema import Draft202012Validator
from referencing import Registry as SchemaRegistry
from referencing import Resource as SchemaResource
from swcraft_tools import __version__
from swcraft_tools.model_registry import current_registry

PACKAGE_JSON = REPO_ROOT / "vscode_extension" / "package.json"

#: The one page these gates do not read: written by the site build from ``swcraft_tools.__version__``,
#: gitignored and not navigated, since the guide is not a sixth lockstep site. Exactly one entry: another
#: name here would be a committed page with no gate over it.
UNCOMMITTED_PAGES = frozenset({"version.md"})

#: The generated *Release notes* section: ``scripts/build_artifacts.py``'s
#: ``write_guide_release_notes`` copies the notes the guide carries into it before
#: mkdocs runs. Gitignored, so its pages are read at their source, not on disk.
GENERATED_SECTION = (
    build_artifacts_module().GUIDE_RELEASE_NOTES_DIR.relative_to(build_artifacts_module().GUIDE_DIR).as_posix()
)

#: A link into a repository host's file tree, which no reader of the guide can open.
_REPOSITORY_LINK = re.compile(r"^https?://[^\s)]*/(?:src|raw|blob|tree)/branch/")

#: The prefix of the generated reference tree. Used only to say which pages the
#: draft report expects a marker on; the gates themselves read every page.
GENERATED_PREFIX = "reference/"

#: The marker a narrative page carries until the product's author has read it.
DRAFT_MARKER = "<!-- draft: not yet reviewed by the product's author -->"

#: Anything shaped like a draft marker, so a page cannot fall out of the report
#: by being reworded rather than reviewed.
_DRAFT_COMMENT = re.compile(r"<!--\s*draft\b[^>]*-->", re.IGNORECASE)

#: ``swcraft.<something>`` spellings that are not VS Code command ids, each with its reason; every entry
#: is a string the command-id gate stops checking.
NON_COMMAND_IDENTIFIERS: dict[str, str] = {
    "swcraft.yaml": "the project manifest's filename, which every command line on every page names",
    "swcraft.exe": (
        "the installed executable's Windows filename — the pages that say where the workstation "
        "installation is have to spell it, and it is a file rather than a contributed command"
    ),
    "swcraft.json": (
        "the Copilot model guard's hook file, `.github/hooks/swcraft.json` — the pages that tell a "
        "user what the guard is and how a project gets it have to name the file, and it is a file "
        "rather than a contributed command"
    ),
    "swcraft.md": (
        "the tail of `.kiro/steering/swcraft.md`, SWCraft's instructions file for Kiro, which the *In "
        "Kiro* page and the pages pointing to it name — the pattern's word boundary starts a match after "
        "the slash; it is a file Onboard Project writes, not a contributed command"
    ),
    "swcraft.dev": (
        "the host of the requirements interchange and model passport schemas' `$id`, which the two "
        "generated schema pages carry byte for byte inside their fences — an identifier URI, not a "
        "contributed command"
    ),
    "swcraft.txt": (
        "the tail of `requirements-swcraft.txt`, the per-project requirements file *Upgrading* tells a "
        "project from before 0.7.0 to delete — the pattern's word boundary starts a match after its "
        "hyphen; it is a file a removed layout held, not a contributed command"
    ),
    "swcraft.env.SWCRAFT_REVIEW": (
        "the tail of `mcp_servers.swcraft.env.SWCRAFT_REVIEW`, the dotted key of Codex's `-c` "
        "override that *Codex and Claude Code in VS Code* gives for reviewing a terminal session "
        "elsewhere — `swcraft` is the server's name in Codex's configuration, and the pattern's word "
        "boundary starts a match after the dot; it is a configuration key, not a contributed command"
    ),
}

#: Dotted segments: a contributed setting can carry more than one dot (``swcraft.experiments.allocation.config``).
_COMMAND_ID = re.compile(r"\bswcraft\.[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")

#: ``[label](target)`` and ``![alt](target)``, with an optional quoted title.
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")

#: A development step's number standing alone (``24.1``), which the shared
#: vocabulary's ``Phase <n>`` does not match; read on the carried notes only.
_BARE_STEP_NUMBER = re.compile(r"(?<![\w.])[1-3]\d\.\d(?![\w.])")

#: A reference to a release note names a version by definition: this shape alone is removed from a page
#: before the version gate reads it.
_RELEASE_NOTE_REFERENCE = re.compile(r"release-notes/\d+\.\d+\.\d+(?:\.md)?")

#: A version in a package pin or the extension's packaged filename, whatever the number: a page pinning a
#: past release is a finding. Not a bare ``\d+\.\d+\.\d+``: the guide names AUTOSAR 4.2.2 and 4.3.0.
_PINNED_VERSION = re.compile(r"swcraft-(?:tools|mcp|agent)==\d+\.\d+\.\d+|swcraft-vscode-\d+\.\d+\.\d+\.vsix")

_ABSOLUTE_SCHEMES = ("http://", "https://", "mailto:")

#: Every heading id each hand-written page carried before its anchors were explicit, as python-markdown
#: read them under MkDocs' effective configuration. A fixture, since a clone need not hold that history.
ANCHOR_RECORD = Path(__file__).parent / "fixtures" / "guide_corpus" / "hand-written-anchors.json"

#: A rendered paragraph, its content captured.
_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)


# Reading the tree


def guide_pages() -> dict[str, Path]:
    """Every committed guide page, keyed by its path relative to ``docs/guide/``."""
    assert GUIDE_DIR.is_dir(), f"{GUIDE_DIR} is missing"
    return {
        relative: path
        for relative, path in committed_pages(pattern="*.md").items()
        if relative not in UNCOMMITTED_PAGES and not relative.startswith(f"{GENERATED_SECTION}/")
    }


def generated_section_pages() -> dict[str, Path]:
    """The pages the site build will write into the *Release notes* section, keyed as a link names them.

    Each value is the page's source under ``docs/release-notes/`` (the index maps to the section's own path).
    Derived from the build script's ``guide_release_notes``, so the gate and the build cannot disagree.
    """
    artifacts = build_artifacts_module()
    pages = {
        f"{GENERATED_SECTION}/{artifacts.GUIDE_RELEASE_NOTES_INDEX}": REPO_ROOT / artifacts.GUIDE_RELEASE_NOTES_DIR
    }
    pages.update({f"{GENERATED_SECTION}/{note.name}": note for note in artifacts.guide_release_notes(REPO_ROOT)})
    return pages


def narrative_pages() -> dict[str, Path]:
    """The hand-written pages: everything outside the generated reference tree."""
    return {name: path for name, path in guide_pages().items() if not name.startswith(GENERATED_PREFIX)}


def contributed_command_ids() -> frozenset[str]:
    """Every ``swcraft.*`` id ``package.json`` contributes: the palette commands and the settings.

    A renamed or deleted setting must turn a page red the same way a command does.
    """
    manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    commands = {entry["command"] for entry in manifest["contributes"]["commands"]}
    settings = set(manifest["contributes"].get("configuration", {}).get("properties", {}))
    return frozenset(commands | settings)


def nav_paths() -> list[str]:
    """Every document path the committed navigation names, depth-first, in order."""
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    return nav_paths_of(config["nav"])


def without_fenced_blocks(text: str) -> str:
    """*text* with every fenced code block removed, blank lines left in their place."""
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            kept.append("")
            continue
        kept.append("" if fenced else line)
    return "\n".join(kept)


# The rules, each a pure function over text


def command_id_violations(text: str) -> list[str]:
    """Every ``swcraft.*`` id *text* names that the extension does not contribute, fences included."""
    contributed = contributed_command_ids()
    unknown: list[str] = []
    for identifier in dict.fromkeys(_COMMAND_ID.findall(text)):
        if identifier in NON_COMMAND_IDENTIFIERS or identifier in contributed:
            continue
        unknown.append(f"{identifier}: not a command `{PACKAGE_JSON.name}` contributes")
    return unknown


def link_targets(text: str) -> list[str]:
    """Every link target *text* carries outside a fenced code block, in order."""
    return _LINK.findall(without_fenced_blocks(text))


def _exists_case_sensitively(inside: str) -> bool:
    """Whether ``docs/guide/<inside>`` exists, matching each segment's case exactly.

    ``Path.exists()`` answers on a case-insensitive volume what the Linux host serving the site 404s, so
    each segment is compared with the directory listing.
    """
    current = GUIDE_DIR.resolve()
    for segment in inside.split("/"):
        try:
            names = {child.name for child in current.iterdir()}
        except (NotADirectoryError, FileNotFoundError):
            return False
        if segment not in names:
            return False
        current = current / segment
    return True


def link_violations(relative: str, text: str) -> list[str]:
    """Every relative link in *text* that does not resolve inside ``docs/guide/``.

    *relative* is the page's own path under ``docs/guide/``; absolute URLs and bare fragments are passed over.
    """
    violations: list[str] = []
    for target in link_targets(text):
        if target.startswith(_ABSOLUTE_SCHEMES) or target.startswith("#"):
            continue
        document = target.split("#", 1)[0]
        if not document:
            continue
        # Composed as text, never resolved: ``Path.resolve()`` canonicalizes the on-disk case on Windows,
        # leaving the case check nothing to find. ``posixpath``, not ``os.path`` (backslashes on Windows).
        # The outside-the-tree question is textual too, as ``mkdocs build --strict`` asks it.
        inside = posixpath.normpath(posixpath.join(posixpath.dirname(relative), document))
        if posixpath.isabs(inside) or inside == ".." or inside.startswith("../"):
            violations.append(
                f"{target}: points outside docs/guide/, which `mkdocs build --strict` refuses — "
                "a release note is linked as release-notes/<version>.md, which the site build "
                "copies in, and the rest of the repository is not something a reader of the "
                "guide can open"
            )
            continue
        if inside.startswith(f"{GENERATED_SECTION}/"):
            if inside not in generated_section_pages():
                violations.append(
                    f"{target}: resolves to docs/guide/{inside}, which the site build does not "
                    "write — no release note of that name, or one the guide does not carry"
                )
            continue
        if not _exists_case_sensitively(inside):
            violations.append(f"{target}: resolves to docs/guide/{inside}, which does not exist")
    return violations


def missing_nav_pages() -> list[str]:
    """Committed pages no navigation row reaches — the direction ``--strict`` does not check."""
    return sorted(set(guide_pages()) - set(nav_paths()))


def unbacked_nav_rows() -> list[str]:
    """Navigation rows naming a page that is not on disk."""
    return sorted(path for path in nav_paths() if path not in guide_pages() and path not in generated_section_pages())


def repository_links(text: str) -> list[str]:
    """Every link *text* carries into a repository host's file tree."""
    return [target for target in link_targets(text) if _REPOSITORY_LINK.match(target)]


def vocabulary_violations(text: str) -> list[str]:
    """The engineering vocabulary *text* contains, read from the one shared list."""
    return vocabulary_hits(text)


def version_violations(text: str) -> list[str]:
    """Every place *text* pins a product version outside an allowlisted release-note reference.

    The current version anywhere, and any version in a ``swcraft-*==`` pin or a ``swcraft-vscode-<v>.vsix``
    filename (a pin of an old release passes the first rule forever). Not reached: a stale product version in
    prose, since a bare ``\\d+.\\d+.\\d+`` would reject the AUTOSAR releases the guide names.
    """
    scanned = _RELEASE_NOTE_REFERENCE.sub("release-notes/<version>", text)
    violations = [
        f"carries the product version {__version__!r}: {line.strip()}"
        for line in scanned.splitlines()
        if __version__ in line
    ]
    violations += [
        f"pins a product version in an install line: {match.strip()}"
        for match in _PINNED_VERSION.findall(scanned)
        if __version__ not in match
    ]
    return violations


def draft_pages() -> list[str]:
    """The narrative pages still carrying a draft marker, in navigation order where possible."""
    pages = narrative_pages()
    ordered = [path for path in nav_paths() if path in pages]
    ordered += [name for name in pages if name not in set(ordered)]
    return [name for name in ordered if _DRAFT_COMMENT.search(pages[name].read_text(encoding="utf-8"))]


def recorded_anchors() -> dict[str, list[str]]:
    """:data:`ANCHOR_RECORD`'s pages: each hand-written page's headings as ``"<hashes> <id>"``."""
    return dict(json.loads(ANCHOR_RECORD.read_text(encoding="utf-8"))["pages"])


def moved_anchors(text: str, recorded: list[str]) -> list[str]:
    """Each recorded ``"<hashes> <id>"`` heading that *text* no longer carries at that level."""
    present = {f"{'#' * level} {ident}" for level, ident in site_toc(text)}
    return [heading for heading in recorded if heading not in present]


def comment_paragraphs(text: str) -> list[str]:
    """Each rendered paragraph of *text* that holds an HTML comment or nothing at all.

    A purpose with no blank line before its text renders inside a ``<p>``; one alone in it, an empty one.
    """
    return [
        match.group(0)
        for match in _PARAGRAPH.finditer(site_html(text))
        if "<!--" in match.group(1) or not match.group(1).strip()
    ]


# Fixtures


def _page(tmp_path: Path, body: str, name: str = "mutated.md") -> str:
    """Write *body* as a guide page and read it back, so a gate reads a file."""
    page = tmp_path / name
    page.write_text(body, encoding="utf-8")
    return page.read_text(encoding="utf-8")


_PAGE_IDS = pytest.mark.parametrize("relative", sorted(guide_pages()))


# The exclusion


def test_the_exclusion_set_is_exactly_the_build_generated_page() -> None:
    """One name, and the reason it is a name: every other page here is committed and gated."""
    assert UNCOMMITTED_PAGES == frozenset({"version.md"})


def test_the_excluded_page_is_gitignored() -> None:
    """The exclusion is only correct while the page really is uncommitted."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/docs/guide/version.md" in ignored


def test_the_excluded_page_is_absent_from_the_navigation() -> None:
    assert not (set(nav_paths()) & UNCOMMITTED_PAGES)


def test_the_generated_section_is_the_one_the_build_writes_and_is_gitignored() -> None:
    assert GENERATED_SECTION == "release-notes"
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert f"/docs/guide/{GENERATED_SECTION}/" in ignored


def test_the_gates_read_a_non_empty_corpus() -> None:
    """A gate over nothing passes vacuously; pin that there is something to read."""
    assert guide_pages()
    assert narrative_pages()


# Gate 2 — the VS Code command ids


class TestCommandIds:
    def test_the_contributed_surface_is_read_from_the_manifest(self) -> None:
        contributed = contributed_command_ids()
        assert contributed, f"no command ids in {PACKAGE_JSON}"
        assert all(identifier.startswith("swcraft.") for identifier in contributed)

    @_PAGE_IDS
    def test_every_command_id_a_page_names_is_contributed(self, relative: str) -> None:
        violations = command_id_violations(guide_pages()[relative].read_text(encoding="utf-8"))
        assert not violations, f"docs/guide/{relative}:\n" + "\n".join(f"  - {v}" for v in violations)

    def test_a_page_naming_an_id_the_extension_does_not_have_fails(self, tmp_path: Path) -> None:
        body = "Run **SWCraft: Validate** (`swcraft.validateEverything`) from the palette.\n"
        violations = command_id_violations(_page(tmp_path, body))
        assert len(violations) == 1 and "swcraft.validateEverything" in violations[0], violations

    def test_a_page_naming_a_contributed_id_passes(self, tmp_path: Path) -> None:
        identifier = sorted(contributed_command_ids())[0]
        assert command_id_violations(_page(tmp_path, f"Run `{identifier}`.\n")) == []

    def test_a_contributed_setting_passes_and_an_invented_one_does_not(self, tmp_path: Path) -> None:
        """The settings half of the surface, both directions."""
        assert command_id_violations(_page(tmp_path, "Set `swcraft.executable`.\n")) == []
        violations = command_id_violations(_page(tmp_path, "Set `swcraft.executablePath`.\n"))
        assert len(violations) == 1 and "swcraft.executablePath" in violations[0], violations

    def test_a_dotted_setting_is_read_whole(self, tmp_path: Path) -> None:
        """A setting with dotted segments is checked as one spelling, both directions."""
        assert command_id_violations(_page(tmp_path, "Set `swcraft.experiments.allocation.config`.\n")) == []
        violations = command_id_violations(_page(tmp_path, "Set `swcraft.experiments.nothing.config`.\n"))
        assert len(violations) == 1 and "swcraft.experiments.nothing.config" in violations[0], violations
        assert command_id_violations(_page(tmp_path, "Open `swcraft.yaml`.\n")) == []

    def test_the_manifest_filename_is_not_read_as_an_id(self, tmp_path: Path) -> None:
        """Every command line on every page names it; it is admitted by name, with a reason."""
        body = "```sh\nswcraft validate --project swcraft.yaml\n```\n"
        assert command_id_violations(_page(tmp_path, body)) == []

    def test_every_admitted_non_command_states_why(self) -> None:
        for spelling, reason in NON_COMMAND_IDENTIFIERS.items():
            assert reason.strip(), f"{spelling} is admitted with no reason"
            assert spelling not in contributed_command_ids(), f"{spelling} IS a contributed id"


# Gate 3 — links and navigation


class TestLinks:
    @_PAGE_IDS
    def test_every_relative_link_resolves_inside_the_guide(self, relative: str) -> None:
        violations = link_violations(relative, guide_pages()[relative].read_text(encoding="utf-8"))
        assert not violations, f"docs/guide/{relative}:\n" + "\n".join(f"  - {v}" for v in violations)

    def test_a_dangling_link_fails(self, tmp_path: Path) -> None:
        body = "See [the model](model/no-such-layer.md).\n"
        violations = link_violations("reference/index.md", _page(tmp_path, body))
        assert len(violations) == 1 and "does not exist" in violations[0], violations

    def test_a_relative_link_out_of_the_tree_fails(self, tmp_path: Path) -> None:
        """The rule that keeps this gate and ``mkdocs build --strict`` agreeing."""
        body = "See [the writer](../concepts/arxml-writer.md).\n"
        violations = link_violations("index.md", _page(tmp_path, body))
        assert len(violations) == 1 and "points outside docs/guide/" in violations[0], violations

    def test_an_absolute_repository_url_passes(self, tmp_path: Path) -> None:
        body = "See [the release notes](https://example.invalid/swcraft/docs/release-notes/0.4.0.md).\n"
        assert link_violations("upgrading.md", _page(tmp_path, body)) == []

    def test_a_link_that_resolves_passes(self, tmp_path: Path) -> None:
        body = "See [the layers](reference/model/index.md) and [SWCs](reference/model/swc.md#fields).\n"
        assert link_violations("index.md", _page(tmp_path, body)) == []

    def test_a_bare_fragment_passes(self, tmp_path: Path) -> None:
        assert link_violations("reference/cli.md", _page(tmp_path, "[init](#swcraft-init)\n")) == []

    def test_a_link_whose_case_is_wrong_fails(self) -> None:
        """The finding a case-insensitive `Path.exists()` misses, read against the real tree."""
        violations = link_violations("index.md", "See [the layers](reference/Model/index.md).\n")
        assert len(violations) == 1 and "does not exist" in violations[0], violations

    def test_a_link_whose_file_segment_case_is_wrong_fails(self) -> None:
        """The file segment's case: an implementation checking directories only cannot pass both cases."""
        violations = link_violations("index.md", "See [the layers](reference/model/Index.md).\n")
        assert len(violations) == 1 and "does not exist" in violations[0], violations

    def test_the_same_link_in_the_committed_case_passes(self) -> None:
        """Without which the case above would pass an implementation that rejects everything."""
        assert link_violations("index.md", "See [the layers](reference/model/index.md).\n") == []

    def test_a_link_inside_a_code_fence_is_not_read(self, tmp_path: Path) -> None:
        """A limitation of the gate, pinned so it stays one."""
        body = "```md\n[an example](nowhere.md)\n```\n"
        assert link_violations("index.md", _page(tmp_path, body)) == []
        assert link_targets(body) == []


class TestReleaseNotesSection:
    """The release notes travel inside the guide, and no page links the repository for them."""

    def test_the_navigation_names_the_section_index(self) -> None:
        assert f"{GENERATED_SECTION}/index.md" in nav_paths()

    def test_the_section_carries_every_note_from_the_floor_on_and_every_older_one_a_page_links(self) -> None:
        artifacts = build_artifacts_module()
        carried = {Path(name).stem for name in generated_section_pages()} - {"index"}
        for note in (REPO_ROOT / artifacts.RELEASE_NOTES_DIR).glob("*.md"):
            version = artifacts.release_note_version(note)
            if version is not None and version >= artifacts.GUIDE_RELEASE_NOTES_FLOOR:
                assert note.stem in carried, f"{note.name} is not carried by the guide"
        assert "0.6.0" in carried, "Upgrading links the hardware release's note"

    @pytest.mark.parametrize(
        "relative", sorted(name for name in generated_section_pages() if not name.endswith("/index.md"))
    )
    def test_no_carried_note_uses_the_engineering_vocabulary(self, relative: str) -> None:
        """Read at the source, since the section itself exists only after a build."""
        source = generated_section_pages()[relative]
        hits = vocabulary_violations(source.read_text(encoding="utf-8"))
        assert not hits, f"{source.relative_to(REPO_ROOT).as_posix()} reaches the guide and uses {hits}"

    @pytest.mark.parametrize(
        "relative", sorted(name for name in generated_section_pages() if not name.endswith("/index.md"))
    )
    def test_no_carried_note_names_a_bare_step_number_or_the_roadmap(self, relative: str) -> None:
        """The shared list matches ``Phase <n>`` only; a carried note also said ``20.1's tree``.

        **What it does not reach**: a single-digit step (``9.1``), a step spelled in words; and it would refuse
        any figure from 10.0 to 39.9 written with one decimal, which a note must reword.
        """
        source = generated_section_pages()[relative]
        text = source.read_text(encoding="utf-8")
        hits = [match.group(0) for match in _BARE_STEP_NUMBER.finditer(text)]
        hits += [match.group(0) for match in re.finditer(r"\broadmap\b", text, re.IGNORECASE)]
        assert not hits, f"{source.relative_to(REPO_ROOT).as_posix()} reaches the guide and names {hits}"

    def test_the_bare_step_pattern_passes_versions_and_refuses_a_step(self) -> None:
        """Without which the case above would pass a pattern that matches nothing."""
        assert _BARE_STEP_NUMBER.search("closed by 24.2 in this release")
        assert _BARE_STEP_NUMBER.search("20.1's tree")
        for text in ("0.10.0", "contract 4.2.2", "SWCraft 0.13.0", "128.5 bytes", "a 3.2 s run"):
            assert not _BARE_STEP_NUMBER.search(text), text

    def test_the_generated_index_uses_no_engineering_vocabulary(self) -> None:
        index = build_artifacts_module().render_guide_release_notes_index(["0.13.0", "0.6.0"])
        assert vocabulary_violations(index) == []

    def test_upgrading_links_the_notes_relatively(self) -> None:
        targets = link_targets((GUIDE_DIR / "upgrading.md").read_text(encoding="utf-8"))
        assert f"{GENERATED_SECTION}/index.md" in targets
        assert f"{GENERATED_SECTION}/0.6.0.md" in targets

    @_PAGE_IDS
    def test_no_page_links_into_the_repository_host(self, relative: str) -> None:
        links = repository_links(guide_pages()[relative].read_text(encoding="utf-8"))
        assert links == [], f"docs/guide/{relative} links into the repository, which its reader cannot open: {links}"

    def test_a_link_into_the_repository_host_is_found(self, tmp_path: Path) -> None:
        body = "See [the note](https://git.example.invalid/owner/repo/src/branch/main/docs/release-notes/0.6.0.md).\n"
        assert len(repository_links(_page(tmp_path, body))) == 1

    def test_a_link_to_a_carried_note_resolves_without_a_build(self, tmp_path: Path) -> None:
        assert link_violations("upgrading.md", _page(tmp_path, "[x](release-notes/0.12.0.md)\n")) == []
        assert link_violations("reference/rules.md", _page(tmp_path, "[x](../release-notes/index.md)\n")) == []

    def test_a_link_to_a_note_the_guide_does_not_carry_fails(self, tmp_path: Path) -> None:
        """0.5.0 exists in the repository but is below the floor and linked by no committed page."""
        violations = link_violations("upgrading.md", _page(tmp_path, "[x](release-notes/0.5.0.md)\n"))
        assert len(violations) == 1 and "does not write" in violations[0], violations

    def test_a_link_to_a_note_that_does_not_exist_fails(self, tmp_path: Path) -> None:
        violations = link_violations("upgrading.md", _page(tmp_path, "[x](release-notes/9.9.9.md)\n"))
        assert len(violations) == 1 and "does not write" in violations[0], violations


class TestNavigation:
    def test_every_navigation_row_names_a_page_that_exists(self) -> None:
        unbacked = unbacked_nav_rows()
        assert not unbacked, f"{MKDOCS_YML.name} navigates pages that are not on disk: {unbacked}"

    def test_every_committed_page_is_reachable_from_the_navigation(self) -> None:
        """The orphan direction, which ``mkdocs build --strict`` logs at INFO and passes."""
        orphans = missing_nav_pages()
        assert not orphans, (
            f"guide pages no {MKDOCS_YML.name} row reaches: {orphans} — add a nav row, or delete the page"
        )

    def test_an_orphan_page_is_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate function itself, not a second copy of its expression, which passes an emptied gate."""
        added = "reference/model/an_added_layer.md"
        perturbed = {**guide_pages(), added: GUIDE_DIR / added}
        monkeypatch.setattr(f"{__name__}.guide_pages", lambda: perturbed)
        assert missing_nav_pages() == [added]

    def test_a_navigation_row_with_no_page_is_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        perturbed = [*nav_paths(), "invented.md"]
        monkeypatch.setattr(f"{__name__}.nav_paths", lambda: perturbed)
        assert unbacked_nav_rows() == ["invented.md"]

    def test_the_navigation_names_every_narrative_page(self) -> None:
        """Stated separately from the sweep above: the hand-written pages are the guide."""
        assert set(narrative_pages()) <= set(nav_paths())


# Gate 4 — the vocabulary


class TestVocabulary:
    def test_the_list_is_the_shared_one(self) -> None:
        """Imported, never re-authored: two copies would be two lists a week later."""
        assert FORBIDDEN_VOCABULARY, "the shared vocabulary list is empty"

    @_PAGE_IDS
    def test_no_page_uses_the_engineering_vocabulary(self, relative: str) -> None:
        hits = vocabulary_violations(guide_pages()[relative].read_text(encoding="utf-8"))
        assert not hits, f"docs/guide/{relative} uses the engineering record's vocabulary: {hits}"

    def test_the_generated_pages_are_scanned_too(self) -> None:
        """Which is what makes the rendered `description`s dual-audience by rule, not by luck."""
        generated = [name for name in guide_pages() if name.startswith(GENERATED_PREFIX)]
        assert generated, "no generated page under docs/guide/reference/"
        for name in generated:
            assert vocabulary_violations(guide_pages()[name].read_text(encoding="utf-8")) == []

    @pytest.mark.parametrize("term", FORBIDDEN_VOCABULARY, ids=lambda term: term.label)
    def test_every_term_is_found_when_a_page_uses_it(self, term: ForbiddenTerm, tmp_path: Path) -> None:
        """One negative fixture per entry, so no term is in the list without being checked."""
        sample = {
            "Phase <n>": "Added in Phase 25.",
            "ADR <n>": "The reasoning is in ADR 011.",
            "total consumer": "The page is a total consumer of the layers.",
            "model contract": "Every layer is declared in the model contract.",
            "binding.json": "The bound names live in binding.json.",
            "docs/concepts/": "See docs/concepts/model-contract.md.",
            "registry": "The registry resolves the layer.",
        }[term.label]
        assert vocabulary_violations(_page(tmp_path, f"# A page\n\n{sample}\n")) == [term.label]

    def test_the_bare_word_contract_is_allowed(self, tmp_path: Path) -> None:
        """An interface *is* a contract, and forbidding the word rejects correct prose."""
        body = "An interface is the contract between two components.\n"
        assert vocabulary_violations(_page(tmp_path, body)) == []


# Gate 5 — the version


class TestVersion:
    @_PAGE_IDS
    def test_no_committed_page_carries_the_product_version(self, relative: str) -> None:
        violations = version_violations(guide_pages()[relative].read_text(encoding="utf-8"))
        assert not violations, (
            f"docs/guide/{relative} would make the guide a sixth version-lockstep site:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_a_page_pinning_the_version_fails(self, tmp_path: Path) -> None:
        body = f"```sh\npip install swcraft-tools=={__version__}\n```\n"
        violations = version_violations(_page(tmp_path, body))
        assert len(violations) == 1 and __version__ in violations[0], violations

    def test_a_release_note_reference_is_allowlisted(self, tmp_path: Path) -> None:
        body = f"See [what changed](https://example.invalid/swcraft/docs/release-notes/{__version__}.md).\n"
        assert version_violations(_page(tmp_path, body)) == []

    def test_the_allowlist_does_not_cover_the_rest_of_the_line(self, tmp_path: Path) -> None:
        """A release-note link is admitted; the sentence around it is still read."""
        body = f"Release {__version__} — see [the notes](docs/release-notes/{__version__}.md).\n"
        assert version_violations(_page(tmp_path, body)) != []

    def test_a_page_pinning_a_version_this_release_has_moved_past_fails(self, tmp_path: Path) -> None:
        """The rule the current-version check cannot carry: a pin of *any* version is a finding."""
        body = "```sh\npip install swcraft-tools==0.3.1 swcraft-mcp==0.3.1\n```\n"
        violations = version_violations(_page(tmp_path, body))
        assert len(violations) == 2 and all("pins a product version" in v for v in violations), violations

    def test_a_stale_vsix_filename_fails(self, tmp_path: Path) -> None:
        assert version_violations(_page(tmp_path, "Install `swcraft-vscode-0.3.1.vsix`.\n")) != []

    def test_an_autosar_release_is_not_a_product_version(self, tmp_path: Path) -> None:
        """Named as a limit and checked as one: the guide quotes 4.2.2 and 4.3.0 on purpose."""
        body = "The AUTOSAR release defaults to 4.2.2; `--arxml-version 4.3.0` overrides it.\n"
        assert version_violations(_page(tmp_path, body)) == []

    def test_a_placeholder_pin_passes(self, tmp_path: Path) -> None:
        """Which is the form the guide's own install lines use."""
        assert version_violations(_page(tmp_path, "pip install swcraft-tools==<version>\n")) == []

    def test_a_version_without_digits_passes(self, tmp_path: Path) -> None:
        """Named as a limitation: "the current release" is exactly how a page should say it."""
        assert version_violations(_page(tmp_path, "Install the current release.\n")) == []

    def test_the_excluded_page_is_excluded_because_it_would_fail(self) -> None:
        """The excluded page's whole purpose, which is why it is excluded rather than rewritten.

        Its text comes from the builder's renderer: on disk it is present only after a site build.
        """
        rendered = build_artifacts_module().render_guide_version_page(__version__)
        assert version_violations(rendered) != []


# The authoring markers

_NARRATIVE_IDS = pytest.mark.parametrize("relative", sorted(narrative_pages()))

_GOOD_PAGE = (
    "# A page\n\n<!-- purpose: When a reader needs a page -->\n\nLead.\n\n"
    "## A section { #a-section }\n\n<!-- purpose: When a reader needs a section -->\n\nBody.\n"
)


class TestAuthoringMarkers:
    """Explicit anchors and purposes on the hand-written pages, and no URL moved."""

    @_NARRATIVE_IDS
    def test_every_heading_carries_its_anchor_and_purpose(self, relative: str) -> None:
        problems = heading_marker_problems(narrative_pages()[relative].read_text(encoding="utf-8"))
        assert problems == [], f"docs/guide/{relative}: " + "; ".join(problems)

    def test_the_record_names_every_hand_written_page_it_was_taken_over(self) -> None:
        """Each page of the record is still a narrative page."""
        recorded = recorded_anchors()
        assert len(recorded) == 13
        assert set(recorded) <= set(narrative_pages()), set(recorded) - set(narrative_pages())

    @pytest.mark.parametrize("relative", sorted(recorded_anchors()))
    def test_no_recorded_anchor_moved(self, relative: str) -> None:
        text = narrative_pages()[relative].read_text(encoding="utf-8")
        moved = moved_anchors(text, recorded_anchors()[relative])
        assert moved == [], f"docs/guide/{relative} no longer carries {moved}: a site URL would move"

    @_PAGE_IDS
    def test_no_rendered_page_wraps_a_comment_in_a_paragraph(self, relative: str) -> None:
        found = comment_paragraphs(guide_pages()[relative].read_text(encoding="utf-8"))
        assert found == [], f"docs/guide/{relative}: {found}"

    def test_a_well_marked_page_passes(self, tmp_path: Path) -> None:
        text = _page(tmp_path, _GOOD_PAGE)
        assert heading_marker_problems(text) == []
        assert comment_paragraphs(text) == []
        assert moved_anchors(text, ["# a-page", "## a-section"]) == []

    def test_a_section_without_an_anchor_fails(self, tmp_path: Path) -> None:
        problems = heading_marker_problems(_page(tmp_path, _GOOD_PAGE.replace(" { #a-section }", "")))
        assert len(problems) == 1 and "no explicit anchor" in problems[0]

    def test_a_heading_without_a_purpose_fails(self, tmp_path: Path) -> None:
        text = _page(tmp_path, _GOOD_PAGE.replace("<!-- purpose: When a reader needs a page -->\n\n", ""))
        problems = heading_marker_problems(text)
        assert len(problems) == 1 and "no purpose comment" in problems[0]

    def test_a_purpose_over_the_cap_fails(self, tmp_path: Path) -> None:
        problems = heading_marker_problems(_page(tmp_path, _GOOD_PAGE.replace("a section -->", "x" * 101 + " -->")))
        assert len(problems) == 1 and "over" in problems[0]

    def test_a_purpose_sharing_its_block_fails_both_gates(self, tmp_path: Path) -> None:
        text = _page(tmp_path, _GOOD_PAGE.replace("a section -->\n\nBody.", "a section -->\nBody."))
        assert "no purpose comment" in heading_marker_problems(text)[0]
        text = _page(tmp_path, _GOOD_PAGE.replace("Body.", "Body. <!-- purpose: inside a paragraph -->"))
        assert comment_paragraphs(text) != []

    def test_a_reworded_anchor_is_found_as_a_moved_url(self, tmp_path: Path) -> None:
        text = _page(tmp_path, _GOOD_PAGE.replace("{ #a-section }", "{ #another-section }"))
        assert heading_marker_problems(text) == []
        assert moved_anchors(text, ["# a-page", "## a-section"]) == ["## a-section"]

    def test_a_reworded_heading_keeps_its_url(self, tmp_path: Path) -> None:
        """The anchor is what the URL rests on, so a heading's new words move nothing."""
        text = _page(tmp_path, _GOOD_PAGE.replace("## A section", "## A section, reworded"))
        assert moved_anchors(text, ["# a-page", "## a-section"]) == []


# The draft report


class TestDraftReport:
    """Which narrative pages still await the product's author — reported, never required.

    Nothing asserts how many pages carry a marker; what is asserted is that the report cannot go quietly
    wrong: one spelling, at the top of a page, on no generated page.
    """

    def test_the_report_accounts_for_every_narrative_page(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Printed, and asserted to partition the corpus: every narrative page a draft or reviewed, never both."""
        pages = narrative_pages()
        drafts = draft_pages()
        remaining = sorted(set(pages) - set(drafts))
        with capsys.disabled():
            print(f"\ndraft report — {len(drafts)} of {len(pages)} narrative pages await a reading")
            for name in drafts:
                print(f"  draft    docs/guide/{name}")
            for name in remaining:
                print(f"  reviewed docs/guide/{name}")

        assert len(drafts) == len(set(drafts)), f"a page is reported twice: {drafts}"
        assert set(drafts) <= set(pages), (
            f"the report names pages that are not narrative pages: {set(drafts) - set(pages)}"
        )
        assert set(drafts) | set(remaining) == set(pages)

    def test_the_two_spellings_of_the_marker_agree(self) -> None:
        """`DRAFT_MARKER` (what a page carries) and `_DRAFT_COMMENT` (what the report finds) agree."""
        assert _DRAFT_COMMENT.fullmatch(DRAFT_MARKER), (
            f"{DRAFT_MARKER!r} does not match the pattern the report searches with"
        )

    def test_every_marker_has_the_one_spelling(self) -> None:
        """A reworded marker would drop a page out of the report without anybody editing it."""
        for name, path in narrative_pages().items():
            text = path.read_text(encoding="utf-8")
            for found in _DRAFT_COMMENT.findall(text):
                assert found == DRAFT_MARKER, f"docs/guide/{name} carries {found!r}, not {DRAFT_MARKER!r}"

    def test_a_marker_stands_at_the_top_of_its_page(self) -> None:
        for name in draft_pages():
            first = narrative_pages()[name].read_text(encoding="utf-8").splitlines()[0]
            assert first.strip() == DRAFT_MARKER, f"docs/guide/{name} hides its draft marker below the fold"

    def test_no_generated_page_carries_a_marker(self) -> None:
        """A generated page is never a draft: its text is a function of the sources it renders."""
        carriers = [
            name
            for name, path in guide_pages().items()
            if name.startswith(GENERATED_PREFIX) and _DRAFT_COMMENT.search(path.read_text(encoding="utf-8"))
        ]
        assert not carriers, f"generated pages carrying a draft marker: {carriers}"

    def test_a_marked_page_is_reported_and_an_unmarked_one_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """:func:`draft_pages` itself, not its pattern, over a corpus of two pages this test writes."""
        _page(tmp_path, f"{DRAFT_MARKER}\n\n# A page\n", "marked.md")
        _page(tmp_path, "# A page\n", "unmarked.md")
        monkeypatch.setattr(
            f"{__name__}.narrative_pages",
            lambda: {"marked.md": tmp_path / "marked.md", "unmarked.md": tmp_path / "unmarked.md"},
        )
        assert draft_pages() == ["marked.md"]

    def test_a_reworded_marker_is_still_seen(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """So that "draft" cannot be edited into something the report stops counting."""
        _page(tmp_path, "<!-- DRAFT: still being written -->\n\n# A page\n")
        monkeypatch.setattr(f"{__name__}.narrative_pages", lambda: {"mutated.md": tmp_path / "mutated.md"})
        assert draft_pages() == ["mutated.md"]


# The How-to section: the shape, the call blocks, the bench names

#: The How-to section's directory under ``docs/guide/``.
HOW_TO_PREFIX = "how-to/"

#: The How-to pages that are not scenarios: the section's index and the *Refusals*
#: page, which has one section per protocol code instead of the scenario shape.
HOW_TO_NON_SCENARIOS = frozenset({"how-to/index.md", "how-to/refusals.md"})

#: Every scenario page's ``##`` sections, in this order and no other.
HOW_TO_SHAPE = (
    "Purpose",
    "Before you start",
    "Names to resolve",
    "Steps",
    "What you get",
    "How to check it",
    "When it is refused",
)

#: The names table's header: the name, an example of it, and how to find yours.
NAMES_TABLE_HEADER = ("Name", "Example", "How to find yours")

#: A step's two labels: the action by hand (either one) and the assistant's.
BY_HAND_LABELS = ("*In VS Code:*", "*On the command line:*")
ASSISTANT_LABEL = "*Through the assistant:*"

#: The keywords comment a How-to page carries under its H1.
_KEYWORDS = re.compile(r"^<!-- keywords: \S(?:.*\S)? -->$")

#: A link into the *Refusals* page's section of one code.
_REFUSAL_LINK = re.compile(r"\]\(refusals\.md#([a-z-]+)\)")

#: The protocol's request schema, read by the call-block gate.
PROTOCOL_SCHEMA = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol" / "protocol.schema.json"
)

#: The MCP server's source, whose ``@mcp.tool()`` functions are the live tools.
SERVER_SOURCE = REPO_ROOT / "packages" / "mcp" / "src" / "swcraft_mcp" / "server.py"

#: The tools whose arguments are a protocol request: ``{"operation": tool} | arguments``.
PROTOCOL_TOOLS = frozenset({"discover", "read", "find", "change_set"})

#: The agent benchmark's committed bench and its two committed task sets.
AGENT_TASKS = AGENT_BENCH
BENCH_MODEL = AGENT_TASKS / "bench" / "model"
BENCH_TASK_DIRS = (AGENT_TASKS / "tasks", AGENT_TASKS / "large" / "tasks")

#: The example project the How-to pages take their names from.
EXAMPLE_PROJECT = REPO_ROOT / "examples" / "basic"

#: Bench names a How-to page may write, each with its reason; every entry is a name the gate stops checking.
BENCH_NAME_ALLOWANCES: dict[str, str] = {
    "RAM": (
        "the memory kind every page about budgets names in prose; the bench's memory element of that "
        "name is not a measured task's answer"
    ),
}

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def how_to_pages() -> dict[str, Path]:
    """The How-to section's pages, keyed by their path under ``docs/guide/``."""
    return {name: path for name, path in narrative_pages().items() if name.startswith(HOW_TO_PREFIX)}


def scenario_pages() -> dict[str, Path]:
    """The How-to pages in the scenario shape: every page but the index and *Refusals*."""
    return {name: path for name, path in how_to_pages().items() if name not in HOW_TO_NON_SCENARIOS}


def _fenced(text: str) -> list[tuple[str, str]]:
    """Each fenced block of *text*: its info string and its body."""
    blocks: list[tuple[str, str]] = []
    info: str | None = None
    body: list[str] = []
    for line in text.split("\n"):
        if line.startswith("```"):
            if info is None:
                info, body = line[3:].strip(), []
            else:
                blocks.append((info, "\n".join(body)))
                info = None
            continue
        if info is not None:
            body.append(line)
    return blocks


def _sections(text: str, level: int) -> list[tuple[str, str]]:
    """Each heading of exactly *level* outside a fence, its words (anchor off) and its text to the next."""
    out: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        heading = None
        if not fenced:
            match = re.match(r"^(#{1,6}) (.*)$", line)
            if match and len(match.group(1)) <= level:
                heading = match
        if heading is not None:
            if len(heading.group(1)) == level:
                out.append((re.sub(r"\s*\{ #[^ }]+ \}$", "", heading.group(2)).strip(), []))
            else:
                out.append(("", []))
            continue
        if out:
            out[-1][1].append(line)
    return [(title, "\n".join(lines)) for title, lines in out if title]


def _h1_lead(text: str) -> list[str]:
    """The lines between the page's H1 and its first ``##``."""
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith("# ")), None)
    if start is None:
        return []
    lead = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        lead.append(line)
    return lead


@functools.cache
def protocol_error_codes() -> tuple[str, ...]:
    """The protocol's closed ``errorCode`` enum, read from the schema."""
    schema = json.loads(PROTOCOL_SCHEMA.read_text(encoding="utf-8"))
    return tuple(schema["$defs"]["errorCode"]["enum"])


def how_to_keyword_problems(text: str) -> list[str]:
    """A How-to page's keywords: one ``<!-- keywords: ... -->`` comment under its H1."""
    found = [line for line in _h1_lead(text) if _KEYWORDS.match(line)]
    if len(found) != 1:
        return [f"carries {len(found)} keywords comments under its H1, not one"]
    return []


def how_to_shape_problems(text: str) -> list[str]:
    """What is wrong with a scenario page's shape, as sentences; empty when nothing is.

    **What it does not reach**: whether a name marked *example* is absent from ``examples/basic``, and
    whether a step's prose matches its call.
    """
    problems: list[str] = []
    sections = dict(_sections(text, 2))
    titles = [title for title, _ in _sections(text, 2)]
    if tuple(titles) != HOW_TO_SHAPE:
        problems.append(f"its sections are {titles}, not {list(HOW_TO_SHAPE)}")
    names = sections.get("Names to resolve", "")
    header = next((line for line in names.split("\n") if line.startswith("|")), "")
    if tuple(cell.strip() for cell in header.strip("|").split("|")) != NAMES_TABLE_HEADER:
        problems.append(f"Names to resolve has no table headed {' | '.join(NAMES_TABLE_HEADER)}")
    steps = sections.get("Steps", "")
    step_sections = _sections(steps, 3)
    if not step_sections:
        problems.append("Steps holds no `###` step")
    for title, body in step_sections:
        starts = [line.strip() for line in body.split("\n")]
        if not any(line.startswith(BY_HAND_LABELS) for line in starts):
            problems.append(f"the step {title!r} says nothing by hand ({' or '.join(BY_HAND_LABELS)})")
        if not any(line.startswith(ASSISTANT_LABEL) for line in starts):
            problems.append(f"the step {title!r} has no {ASSISTANT_LABEL} paragraph")
    if not any(info == "json" for info, _ in _fenced(steps)):
        problems.append("Steps holds no call block")
    refused = sections.get("When it is refused", "")
    codes = _REFUSAL_LINK.findall(refused)
    if not codes:
        problems.append("When it is refused links no section of the Refusals page")
    unknown = sorted(set(codes) - set(protocol_error_codes()))
    if unknown:
        problems.append(f"When it is refused links codes the protocol does not have: {unknown}")
    return problems


@functools.cache
def live_tools() -> dict[str, frozenset[str]]:
    """Every tool the MCP server registers, with the parameters it takes, read by ``ast``."""
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    tools: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            ast.unparse(decorator) == "mcp.tool()" for decorator in node.decorator_list
        ):
            arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            tools[node.name] = frozenset(argument.arg for argument in arguments if argument.arg != "ctx")
    return tools


@functools.cache
def _request_validator() -> Draft202012Validator:
    """A validator for ``protocol.schema.json#/$defs/request``."""
    schema = json.loads(PROTOCOL_SCHEMA.read_text(encoding="utf-8"))
    registry = SchemaRegistry().with_resources([(schema["$id"], SchemaResource.from_contents(schema))])
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/request"}, registry=registry)


def call_block_problems(text: str) -> list[str]:
    """What is wrong with *text*'s ``json`` call blocks, as sentences; empty when nothing is."""
    problems: list[str] = []
    for number, (info, body) in enumerate(_fenced(text), 1):
        if info != "json":
            continue
        where = f"json block {number}"
        try:
            call: Any = json.loads(body)
        except json.JSONDecodeError as error:
            problems.append(f"{where} does not parse: {error}")
            continue
        if not isinstance(call, dict) or set(call) != {"tool", "arguments"} or not isinstance(call["arguments"], dict):
            problems.append(f'{where} is not {{"tool": ..., "arguments": {{...}}}}')
            continue
        tool, arguments = call["tool"], call["arguments"]
        if tool not in live_tools():
            problems.append(f"{where} names {tool!r}, which the server does not register")
            continue
        extra = sorted(set(arguments) - live_tools()[tool])
        if extra:
            problems.append(f"{where}: {tool} takes no {extra}")
        if tool in PROTOCOL_TOOLS:
            errors = list(_request_validator().iter_errors({"operation": tool} | arguments))
            if errors:
                problems.append(f"{where}: the protocol refuses the request: {errors[0].message}")
    return problems


def _names_in(node: Any, keys: frozenset[str], out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str):
                out.add(value)
            _names_in(value, keys, out)
    elif isinstance(node, list):
        for value in node:
            _names_in(value, keys, out)


def _strings(node: Any, out: set[str], skip: frozenset[str] = frozenset({"layer"})) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in skip:
                _strings(value, out, skip)
    elif isinstance(node, list):
        for value in node:
            _strings(value, out, skip)
    elif isinstance(node, str):
        out.add(node.split("@", 1)[0])


def _looks_named(name: str) -> bool:
    """A name, not a word: an identifier with a capital, an underscore or a digit."""
    return bool(_IDENTIFIER.fullmatch(name)) and any(c.isupper() or c == "_" or c.isdigit() for c in name)


def _clearly_named(name: str) -> bool:
    """A name a sentence cannot hold by accident: a capital past the first letter, an underscore or a digit."""
    return bool(_IDENTIFIER.fullmatch(name)) and (
        any(c.isupper() for c in name[1:])
        and any(c.islower() for c in name)
        or "_" in name
        or any(c.isdigit() for c in name)
    )


@functools.cache
def model_vocabulary() -> frozenset[str]:
    """Every string value the model contract's fields enumerate: the model's own words, never an element's name."""
    kinds = current_registry().kinds.values()
    return frozenset(value for kind in kinds for field in kind.fields for value in field.enum if isinstance(value, str))


@functools.cache
def bench_only_names() -> frozenset[str]:
    """The names of the committed bench and its task files that ``examples/basic`` does not also have.

    Plain-word shapes and :data:`BENCH_NAME_ALLOWANCES` are not names here, nor is a task string that is a value
    of the model's vocabulary (:func:`_task_names`); a guide-question task is skipped, since its answer is read
    from the guide.
    """
    names: set[str] = set()
    declared: set[str] = set()
    for path in sorted(BENCH_MODEL.rglob("*.yaml"), key=lambda path: path.as_posix()):
        _names_in(yaml.safe_load(path.read_text(encoding="utf-8")), frozenset({"name", "id"}), declared)
    names |= {name for name in declared if _looks_named(name)}
    for directory in BENCH_TASK_DIRS:
        for path in sorted(directory.glob("*.json"), key=lambda path: path.as_posix()):
            task = json.loads(path.read_text(encoding="utf-8"))
            if task.get("kind") != "guide":
                names |= _task_names(task, frozenset(declared))
    example: set[str] = set()
    for path in EXAMPLE_PROJECT.rglob("*.yaml"):
        example |= set(_IDENTIFIER.findall(path.read_text(encoding="utf-8")))
    return frozenset(names - example - set(BENCH_NAME_ALLOWANCES))


def _task_names(task: dict[str, Any], declared: frozenset[str]) -> set[str]:
    """The clearly named strings a bench task writes, a value of the model's vocabulary left out.

    A vocabulary value still counts when the bench model (*declared*) or the task itself carries it as an
    element's ``name`` or ``id``: an element named like a vocabulary word is a name all the same.
    """
    written = {"expect": task.get("expect"), "reference": task.get("reference")}
    found: set[str] = set()
    _strings(written, found)
    found |= set(_IDENTIFIER.findall(task["prompt"]))
    elements: set[str] = set()
    _names_in(written, frozenset({"name", "id"}), elements)
    vocabulary = model_vocabulary() - declared - elements
    return {name for name in found - vocabulary if _clearly_named(name)}


def bench_name_problems(text: str) -> list[str]:
    """Every bench-only name *text* writes as a whole word, as sentences."""
    words = set(re.findall(r"(?<![\w-])[A-Za-z][A-Za-z0-9_]*(?![\w-])", text))
    return [f"names {name}, an element of the benchmark's bench" for name in sorted(words & bench_only_names())]


_SCENARIO = (
    "# Do a thing\n\n<!-- purpose: P -->\n\n<!-- keywords: thing, do -->\n\nLead.\n\n"
    "## Purpose { #purpose }\n\n<!-- purpose: P -->\n\nWhy.\n\n"
    "## Before you start { #before-you-start }\n\n<!-- purpose: P -->\n\n- A thing exists.\n\n"
    "## Names to resolve { #names-to-resolve }\n\n<!-- purpose: P -->\n\n"
    "| Name | Example | How to find yours |\n|---|---|---|\n| The component | `ExampleSWC` | `find` |\n\n"
    "## Steps { #steps }\n\n<!-- purpose: P -->\n\n### 1. Find it { #find-it }\n\n<!-- purpose: P -->\n\n"
    "*In VS Code:* open the file.\n\n*Through the assistant:* one call.\n\n"
    '```json\n{"tool": "find", "arguments": {"names": ["ExampleSWC"]}}\n```\n\n'
    "## What you get { #what-you-get }\n\n<!-- purpose: P -->\n\nIt.\n\n"
    "## How to check it { #how-to-check-it }\n\n<!-- purpose: P -->\n\nRead it.\n\n"
    "## When it is refused { #when-it-is-refused }\n\n<!-- purpose: P -->\n\n"
    "| Code | Meaning | What to do |\n|---|---|---|\n"
    "| [`stale-baseline`](refusals.md#stale-baseline) | moved | read |\n"
)

_SCENARIO_IDS = pytest.mark.parametrize("relative", sorted(scenario_pages()))
_HOW_TO_IDS = pytest.mark.parametrize("relative", sorted(how_to_pages()))


class TestHowTo:
    """The How-to section's three gates, over the committed pages and shown firing."""

    def test_the_section_has_its_index_its_refusals_page_and_scenarios(self) -> None:
        assert HOW_TO_NON_SCENARIOS <= set(how_to_pages())
        assert scenario_pages(), "the How-to section holds no scenario page"

    @_HOW_TO_IDS
    def test_every_page_carries_its_keywords(self, relative: str) -> None:
        problems = how_to_keyword_problems(how_to_pages()[relative].read_text(encoding="utf-8"))
        assert problems == [], f"docs/guide/{relative}: {problems}"

    @_SCENARIO_IDS
    def test_every_scenario_page_has_the_shape(self, relative: str) -> None:
        problems = how_to_shape_problems(scenario_pages()[relative].read_text(encoding="utf-8"))
        assert problems == [], f"docs/guide/{relative}:\n" + "\n".join(f"  - {p}" for p in problems)

    @_HOW_TO_IDS
    def test_every_call_block_is_a_live_call(self, relative: str) -> None:
        problems = call_block_problems(how_to_pages()[relative].read_text(encoding="utf-8"))
        assert problems == [], f"docs/guide/{relative}:\n" + "\n".join(f"  - {p}" for p in problems)

    @_HOW_TO_IDS
    def test_no_page_names_a_bench_only_element(self, relative: str) -> None:
        problems = bench_name_problems(how_to_pages()[relative].read_text(encoding="utf-8"))
        assert problems == [], f"docs/guide/{relative}: {problems}"

    def test_every_page_links_the_refusals_page_at_a_code_it_has(self) -> None:
        anchors = {anchor for _, anchor in site_toc((GUIDE_DIR / "how-to" / "refusals.md").read_text(encoding="utf-8"))}
        for relative, path in how_to_pages().items():
            for code in _REFUSAL_LINK.findall(path.read_text(encoding="utf-8")):
                assert code in anchors, f"docs/guide/{relative} links refusals.md#{code}, which is no section"

    def test_a_well_shaped_page_passes(self) -> None:
        assert how_to_keyword_problems(_SCENARIO) == []
        assert how_to_shape_problems(_SCENARIO) == []
        assert call_block_problems(_SCENARIO) == []
        assert bench_name_problems(_SCENARIO) == []

    def test_a_page_without_keywords_fails(self) -> None:
        assert how_to_keyword_problems(_SCENARIO.replace("<!-- keywords: thing, do -->\n\n", "")) != []

    def test_a_missing_or_moved_section_fails(self) -> None:
        moved = _SCENARIO.replace("## What you get", "## Outcome")
        assert any("its sections are" in p for p in how_to_shape_problems(moved))

    def test_a_names_table_without_the_example_column_fails(self) -> None:
        text = _SCENARIO.replace("| Name | Example | How to find yours |", "| Name | How to find yours |")
        assert any("Names to resolve" in p for p in how_to_shape_problems(text))

    def test_a_step_without_the_assistant_or_by_hand_fails(self) -> None:
        text = _SCENARIO.replace("*Through the assistant:* one call.", "One call.")
        assert any("no *Through the assistant:*" in p for p in how_to_shape_problems(text))
        text = _SCENARIO.replace("*In VS Code:* open the file.", "Open the file.")
        assert any("says nothing by hand" in p for p in how_to_shape_problems(text))

    def test_a_refusal_row_naming_no_protocol_code_fails(self) -> None:
        text = _SCENARIO.replace("refusals.md#stale-baseline", "refusals.md#stale-token")
        assert any("does not have" in p for p in how_to_shape_problems(text))

    def test_a_call_block_naming_no_live_tool_fails(self) -> None:
        text = _SCENARIO.replace('"tool": "find"', '"tool": "lookup"')
        assert any("does not register" in p for p in call_block_problems(text))

    def test_a_call_block_with_an_argument_the_tool_does_not_take_fails(self) -> None:
        body = '```json\n{"tool": "validate", "arguments": {"strict": true}}\n```\n'
        assert any("takes no" in p for p in call_block_problems(body))

    def test_a_call_block_the_protocol_refuses_fails(self) -> None:
        body = (
            '```json\n{"tool": "change_set", "arguments": {"baseline": "not-a-token", "operations": '
            '[{"operation": "create", "address": {"layer": "swc", "element": "X"}, "value": {}}]}}\n```\n'
        )
        assert any("protocol refuses" in p for p in call_block_problems(body))

    def test_a_call_block_that_is_not_a_call_fails(self) -> None:
        assert any("is not" in p for p in call_block_problems('```json\n{"names": ["X"]}\n```\n'))
        assert any("does not parse" in p for p in call_block_problems("```json\n{not json\n```\n"))

    def test_a_bench_only_name_is_found_and_a_shared_one_is_not(self) -> None:
        assert bench_name_problems("Connect `DiagnosticManager` to it.\n") != []
        assert bench_name_problems("Map it to `uint8` on the `CPU`.\n") == []

    def test_the_bench_names_come_from_the_bench_and_both_task_sets(self) -> None:
        names = bench_only_names()
        assert {"DiagnosticManager", "IThermalProtectionStatus", "pForwardedThermalStatus"} <= names
        assert "Swc075abcdefghij" in names, "the large bench is held through its task files"
        assert not {"uint8", "uint16", "CPU", "value", "Add"} & names

    def test_a_value_of_the_model_vocabulary_in_a_task_is_not_a_bench_name(self) -> None:
        assert "call_returned" in model_vocabulary()
        written = {"prompt": "Add a call_returned event to Sensor_2.", "expect": {"type": "call_returned"}}
        assert _task_names(written, frozenset()) == {"Sensor_2"}
        named = {"prompt": "Rename it.", "expect": {"name": "call_returned"}}
        assert _task_names(named, frozenset()) == {"call_returned"}
        assert _task_names({"prompt": "Use call_returned."}, frozenset({"call_returned"})) == {"call_returned"}
        assert bench_name_problems("A call_returned event on DiagnosticManager.\n") == [
            "names DiagnosticManager, an element of the benchmark's bench"
        ]

    def test_every_allowance_states_why(self) -> None:
        for name, reason in BENCH_NAME_ALLOWANCES.items():
            assert reason.strip(), name
