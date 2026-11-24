"""The generated half of the user guide's reference section.

``docs/guide/reference/`` is committed output: every page is rendered from a declaration the product
already reads, so a layer, a field, a rule or a command cannot change without its page changing. This
package owns the pages generated inside ``packages/tools/``; the tool reference is generated in
``packages/mcp/``, because ``swcraft_tools`` may not import ``swcraft_mcp``. Both are written by
``python scripts/dev.py docs-reference``.

:func:`all_artifacts` is the union of the generator modules' pages, keyed by path relative to
``docs/guide/``: one mapping the drift gate compares against the committed tree in both directions.

The reference index is rendered from that union: a new page appears with no second edit, and a page
with no summary is refused (:class:`UnsummarizedPage`) rather than listed as a bare filename. The one
page this package cannot see, ``reference/tools.md``, is named in :data:`FOREIGN_PAGES` so the index
is complete. :func:`nav_entries` derives ``mkdocs.yml``'s navigation rows from the same union.

**What this package does not reach:** whether the declarations are true of the code, whether the
prose reads well, and the hand-written narrative pages beside ``reference/``.
"""

from __future__ import annotations

from swcraft_tools.guide_reference.catalogue import FOREIGN_PAGES as FOREIGN_PAGES
from swcraft_tools.guide_reference.catalogue import INDEX_PATH as INDEX_PATH
from swcraft_tools.guide_reference.catalogue import NAV_SECTION_TITLE as NAV_SECTION_TITLE
from swcraft_tools.guide_reference.catalogue import UnsummarizedPage as UnsummarizedPage
from swcraft_tools.guide_reference.catalogue import UntitledPage as UntitledPage
from swcraft_tools.guide_reference.catalogue import all_artifacts as all_artifacts
from swcraft_tools.guide_reference.catalogue import index_page as index_page
from swcraft_tools.guide_reference.catalogue import nav_entries as nav_entries

__all__ = [
    "FOREIGN_PAGES",
    "INDEX_PATH",
    "NAV_SECTION_TITLE",
    "UnsummarizedPage",
    "UntitledPage",
    "all_artifacts",
    "index_page",
    "nav_entries",
]
