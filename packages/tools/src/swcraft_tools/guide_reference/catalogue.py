"""The reference section's catalogue: every generated page, the index rendered from them, and the nav rows.

The package ``__init__`` re-exports these names; this module imports its siblings by module path and
nothing from the package itself.
"""

from __future__ import annotations

from collections.abc import Mapping

import swcraft_tools.guide_reference.cli as cli
import swcraft_tools.guide_reference.interchange as interchange
import swcraft_tools.guide_reference.model as model
import swcraft_tools.guide_reference.passport as passport
import swcraft_tools.guide_reference.render as render
import swcraft_tools.guide_reference.rules as rules
from swcraft_tools.model_registry import ModelRegistry

#: The relative path of the section index this package renders.
INDEX_PATH = "reference/index.md"

#: The title and one-line summary of each page the index lists by name, keyed by its path relative to
#: ``docs/guide/``. The layer pages under ``reference/model/`` are folded into the model index's row.
PAGE_SUMMARIES: dict[str, tuple[str, str]] = {
    model.MODEL_INDEX_PATH: (
        "The model layers",
        "Every layer of the model, and for each one every kind of element and every key it accepts.",
    ),
    rules.RULES_PATH: (
        "The checks",
        "Every check SWCraft runs over a project, what each one requires and which layers it reads.",
    ),
    cli.CLI_PATH: (
        "The command line",
        "Every `swcraft` command, every option it accepts and what it does when you leave it out.",
    ),
    interchange.INTERCHANGE_PATH: (
        "The requirements interchange file",
        "The schema of the file `swcraft import requirements` reads, for checking a converter's output.",
    ),
    passport.PASSPORT_PATH: (
        "The model passport",
        "The schema of the file `swcraft passport` writes, the shape of a model without its names.",
    ),
}

#: The prefix of this section's pages, stripped from an index link target (a sibling link).
_REFERENCE_PREFIX = "reference/"

#: The prefix whose pages the model index's row already covers.
MODEL_PAGE_PREFIX = f"{_REFERENCE_PREFIX}model/"

#: The pages of this section another package generates, with their index rows. ``reference/tools.md``
#: is rendered by ``swcraft_mcp.tool_reference``, which ``swcraft_tools`` may not import, so it is named
#: here rather than discovered; ``packages/mcp/tests/test_tool_reference.py`` asserts the name.
FOREIGN_PAGES: dict[str, tuple[str, str]] = {
    "reference/tools.md": (
        "The assistant's tools",
        "Every tool the assistant can call, with the arguments it fills in for each one.",
    ),
}

#: The ``nav`` section every generated page hangs under in ``mkdocs.yml``, shared by
#: :func:`nav_entries`, the rewrite in ``__main__`` and the nav gate so they read one block.
NAV_SECTION_TITLE = "Reference"

#: The pages whose navigation label is not their own title: the index's heading is "Reference",
#: and "Reference > Reference" would name the same thing twice.
_NAV_TITLE_OVERRIDES = {INDEX_PATH: "Overview"}


class UntitledPage(ValueError):
    """A generated page with no level-1 heading to take a navigation label from.

    Raised rather than falling back to the file name, which is a path, not a label.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"{path}: no `# ` heading to take a navigation label from")


class UnsummarizedPage(ValueError):
    """A generated page the index has no summary for.

    Raised rather than listing the bare path, which would read as a description of a page that has none.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"{path}: no index summary; add one to guide_reference.catalogue.PAGE_SUMMARIES")


def index_page(artifacts: Mapping[str, str]) -> str:
    """The reference section's index, rendered from the pages *artifacts* holds, in the mapping's order.

    :data:`FOREIGN_PAGES` is appended last, so the index's claim to list the section exhaustively
    holds.
    """
    rows = []
    for path in artifacts:
        if path == INDEX_PATH:
            continue
        if path.startswith(MODEL_PAGE_PREFIX) and path != model.MODEL_INDEX_PATH:
            continue
        if path not in PAGE_SUMMARIES:
            raise UnsummarizedPage(path)
        title, summary = PAGE_SUMMARIES[path]
        rows.append((render.link(title, path[len(_REFERENCE_PREFIX) :]), summary))
    for path, (title, summary) in FOREIGN_PAGES.items():
        rows.append((render.link(title, path[len(_REFERENCE_PREFIX) :]), summary))
    return render.document(
        render.GENERATED_COMMENT,
        render.heading(1, "Reference", purpose="Where to look up a key, a command, a check or a tool, page by page"),
        render.GENERATED_NOTICE,
        render.paragraph(
            "The pages in this section list what SWCraft accepts and what it does, exhaustively. They are "
            "the place to look up a key, a command or a check; the pages outside this section are the place "
            "to learn how the pieces fit together."
        ),
        render.table(("Page", "What it lists"), rows),
    )


def all_artifacts(*, registry: ModelRegistry | None = None) -> dict[str, str]:
    """Every page generated inside ``packages/tools/``, keyed by its path relative to ``docs/guide/``.

    The generator sets first, in declaration order, then the index. ``registry`` is threaded only to
    the generators that read one, so a ``use_registry`` block reaches these pages as every other total
    consumer. The rules page is rendered from the contract document, the schema pages from package
    data; the command page builds its ``argparse`` tree itself, which still resolves ``swcraft list``'s
    layers from the ambient registry.
    """
    artifacts: dict[str, str] = {}
    artifacts.update(model.all_artifacts(registry=registry))
    artifacts.update(rules.all_artifacts())
    artifacts.update(cli.all_artifacts())
    artifacts.update(interchange.all_artifacts())
    artifacts.update(passport.all_artifacts())
    artifacts[INDEX_PATH] = index_page(artifacts)
    return artifacts


def title_of(path: str, text: str) -> str:
    """The navigation label of one generated page: its own level-1 heading.

    Read from the rendered text, so retitling a page in its generator retitles its navigation row;
    :data:`_NAV_TITLE_OVERRIDES` is the one exception.
    """
    if path in _NAV_TITLE_OVERRIDES:
        return _NAV_TITLE_OVERRIDES[path]
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise UntitledPage(path)


def nav_entries(artifacts: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The ``mkdocs.yml`` navigation rows for *artifacts*: ``(label, path)`` pairs, in order.

    The section index first, then every other page in the mapping's order. **Does not reach:** the
    tool reference generated in ``packages/mcp/``.
    """
    ordered = [INDEX_PATH, *(path for path in artifacts if path != INDEX_PATH)]
    return tuple((title_of(path, artifacts[path]), path) for path in ordered)
