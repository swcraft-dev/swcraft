"""Shared fixtures for the guide's gates: the tree, the navigation and the vocabulary.

The reference drift gate, the render gate, the guide surface gate and
`mcp_server`'s tool-reference gate all first ask where the tree is, which files are
in it and what the committed navigation names; the answers are held once here so
the gates cannot drift apart. Each gate's filters stay at its call sites.
`packages/mcp/pyproject.toml` puts this directory on its pytest `pythonpath`, which
makes the import across the two projects work.

**The vocabulary.** The guide (`docs/guide/`) is written for an architect modelling
in SWCraft; the engineering record (`README.md`, the concept documents, decision
records, plans) has its own vocabulary — phase numbers, decision-record citations,
declaration-file names, internal mechanisms — which reads to the guide's reader as
a dangling reference. `FORBIDDEN_VOCABULARY` is that list, read by
:mod:`tools.tests.test_model_contract` (no `description` of the shipped contract may
contain it: those strings are rendered into a user-facing page and the editor's
hover), by the guide's narrative and generated-page gates and by other suites
(`test_cli.py`, `test_public_tree_hygiene.py`, `packages/mcp/tests/test_tool_reference.py`).

**What the list does not reach:** prose free of every term and still wrong or
written for the wrong reader; a string a generator begins rendering later that no
gate scans; and the bare word "contract", deliberately absent because the guide uses
it legitimately. `packages/mcp/tests/test_removed_tool_names.py` and
`test_generic_core_gate.py`'s documentation scan reach `docs/guide/` independently.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swcraft_tools.guide_corpus import effective_markdown_config
from swcraft_tools.guide_search import PURPOSE_CAP_CHARACTERS

__all__ = [
    "FORBIDDEN_VOCABULARY",
    "ForbiddenTerm",
    "GUIDE_DIR",
    "MKDOCS_YML",
    "REFERENCE_DIR",
    "REPO_ROOT",
    "committed_pages",
    "generated_heading_problems",
    "heading_marker_problems",
    "help_links_constant",
    "nav_paths",
    "site_html",
    "site_toc",
    "vocabulary_hits",
]

#: The repository root, from this file's own location: derived rather than imported
#: from a ``conftest``, which resolves to a different module depending on which
#: project's suite is running.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The guide's source tree — ``mkdocs.yml``'s ``docs_dir`` — and the generated
#: section inside it.
GUIDE_DIR = REPO_ROOT / "docs" / "guide"
REFERENCE_DIR = GUIDE_DIR / "reference"

#: The site configuration whose ``nav`` block the generator rewrites and every
#: navigation gate reads.
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"


def help_links_constant(name: str) -> str:
    """The source text of ``helpLinks.ts``' ``export const <name> = …;``, read rather than transcribed.

    The editor's rule links and *Learn more* depend on constants the CLI's output
    and the rules page must agree with. What it does not reach: a constant that is
    not exported, or one not written as one expression ending in ``;`` at a line end.
    """
    source = (REPO_ROOT / "vscode_extension" / "src" / "helpLinks.ts").read_text(encoding="utf-8")
    found = re.search(rf"^export const {name}\b[^=]*= (.+?);$", source, re.MULTILINE | re.DOTALL)
    assert found is not None, f"helpLinks.ts declares {name}"
    return found.group(1)


def committed_pages(root: Path | None = None, pattern: str = "*") -> dict[str, Path]:
    """Every committed file under *root*, keyed by its path relative to :data:`GUIDE_DIR`.

    *root* defaults to the whole guide and *pattern* to every file. Sorted, with
    POSIX keys, so a failure message reads the same on every host.

    **What it does not reach:** which of those pages a gate should read.
    ``version.md`` is build-generated and gitignored, so it may be present or absent;
    a gate that must not see it filters it out at the call site.
    """
    root = GUIDE_DIR if root is None else root
    return {
        path.relative_to(GUIDE_DIR).as_posix(): path
        for path in sorted(root.rglob(pattern), key=lambda path: path.as_posix())
        if path.is_file()
    }


def nav_paths(node: Any) -> list[str]:
    """Every document path a ``nav`` node names, depth-first, in declaration order.

    *node* is anything ``yaml.safe_load`` produces for a navigation block or a
    section of it: a string is a path, a list is rows, a mapping is ``label: value``
    pairs. A caller scopes the walk by handing it the sub-node it cares about.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [path for entry in node for path in nav_paths(entry)]
    if isinstance(node, dict):
        return [path for value in node.values() for path in nav_paths(value)]
    return []


@dataclass(frozen=True)
class ForbiddenTerm:
    """One term of the engineering vocabulary, with the reason it is out of scope.

    *pattern* is matched case-insensitively; *label* is what a failure message
    names, so it reads as the term rather than as a regular expression.
    """

    label: str
    pattern: str
    reason: str

    def search(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE) is not None


#: The engineering vocabulary, in the order a reader would meet it. Every entry was
#: measured against the contract's descriptions, the tools' descriptions and the
#: `argparse` help before it was added, so none is speculative.
FORBIDDEN_VOCABULARY: tuple[ForbiddenTerm, ...] = (
    ForbiddenTerm(
        label="Phase <n>",
        pattern=r"\bphase\s+\d",
        reason="the development history's numbering; the reader of the guide has no phase list",
    ),
    ForbiddenTerm(
        label="ADR <n>",
        pattern=r"\badr\s+\d",
        reason="a decision record lives in the engineering tree and is not linked from the guide",
    ),
    ForbiddenTerm(
        label="total consumer",
        pattern=r"\btotal consumer",
        reason="an internal coverage status, meaningless outside the coverage declarations",
    ),
    ForbiddenTerm(
        label="model contract",
        pattern=r"\bmodel contract\b",
        reason=(
            "the internal name of the declaration file; the guide says what the rule IS, not which "
            "file declares it. The bare word 'contract' is deliberately still allowed"
        ),
    ),
    ForbiddenTerm(
        label="binding.json",
        pattern=r"binding\.json",
        reason="an internal declaration file the reader never opens",
    ),
    ForbiddenTerm(
        label="docs/concepts/",
        pattern=r"docs/concepts/",
        reason="a path into the engineering record; the guide links to what a user reads",
    ),
    ForbiddenTerm(
        label="registry",
        pattern=r"\bregistr(y|ies)\b",
        reason="an internal mechanism's name; to the reader the vocabulary is simply the model",
    ),
)


