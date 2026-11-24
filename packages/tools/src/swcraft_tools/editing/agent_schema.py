"""The model schema as a compact section of the agent profile.

``discover``'s schema answer is a function of the contract, not of the project,
so an agent host can hold it in its profile and spend its ``discover`` call on
the baseline token alone (``form: "baseline"``).

This module renders the registry (:func:`swcraft_tools.editing.registry.or_current`)
as one line per field and rewrites the section between :data:`BEGIN_MARKER` and
:data:`END_MARKER` of each profile it is given. The line keeps what authoring
needs: the type, ``req`` and ``imm`` where true, the unit, the enum members, the
layer a reference points into and how a collection member is identified.
``python scripts/dev.py schemas`` runs it, and ``test_agent_schema_drift.py``
compares every committed section with a fresh rendering.

What it does not reach: the rules ``check-model`` applies across elements (a
mapping set a port's type needs, a memory budget a ``strict`` pool demands);
those still answer as a ``consistency-violation`` when a change set breaks one.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from swcraft_tools.editing import registry as reg

__all__ = ["BEGIN_MARKER", "END_MARKER", "render_section", "rewrite_profile"]

BEGIN_MARKER = "<!-- BEGIN GENERATED: model schema -->"
END_MARKER = "<!-- END GENERATED: model schema -->"

#: An enum this long that more than one field declares is printed once, under
#: an alias, rather than on every field that uses it.
_ALIAS_MIN_MEMBERS = 6


def render_section(registry: reg.Registry | None = None) -> str:
    """The generated section, markers included, ending in a newline."""
    registry = reg.or_current(registry)
    aliases = _enum_aliases(registry)
    lines = [
        BEGIN_MARKER,
        "",
        "## The model schema",
        "",
        "Every layer, element kind and field `discover` publishes, one field per line:",
        "`field: type`, then `req` (required) and `imm` (immutable) where true, the unit and",
        "`-> layer` for a reference. A bare field name is an optional string. A list's member",
        "is addressed `{member: <name>}` where the line says `by name`, `{member: {a, b}}`",
        "where it says `by (a, b)`, and by its whole value otherwise; a map's member is",
        "`{key: <key>}`. A kind marked `in owner.field` lives inside its owner and is",
        "addressed through that field. A layer's `discriminator` is the field whose value",
        "names the kind.",
        "",
        "```text",
    ]
    lines.extend(f"{alias} = {'|'.join(map(str, members))}" for members, alias in aliases.items())
    for layer in registry.layers:
        lines.append("")
        heading = f"layer {layer.name} (identity {layer.identity_field}"
        top_discriminators = {kind.discriminator_field for kind in layer.element_kinds if not kind.member_only}
        shared = next(iter(top_discriminators)) if len(top_discriminators) == 1 else None
        lines.append(heading + (f", discriminator {shared})" if shared else ")"))
        for kind in layer.element_kinds:
            heading = f" kind {kind.kind}"
            if kind.member_only:
                heading += " in " + ", ".join(f"{owner}.{field}" for owner, field in kind.member_of)
            if kind.discriminator_field and (kind.member_only or not shared):
                heading += f", discriminator {kind.discriminator_field}"
            lines.append(heading)
            lines.extend(f"  {_field_line(field, aliases)}" for field in kind.fields)
    lines.append("```")
    lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _enum_aliases(registry: reg.Registry) -> dict[tuple, str]:
    """``{members: alias}`` for each long enum more than one field declares, named after its first field."""
    counts: Counter[tuple] = Counter()
    first_field: dict[tuple, str] = {}
    for layer in registry.layers:
        for kind in layer.element_kinds:
            for field in kind.fields:
                if len(field.enum) >= _ALIAS_MIN_MEMBERS:
                    counts[field.enum] += 1
                    first_field.setdefault(field.enum, field.name)
    aliases: dict[tuple, str] = {}
    for members, count in counts.items():
        if count > 1:
            alias = first_field[members].upper()
            while alias in aliases.values():
                alias += "_"
            aliases[members] = alias
    return aliases


def _field_line(field: reg.FieldDescriptor, aliases: dict[tuple, str]) -> str:
    """One field as its profile line: ``name: type`` and the notes authoring needs.

    A plain optional string with no notes is its bare name, the most common
    field written in the fewest bytes.
    """
    if field.type == "string" and not (
        field.required or field.immutable or field.unit or field.enum or field.reference or field.collection
    ):
        return field.name
    if field.enum:
        type_text = aliases.get(field.enum) or "|".join(map(str, field.enum))
    else:
        type_text = field.type
    parts = [f"{field.name}: {type_text}"]
    if field.required:
        parts.append("req")
    if field.immutable:
        parts.append("imm")
    if field.unit:
        parts.append(field.unit)
    if field.reference is not None:
        target = field.reference.layer
        if field.reference.identity_field != "name":
            target += f".{field.reference.identity_field}"
        parts.append(f"-> {target}")
    if field.collection is not None:
        parts.append(_collection_note(field.collection))
    return " ".join(part for part in parts if part)


def _collection_note(collection: reg.CollectionDescriptor) -> str:
    """How a collection's member is identified: ``by field``, ``by (fields)``, its discriminator, or nothing.

    A map is keyed and needs no note; a whole-value collection (a list of names)
    is identified by its value and needs none either.
    """
    if collection.kind == "map":
        return ""
    if collection.member_identity == "field":
        return f"by {collection.identity_field}"
    if collection.member_identity == "composite":
        return f"by ({', '.join(collection.identity_fields)})"
    if collection.discriminator_field:
        return f"discriminator {collection.discriminator_field}"
    return ""


def rewrite_profile(text: str, section: str) -> str:
    """*text* with the section between the markers replaced by *section*.

    Raises:
        ValueError: *text* does not carry both markers, in order, once each.
    """
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin < 0 or end < begin or text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError("the profile does not carry the generated-section markers once each, in order")
    tail = end + len(END_MARKER)
    if text[tail : tail + 1] == "\n":
        tail += 1
    return text[:begin] + section + text[tail:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profiles", nargs="+", type=Path, help="agent profiles carrying the generated-section markers")
    args = parser.parse_args(argv)
    section = render_section(registry=reg.current())
    for path in args.profiles:
        text = path.read_text(encoding="utf-8")
        updated = rewrite_profile(text, section)
        # Silent: `dev.py` labels the step.
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
