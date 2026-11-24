"""The guide's page of the model passport schema.

``swcraft passport`` writes one JSON file describing a model's shape (counts, distributions and
lengths, never a name, a value or a text), which a user sends where the model may not go. This module
renders the shipped schema, ``model_passport.schema.json``, onto one guide page, verbatim, through
:func:`.interchange.fenced_schema_page` (see there for why it is not re-rendered). It reads the
schema through ``passport.schema_text``; no model declaration reaches it.

**What this page does not reach:** the rules the schema cannot express (a total equals the sum of its
parts, a list is descending), which the schema's ``description`` names and the pass meets by
construction; and whether the schema is valid or the pass meets it, which the pass's tests check.
"""

from __future__ import annotations

from swcraft_tools.guide_reference.interchange import UnfenceableSchema, fenced_schema_page
from swcraft_tools.passport import schema_text

__all__ = [
    "PASSPORT_PATH",
    "UnfenceableSchema",
    "all_artifacts",
    "passport_page",
]

#: The relative path of the page this module renders.
PASSPORT_PATH = "reference/model-passport.md"


def passport_page(*, text: str | None = None) -> str:
    """The page: heading, notice, preamble and the schema *text* verbatim inside a ``json`` fence.

    *text* defaults to the shipped schema file's text.
    """
    return fenced_schema_page(
        "The model passport",
        "What a model passport holds, and sending one instead of a model that cannot be shared",
        (
            "A model passport describes the shape of a model in counts, distributions and lengths: how "
            "many components, interfaces and connectors it holds, how their ports are spread, how long "
            "its identifiers are. It carries no name, no value and no text, so nothing it holds says "
            "what the model is about.",
            "When a model cannot leave your organisation and you need to report a problem SWCraft has "
            "with it, send the passport instead. "
            "`swcraft passport --project swcraft.yaml --output passport.json` writes it; without "
            "`--output` it goes to standard output, so you can read every number before you send it. The "
            "file carries no path either, and a project the command cannot read is reported in a sentence "
            "that names no path and nothing of the model.",
            "On SWCraft's side the passport tells what shape of model the problem occurs on. Generating "
            "a model of that shape from it, to reproduce the problem, is not possible for every passport "
            "yet: a passport whose shape the specimen generator cannot meet is refused by name, never "
            "approximated. The specimen generator, the program the schema's own `description` below "
            "names, is `swcraft-specimens`: a program of its own, separate from SWCraft's commands, "
            "delivered with the workstation archive and installed beside `swcraft`. "
            "`swcraft-specimens specimen passport.json --out specimen` generates the model. That "
            "`description` says which parts of a passport it meets exactly and which it compares after "
            "generating.",
        ),
        schema_text() if text is None else text,
    )


def all_artifacts() -> dict[str, str]:
    """The one page this module renders, keyed by its path relative to ``docs/guide/``."""
    return {PASSPORT_PATH: passport_page()}
