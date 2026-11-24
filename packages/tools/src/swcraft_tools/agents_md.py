"""The project's ``AGENTS.md`` and Kiro's steering file: a fixed host preamble, then the agent profile's body.

Codex reads a project's ``AGENTS.md``, and Claude Code reads it through the project's
``CLAUDE.md`` (one ``@AGENTS.md`` import line). Both get the agent profile's body without its
frontmatter, preceded by :data:`HOST_PREAMBLE`, which corrects the one thing the body says that is
false for these hosts: that the agent has no file or shell tools of its own.

Kiro reads the same body from ``.kiro/steering/swcraft.md`` (:func:`render_kiro_steering`), behind
:data:`KIRO_PREAMBLE`, because Kiro cuts ``AGENTS.md`` at 50 000 characters and loses the profile's
schema section. The steering file carries no front matter, so Kiro always includes it and the marker
stays its first line.

The files are generated, never hand-copied: ``python scripts/dev.py docs-reference`` and
``python scripts/dev.py schemas`` run this module's ``main`` and write the template's and the
extension's copies.

The first line is the marker by which Onboard Project recognises a file of its own
(:data:`MARKDOWN_MARKER`; :data:`TOML_MARKER`, the same sentence as a ``#`` comment, in
``.codex/config.toml``). A file whose first line is no longer the marker is the user's, and Onboard
Project keeps it.

What it does not reach: whether a host reads the file as instructions the way they are meant; and a
profile without frontmatter is taken whole.
"""

from __future__ import annotations

import argparse
from pathlib import Path

__all__ = [
    "HOST_PREAMBLE",
    "KIRO_PREAMBLE",
    "MARKDOWN_MARKER",
    "MARKER_SENTENCE",
    "TOML_MARKER",
    "profile_body",
    "render_agents_md",
    "render_kiro_steering",
]

#: The sentence a file Onboard Project wrote opens with; its removal hands the file to the user.
MARKER_SENTENCE = (
    "Written by SWCraft's Onboard Project. Edit freely: once this line is gone, Onboard Project keeps the file."
)

#: The first line of the project's ``AGENTS.md`` and ``CLAUDE.md``.
MARKDOWN_MARKER = f"<!-- {MARKER_SENTENCE} -->"

#: The first line of the project's ``.codex/config.toml``.
TOML_MARKER = f"# {MARKER_SENTENCE}"

#: What Codex and Claude Code read before the profile's body, fixed text.
HOST_PREAMBLE = (
    "In Codex and Claude Code you also have file and shell tools. Use none of them on the model\n"
    "directory, `swcraft.yaml`, `.mcp.json`, `.codex/`, `.claude/`, `.github/hooks/`, `.kiro/hooks/`,\n"
    "`.kiro/settings/` or `.kiro/agents/`: the project's model guard denies them, and a denial is the\n"
    "boundary working. The SWCraft tools are the only way to read and change the model. Change nothing\n"
    "in the SWCraft installation folder (`~/.swcraft`) either: the user updates and reinstalls SWCraft.\n"
    "Ignore the `tools:` list and the `.github/hooks/` file named below: they are Copilot's.\n"
)

#: What Kiro reads before the profile's body in ``.kiro/steering/swcraft.md``, fixed text.
KIRO_PREAMBLE = (
    "In Kiro you also have file and shell tools. Use none of them on the model directory, and use\n"
    "none of them to change `swcraft.yaml`, `.kiro/hooks/`, `.kiro/settings/`, `.kiro/agents/`,\n"
    "`.mcp.json`, `.codex/`, `.claude/`, `.github/hooks/` or the SWCraft installation folder\n"
    "(`~/.swcraft`): the project's model guard denies it, and a denial is the boundary working.\n"
    "For a question about SWCraft itself — how to do something, or what a refusal, setting or command\n"
    "means — call SWCraft's `guide` first, before any file, search, web or documentation tool of Kiro's:\n"
    "Kiro's documentation does not describe SWCraft. The SWCraft tools are the only way to read and\n"
    "change the model; load each with `tool_load` before its first call.\n"
    "Ignore the `tools:` list and the `.github/hooks/` file named below: they are Copilot's.\n"
)


def profile_body(text: str) -> str:
    """*text*, an agent profile, with its ``---`` frontmatter and the blank lines after it dropped."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError("the profile opens a frontmatter block it never closes")
    return text[end + len("\n---\n") :].lstrip("\n")


def render_agents_md(profile_text: str) -> str:
    """The project's ``AGENTS.md`` for the agent profile *profile_text*: marker, preamble, body."""
    return f"{MARKDOWN_MARKER}\n\n{HOST_PREAMBLE}\n{profile_body(profile_text)}"


def render_kiro_steering(profile_text: str) -> str:
    """Kiro's ``.kiro/steering/swcraft.md`` for the agent profile *profile_text*: marker, Kiro preamble, body."""
    return f"{MARKDOWN_MARKER}\n\n{KIRO_PREAMBLE}\n{profile_body(profile_text)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Path, required=True, help="the template's agent profile")
    parser.add_argument(
        "--kiro-steering",
        type=Path,
        action="append",
        default=[],
        help="a Kiro steering file to write (repeatable)",
    )
    parser.add_argument("outputs", nargs="+", type=Path, help="the AGENTS.md files to write")
    args = parser.parse_args(argv)
    profile_text = args.profile.read_text(encoding="utf-8")
    agents_md = render_agents_md(profile_text)
    steering = render_kiro_steering(profile_text)
    for path, rendered in (
        *((path, agents_md) for path in args.outputs),
        *((path, steering) for path in args.kiro_steering),
    ):
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != rendered:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
