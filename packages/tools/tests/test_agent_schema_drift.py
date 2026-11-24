"""The drift gate for the agent profiles' generated model schema section.

Each profile in ``AGENT_PROFILES`` carries the committed section
:func:`swcraft_tools.editing.agent_schema.render_section` renders from the registry; this module compares
it with a fresh rendering and names the command that repairs drift.

What it does not reach: whether an agent reads the section the way it is meant to.
"""

from __future__ import annotations

import pytest
from conftest import REPO_ROOT, dev_module
from swcraft_tools import __version__
from swcraft_tools.editing import agent_schema
from swcraft_tools.editing import registry as reg

#: Read from ``AGENT_PROFILES`` rather than restated, so a profile added there is checked here too.
PROFILES = tuple(REPO_ROOT / profile for profile in dev_module().AGENT_PROFILES)

REGENERATE = "regenerate with `python scripts/dev.py schemas` and commit the result"

#: The generated section's size in UTF-8 bytes, markers included (contract
#: ``12.7.0``: fifteen layers, 67 kinds, 359 field lines). A contract change that
#: adds kinds or fields moves it.
RECORDED_SCHEMA_SECTION_BYTES = 12_647
#: The headroom above the record: one median kind block (145 bytes over the 67 kinds), so the ceiling
#: is ``int(12 647 × 1.0115)`` = 12 792 bytes. One more kind of median width fits; a second fires it.
SCHEMA_SECTION_MARGIN = 0.0115


@pytest.mark.parametrize("profile", PROFILES, ids=lambda path: path.relative_to(REPO_ROOT).parts[0])
def test_the_committed_section_is_the_rendered_one(profile) -> None:
    text = profile.read_text(encoding="utf-8")
    assert agent_schema.rewrite_profile(text, agent_schema.render_section()) == text, (
        f"{profile.relative_to(REPO_ROOT).as_posix()}: the model schema section has drifted — {REGENERATE}"
    )


def test_the_section_names_every_layer_and_no_version() -> None:
    from swcraft_tools.editing import registry as reg

    section = agent_schema.render_section()
    for name in reg.or_current(None).layer_names:
        assert f"\nlayer {name} (" in section
    assert __version__ not in section


def test_what_the_host_receives_of_the_contract_stays_under_its_ceiling() -> None:
    """The tripwire on the size of what the host receives of the contract, read once per chat.

    Moving either constant is a decision to take, never a number to update. What it does not reach:
    tokens rather than bytes, the profile's prose outside the markers, and whether an agent reads it well.
    """
    size = len(agent_schema.render_section().encode("utf-8"))
    ceiling = int(RECORDED_SCHEMA_SECTION_BYTES * (1 + SCHEMA_SECTION_MARGIN))
    assert ceiling == 12_792, "the arithmetic beside SCHEMA_SECTION_MARGIN quotes a ceiling of 12 792 bytes"
    assert size <= ceiling, (
        f"the profile's generated schema section measures {size} bytes, above its {ceiling}-byte ceiling "
        f"({RECORDED_SCHEMA_SECTION_BYTES} recorded × (1 + {SCHEMA_SECTION_MARGIN})). The contract grew by about "
        "a layer's worth of fields, and every chat's profile carries it. Re-measure, then either narrow what a "
        "field line states or move the record and the margin deliberately, with the arithmetic beside them."
    )


def test_a_profile_without_the_markers_is_refused() -> None:
    with pytest.raises(ValueError, match="markers"):
        agent_schema.rewrite_profile("no markers here\n", agent_schema.render_section())


def _section_lines() -> dict[tuple[str, str], dict[str, list[str]]]:
    """``{(layer, kind): {field: tokens}}`` read back off the rendered section."""
    lines: dict[tuple[str, str], dict[str, list[str]]] = {}
    layer = kind = ""
    for line in agent_schema.render_section().splitlines():
        if line.startswith("layer "):
            layer = line.split()[1]
        elif line.startswith(" kind "):
            kind = line.split()[1].rstrip(",")
            lines[(layer, kind)] = {}
        elif line.startswith("  ") and layer and kind:
            tokens = line.split()
            lines[(layer, kind)][tokens[0].rstrip(":")] = tokens[1:]
    return lines


def test_every_field_line_states_what_its_descriptor_declares() -> None:
    """Each field's line, read back, says what the registry declares — and nothing it does not.

    What it does not reach: the unit's spelling beyond its presence as a token, and ``full``'s prose.
    """
    registry = reg.or_current(None)
    aliases = {alias: members for members, alias in agent_schema._enum_aliases(registry).items()}
    rendered = _section_lines()
    checked = 0
    for layer in registry.layers:
        for kind in layer.element_kinds:
            fields = rendered[(layer.name, kind.kind)]
            assert list(fields) == [field.name for field in kind.fields], (layer.name, kind.kind)
            for field in kind.fields:
                tokens = fields[field.name]
                where = f"{layer.name}.{kind.kind}.{field.name}"
                assert ("req" in tokens) == field.required, where
                assert ("imm" in tokens) == field.immutable, where
                if field.reference is not None:
                    assert tokens[tokens.index("->") + 1].split(".")[0] == field.reference.layer, where
                else:
                    assert "->" not in tokens, where
                if field.enum:
                    stated = tokens[0]
                    members = aliases.get(stated) or tuple(stated.split("|"))
                    assert tuple(map(str, members)) == tuple(map(str, field.enum)), where
                elif tokens:
                    assert " ".join(tokens).startswith(field.type), where
                collection = field.collection
                if collection is not None and collection.kind != "map" and collection.member_identity == "field":
                    assert tokens[-2:] == ["by", collection.identity_field], where
                checked += 1
    assert checked == sum(len(kind.fields) for layer in registry.layers for kind in layer.element_kinds)


@pytest.mark.parametrize(
    "text",
    [
        f"{agent_schema.BEGIN_MARKER}\n{agent_schema.BEGIN_MARKER}\n{agent_schema.END_MARKER}\n",
        f"{agent_schema.BEGIN_MARKER}\n{agent_schema.END_MARKER}\n{agent_schema.END_MARKER}\n",
        f"{agent_schema.END_MARKER}\n{agent_schema.BEGIN_MARKER}\n",
        f"{agent_schema.BEGIN_MARKER}\nno end\n",
    ],
    ids=["begin-twice", "end-twice", "out-of-order", "no-end"],
)
def test_a_profile_whose_markers_are_not_once_each_in_order_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="markers"):
        agent_schema.rewrite_profile(text, agent_schema.render_section())


def test_the_rewrite_keeps_the_text_around_the_section() -> None:
    profile = f"before\n{agent_schema.BEGIN_MARKER}\nstale\n{agent_schema.END_MARKER}\nafter\n"
    section = agent_schema.render_section()
    assert agent_schema.rewrite_profile(profile, section) == f"before\n{section}after\n"
