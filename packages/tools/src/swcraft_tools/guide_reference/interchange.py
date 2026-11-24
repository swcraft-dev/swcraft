"""The guide's page of the requirements interchange schema.

``swcraft import requirements --source FILE`` reads one JSON file, a strict profile of Sphinx-Needs'
``needs.json`` format, written by a converter from a requirements tool. This module renders the
shipped schema, ``requirements_interchange.schema.json``, onto one guide page so a converter's author
copies it from the guide rather than from a source tree.

The schema is carried verbatim, byte for byte, inside one three-backtick ``json`` fence: a
``json.loads``/``json.dumps`` round trip would publish a second spelling of the shipped text. A schema
text carrying three or more backticks would close the fence early and is refused
(:class:`UnfenceableSchema`). No model declaration reaches this module.

**What this page does not reach:** the rules JSON Schema cannot express (comparing a value with a key,
counting members), which are the reader's alone and named in the schema's ``description``; nothing
here checks that description is complete. Nor whether the schema is valid JSON Schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import files as resource_files

import swcraft_tools.guide_reference.render as render
from swcraft_tools.requirements_interchange import SCHEMA_RESOURCE

__all__ = [
    "INTERCHANGE_PATH",
    "UnfenceableSchema",
    "all_artifacts",
    "fenced_schema_page",
    "interchange_page",
]

#: The relative path of the page this module renders.
INTERCHANGE_PATH = "reference/requirements-interchange.md"

#: The fence the schema is carried in. Three backticks, CommonMark's shortest.
_FENCE = "```"


class UnfenceableSchema(ValueError):
    """A schema text the fence cannot hold unchanged: it carries a run of three or more backticks."""

    def __init__(self) -> None:
        super().__init__(f"the schema text carries {_FENCE!r}, which would close the page's fence early")


def _schema_text() -> str:
    """The shipped schema file's text, read from the package data exactly as the wheel carries it."""
    return (resource_files("swcraft_tools") / SCHEMA_RESOURCE).read_text(encoding="utf-8")


def fenced_schema_page(title: str, purpose: str, paragraphs: Sequence[str], text: str) -> str:
    """A schema page: heading and its *purpose*, notice, *paragraphs* and the schema *text* inside a ``json`` fence.

    Shared by both schema pages. Raises :class:`UnfenceableSchema` for a *text* carrying the fence; a
    *text* not ending in a newline gets one, the only byte the fence adds.
    """
    if _FENCE in text:
        raise UnfenceableSchema
    body = text if text.endswith("\n") else text + "\n"
    return render.document(
        render.GENERATED_COMMENT,
        render.heading(1, title, purpose=purpose),
        render.GENERATED_NOTICE,
        *(render.paragraph(paragraph) for paragraph in paragraphs),
        f"{_FENCE}json\n{body}{_FENCE}",
    )


def interchange_page(*, text: str | None = None) -> str:
    """The page: heading, notice, preamble and the schema *text* verbatim inside a ``json`` fence.

    *text* defaults to the shipped schema file's text.
    """
    return fenced_schema_page(
        "The requirements interchange file",
        "Checking the requirements file a converter wrote against the schema before you import it",
        (
            "`swcraft import requirements --source FILE` reads one JSON file: a strict profile of the "
            "Sphinx-Needs `needs.json` format. A converter from your requirements tool writes it, and the "
            "schema below is the one SWCraft ships, in JSON Schema Draft 2020-12, for checking a "
            "converter's output before you import it.",
            "The schema cannot express every rule. Its own `description` names the ones SWCraft's reader "
            "checks as well, and `swcraft import requirements --dry-run` is the complete check: it reads "
            "the file and reports what the import would change, writing nothing.",
        ),
        _schema_text() if text is None else text,
    )


def all_artifacts() -> dict[str, str]:
    """The one page this module renders, keyed by its path relative to ``docs/guide/``."""
    return {INTERCHANGE_PATH: interchange_page()}
