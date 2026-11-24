"""The guide's tool reference: every tool the assistant can call.

Generates ``docs/guide/reference/tools.md`` from the live registration,
``asyncio.run(mcp.list_tools())``, so a tool this module renders is a tool the
assistant really has. It lives here rather than in ``swcraft_tools.guide_reference``
because ``swcraft_tools`` may not import ``swcraft_mcp``; it takes only the
emitters of ``swcraft_tools.guide_reference.render``. ``python scripts/dev.py
docs-reference`` runs this module as one step.

Rendering is deterministic (registration order, declaration order, no clock, host
path or version), so a re-render is byte-identical. A tool with no description, a
parameter shape with no rendering or a top-level schema that is not an object
raises :class:`UnrenderableTool` or :class:`UnrenderableParameter` rather than
leaving a blank cell.

What the page does not reach: only the first paragraph of each tool's
description is rendered, and of each parameter only its name, what it accepts
and whether it is required.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastmcp.tools import Tool
from swcraft_tools.guide_reference import render

from swcraft_mcp.server import mcp

__all__ = [
    "TOOLS_PATH",
    "UnrenderableParameter",
    "UnrenderableTool",
    "all_artifacts",
    "main",
    "parameter_rows",
    "purpose",
    "registered_tools",
    "tools_page",
]

#: The page this module renders, relative to ``docs/guide/`` like ``mkdocs.yml``'s rows.
TOOLS_PATH = "reference/tools.md"

_TABLE_HEADERS = ("Argument", "Accepted value", "Required")

#: How each JSON Schema scalar type is written for a reader. A mapping, so a type
#: with no phrase is a missing key and refused rather than given a default.
_SCALAR_PHRASES = {
    "string": "text",
    "boolean": "`true` or `false`",
    "integer": "a whole number",
    "number": "a number",
    "object": "a structured value",
}


class UnrenderableTool(ValueError):
    """A registered tool this generator has no rendering for.

    Raised instead of skipping it: a reader who does not find a tool concludes
    it does not exist.
    """

    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool}: {reason}")


class UnrenderableParameter(ValueError):
    """A parameter schema shape this generator has no rendering for.

    An empty cell would read as an argument that accepts anything.
    """

    def __init__(self, tool: str, parameter: str, reason: str) -> None:
        self.tool = tool
        self.parameter = parameter
        self.reason = reason
        super().__init__(f"{tool}.{parameter}: {reason}")


def registered_tools() -> tuple[Tool, ...]:
    """Every tool the server registers, in registration order."""
    return tuple(asyncio.run(mcp.list_tools()))


def purpose(tool: Tool) -> str:
    """One tool's purpose: the first paragraph of its description, on one line.

    The paragraph, not the line, so a wrapped summary is not cut mid-clause.
    """
    description = (tool.description or "").strip()
    if not description:
        raise UnrenderableTool(tool.name, "the tool declares no description to take a purpose from")
    return render.paragraph(description.split("\n\n", 1)[0])


def _type_phrase(tool: str, name: str, schema: dict[str, Any]) -> str:
    """What one parameter accepts, from its JSON Schema fragment.

    An ``anyOf``'s ``null`` branch is dropped: the "Required" column already says
    the argument may be left out.
    """
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        if not branches:
            raise UnrenderableParameter(tool, name, "accepts nothing but null")
        return " or ".join(_type_phrase(tool, name, branch) for branch in branches)
    declared = schema.get("type")
    if declared == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise UnrenderableParameter(tool, name, "a list whose entries the schema does not describe")
        return f"a list, each entry {_type_phrase(tool, name, items)}"
    phrase = _SCALAR_PHRASES.get(declared) if isinstance(declared, str) else None
    if phrase is None:
        raise UnrenderableParameter(tool, name, f"no guide rendering for the schema type {declared!r}")
    return phrase


def _requirement_phrase(schema: dict[str, Any], required: bool) -> str:
    """Whether one parameter must be given: required, optional, or optional with its default."""
    if required:
        return "required"
    default = schema.get("default", None)
    if default is None:
        return "optional"
    return f"optional — {render.code(render.literal(default))} when you leave it out"


def parameter_rows(tool: Tool) -> tuple[tuple[str, ...], ...]:
    """One row per argument of *tool*, in the order the tool declares them."""
    schema = tool.parameters or {}
    declared_type = schema.get("type")
    if declared_type not in (None, "object"):
        raise UnrenderableTool(tool.name, f"its arguments are declared as {declared_type!r} rather than an object")
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    return tuple(
        (
            render.code(name),
            _type_phrase(tool.name, name, fragment),
            _requirement_phrase(fragment, name in required),
        )
        for name, fragment in properties.items()
    )


def _anchor(name: str) -> str:
    """The explicit anchor of one tool's heading, and the target of its link in the summary table.

    MkDocs' own slug shape (lower case, the backticks dropped), so the anchor
    keeps the id the heading minted before anchors were explicit.
    """
    return name.lower()


def _tool_section(tool: Tool) -> tuple[str, ...]:
    """One tool: its heading, its purpose, and its arguments."""
    blocks: list[str] = [
        render.heading(
            2,
            render.code(tool.name),
            anchor=_anchor(tool.name),
            purpose=f"What the `{tool.name}` tool does, and the arguments the assistant fills in to call it",
        ),
        purpose(tool),
    ]
    rows = parameter_rows(tool)
    if rows:
        blocks.append(render.table(_TABLE_HEADERS, rows))
    else:
        blocks.append(render.paragraph("This tool takes no arguments."))
    return tuple(blocks)


def tools_page(*, tools: Sequence[Tool] | None = None) -> str:
    """The whole page: the intro, the summary table, then one section per tool.

    *tools* defaults to the live registration; a test passes a perturbed surface.
    """
    resolved = registered_tools() if tools is None else tuple(tools)
    if not resolved:
        raise UnrenderableTool("<none>", "the server registers no tools, so there is no surface to describe")
    blocks: list[str] = [
        render.GENERATED_COMMENT,
        render.heading(
            1,
            "The assistant's tools",
            purpose="What the assistant can ask SWCraft to do, tool by tool, and what it cannot do to your files",
        ),
        render.GENERATED_NOTICE,
        render.paragraph(
            "The assistant does not edit your files. It asks SWCraft to do things, and the things it can ask "
            "for are exactly the ones below — there is no other way in. Reading this page is how you find out "
            "what the assistant is able to do for you, and what it is structurally unable to do to you."
        ),
        render.paragraph(
            "The first four are the whole editing surface. Three of them only answer questions; the fourth is "
            "the single way your model is ever written, and it writes a whole proposal at once or nothing at "
            "all, after you have seen the exact list of changes. The rest answer a question about the model "
            "or the guide, or run a check, a build, or an exchange with another tool. None of them touches "
            "a model file — but the ones that build or export do write the files SWCraft generates *from* "
            "the model, the AUTOSAR output among them, which is work you can always run again."
        ),
        render.paragraph(
            "They are listed in the order the server offers them, each with the arguments the assistant fills "
            "in when it calls one."
        ),
        render.table(
            ("Tool", "What it is for"),
            [(render.link(render.code(tool.name), f"#{_anchor(tool.name)}"), purpose(tool)) for tool in resolved],
        ),
    ]
    for tool in resolved:
        blocks.extend(_tool_section(tool))
    return render.document(*blocks)


def all_artifacts(*, tools: Sequence[Tool] | None = None) -> dict[str, str]:
    """The one page this module renders, keyed by its path relative to ``docs/guide/``."""
    return {TOOLS_PATH: tools_page(tools=tools)}


def main(argv: list[str] | None = None) -> int:
    """``python -m swcraft_mcp.tool_reference --output-dir DIR``.

    ``--output-dir`` is the guide's source tree, ``docs/guide``, because the
    artifact key is relative to it.

    There is deliberately no ``--check`` mode (the drift gate is a test) and no
    navigation rewrite: the page's ``mkdocs.yml`` row is committed and kept by
    the sibling generator, so two generators never write one ``nav`` block.
    """
    parser = argparse.ArgumentParser(
        prog="python -m swcraft_mcp.tool_reference",
        description="Generate the user guide's tool reference from the live tool registration.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="the guide's source tree (docs/guide); the page is written at its path relative to it",
    )
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    for relative, text in all_artifacts().items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the one line only `python -m ...` reaches
    raise SystemExit(main())