def vocabulary_hits(text: str) -> list[str]:
    """The labels of every forbidden term *text* contains, in list order."""
    return [term.label for term in FORBIDDEN_VOCABULARY if term.search(text)]


# ---------------------------------------------------------------------------
# The headings' authoring markers: explicit anchors, unchanged ids, purposes
# ---------------------------------------------------------------------------

#: An ATX heading line, its level and its text (attribute list included).
_ATX_HEADING = re.compile(r"^(#{1,6}) (.*)$")

#: The explicit anchor ``attr_list`` reads off the end of a heading.
_EXPLICIT_ANCHOR = re.compile(r" \{ #([^ }]+) \}$")

#: The purpose comment the guide's corpus producer reads under a heading.
_PURPOSE = re.compile(r"^<!-- purpose: (\S(?:.*\S)?) -->$")


@functools.cache
def _site_markdown() -> Any:
    """python-markdown configured as the site build configures it: MkDocs' effective extension list."""
    import markdown

    extensions, configs = effective_markdown_config(MKDOCS_YML)
    return markdown.Markdown(extensions=extensions, extension_configs=configs)


def site_toc(text: str) -> list[tuple[int, str]]:
    """Every heading's level and HTML id, as the site mints them (``toc_tokens``, flattened)."""
    md = _site_markdown()
    md.reset()
    md.convert(text)
    out: list[tuple[int, str]] = []

    def walk(tokens: list[dict[str, Any]]) -> None:
        for token in tokens:
            out.append((int(token["level"]), str(token["id"])))
            walk(token["children"])

    walk(md.toc_tokens)
    return out


def site_html(text: str) -> str:
    """The HTML the site renders *text* to, under MkDocs' effective Markdown configuration."""
    md = _site_markdown()
    md.reset()
    return str(md.convert(text))


def heading_marker_problems(text: str) -> list[str]:
    """What is wrong with a page's authoring markers on its headings, as sentences; empty when nothing is.

    Two rules, over every heading outside a fenced block:

    * every ``##``/``###`` carries an explicit anchor, ``{ #anchor }``, so an
      edit of the heading's words moves neither the site's URL nor the guide
      corpus's section id;
    * every heading of levels 1-3 has a purpose comment as the block right under
      it, at most ``guide_search.PURPOSE_CAP_CHARACTERS`` characters.

    Applied to every guide page: the generated ones by the render gate, the
    hand-written ones by ``test_guide_surface.py``'s marker gate.

    **What this does not reach**: whether a purpose says anything true, whether an
    anchor is the one the page carried before (the hand-written pages' record and
    the generated pages' third rule hold that), and a heading the line scan cannot
    see (setext, inside HTML) -- the corpus producer refuses such a page.
    """
    lines = text.split("\n")
    problems: list[str] = []
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _ATX_HEADING.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        if level > 3:
            continue
        if level >= 2 and _EXPLICIT_ANCHOR.search(line) is None:
            problems.append(f"line {index + 1}: {line!r} has no explicit anchor")
        below = lines[index + 1 : index + 4]
        purpose = _PURPOSE.match(below[1]) if len(below) == 3 and below[0] == "" and below[2] == "" else None
        if purpose is None:
            problems.append(f"line {index + 1}: {line!r} has no purpose comment as the block under it")
        elif len(purpose.group(1)) > PURPOSE_CAP_CHARACTERS:
            problems.append(
                f"line {index + 1}: the purpose under {line!r} is {len(purpose.group(1))} characters, "
                f"over {PURPOSE_CAP_CHARACTERS}"
            )
    return problems


def generated_heading_problems(text: str) -> list[str]:
    """What is wrong with a generated page's headings, as sentences; empty when nothing is.

    Three rules, read off the page as the site reads it: the two of
    :func:`heading_marker_problems` -- an explicit anchor on every ``##``/``###``
    and a purpose under every heading of levels 1-3 -- and

    * the explicit anchors change no id: the page's ``toc_tokens`` under MkDocs'
      effective configuration equal those of the page with every attribute list
      removed. A generator derives each anchor from the name it renders, so the two
      stay equal; a hand-written heading's words may change while its anchor stays,
      so this rule is the generated pages' alone.

    **What this does not reach**: whether a purpose says anything true, and a
    heading the line scan cannot see (setext, inside HTML) -- the corpus producer
    refuses such a page.
    """
    lines = text.split("\n")
    problems = heading_marker_problems(text)
    bare = "\n".join(_EXPLICIT_ANCHOR.sub("", line) if _ATX_HEADING.match(line) else line for line in lines)
    with_anchors, without = site_toc(text), site_toc(bare)
    if with_anchors != without:
        moved = [(a, b) for a, b in zip(with_anchors, without, strict=False) if a != b]
        problems.append(f"the explicit anchors change the page's ids (explicit, minted): {moved or 'count differs'}")
    return problems
