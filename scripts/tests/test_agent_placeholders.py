"""``scripts/agent_placeholders.py`` finds and fills ``{{name}}`` placeholders at any depth.

Not covered here: a binding's choice of values, and the question set's use of the form.
"""

from __future__ import annotations

from types import ModuleType

import pytest
from conftest import load_repo_script

PLACEHOLDERS_MODULE = "swcraft_agent_placeholders"


@pytest.fixture(scope="module")
def placeholders() -> ModuleType:
    return load_repo_script(PLACEHOLDERS_MODULE, "scripts/agent_placeholders.py")


_TEMPLATE = {
    "prompt": "Who reads {{element}} of {{interface}}?",
    "requests": [{"address": {"name": "{{interface}}"}, "depth": 1}, "{{level}}"],
    "count": 3,
    "flag": None,
}


def test_every_name_is_found_at_any_depth(placeholders: ModuleType) -> None:
    assert placeholders.placeholders_in(_TEMPLATE) == {"element", "interface", "level"}


@pytest.mark.parametrize(
    "text",
    ["{{Interface}}", "{interface}", "{{ interface }}", "{{inter-face}}", "{{1}}", ""],
    ids=["upper-case", "single-braces", "spaces", "hyphen", "digit", "empty"],
)
def test_a_text_outside_the_form_names_no_placeholder(placeholders: ModuleType, text: str) -> None:
    assert placeholders.placeholders_in(text) == set()


def test_fill_replaces_every_name_and_keeps_the_rest(placeholders: ModuleType) -> None:
    values = {"element": "value", "interface": "IExample", "level": "B"}
    assert placeholders.fill(_TEMPLATE, values) == {
        "prompt": "Who reads value of IExample?",
        "requests": [{"address": {"name": "IExample"}, "depth": 1}, "B"],
        "count": 3,
        "flag": None,
    }


def test_fill_leaves_the_template_unchanged(placeholders: ModuleType) -> None:
    before = repr(_TEMPLATE)
    placeholders.fill(_TEMPLATE, {"element": "e", "interface": "i", "level": "l"})
    assert repr(_TEMPLATE) == before


def test_fill_does_not_touch_keys(placeholders: ModuleType) -> None:
    assert placeholders.fill({"{{name}}": "{{name}}"}, {"name": "X"}) == {"{{name}}": "X"}


def test_a_name_without_a_value_raises_key_error(placeholders: ModuleType) -> None:
    with pytest.raises(KeyError, match="element"):
        placeholders.fill("{{element}}", {})
