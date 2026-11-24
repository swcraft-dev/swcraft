"""The ``import_component`` draft of a source without ``HANDLE-TERMINATION-AND-RESTART``.

``rule:swc.undeclared-termination-and-restart`` refuses, in pass D, a non-stub component that sets no
``handle_termination_and_restart``. For a source that states no value the draft creates the component without one
-- no value invented, nothing new said. The client appends an ``update`` of the field and the pair commits in one
change set; submitted as drafted, pass D refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import swcraft_mcp.server as server
import yaml
from lxml import etree
from protocol_client import ScriptedHuman

from tests.test_import_change_set import FOREIGN_SWC, _foreign_export, _plan, _submit, _target_project, _trace_link
from tests.test_review_mode import set_mode, state

FIELD = "handle_termination_and_restart"
TAG = "HANDLE-TERMINATION-AND-RESTART"


def _export_stating_none(root: Path) -> str:
    """``_foreign_export``'s export with the element removed, as a tool that omits it writes."""
    source = Path(_foreign_export(root))
    tree = etree.parse(str(source))
    removed = [element for element in tree.getroot().iter() if etree.QName(element).localname == TAG]
    assert removed, "the export this starts from states the value"
    for element in removed:
        element.getparent().remove(element)
    source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return str(source)


def _set_the_value() -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {"layer": "swc", "element": FOREIGN_SWC, "path": [{"field": FIELD}]},
        "value": "no_support",
    }


def test_the_draft_creates_the_component_without_a_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _target_project(tmp_path / "project", monkeypatch)
    source = _export_stating_none(tmp_path)

    result = _plan(project, source)

    (create,) = [
        operation
        for operation in result["change_set"]["operations"]
        if operation["operation"] == "create" and operation["address"] == {"layer": "swc", "element": FOREIGN_SWC}
    ]
    # The field stays at its null default: no value invented.
    assert create["value"][FIELD] is None

    # Nothing new said: the reply for the export stating the value differs only in that field.
    stating_source = _foreign_export(tmp_path / "stating")
    stating = _plan(project, stating_source)
    (stated_create,) = [
        operation
        for operation in stating["change_set"]["operations"]
        if operation["operation"] == "create" and operation["address"] == {"layer": "swc", "element": FOREIGN_SWC}
    ]
    assert stated_create["value"][FIELD] == "no_support"
    stated_create["value"][FIELD] = None
    # The report names its source file, which is the one other difference.
    assert json.dumps(stating, sort_keys=True).replace(json.dumps(stating_source)[1:-1], "<source>") == json.dumps(
        result, sort_keys=True
    ).replace(json.dumps(source)[1:-1], "<source>")


def test_submitted_as_drafted_it_is_refused_by_pass_d(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _target_project(tmp_path / "project", monkeypatch)
    source = _export_stating_none(tmp_path)
    set_mode(monkeypatch, server.REVIEW_ELICIT)
    draft = _plan(project, source)["change_set"]
    before = state(project)
    human = ScriptedHuman(True)

    response = _submit(draft, _trace_link(), human=human)

    assert response["error"] == "consistency-violation", response
    assert response["message"].startswith(f"SWC '{FOREIGN_SWC}' sets no handle_termination_and_restart"), response
    assert human.questions == [], "a change set refused before review must never reach a human"
    assert state(project) == before


def test_accepted_with_the_appended_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _target_project(tmp_path / "project", monkeypatch)
    source = _export_stating_none(tmp_path)
    set_mode(monkeypatch, server.REVIEW_ELICIT)
    draft = _plan(project, source)["change_set"]

    response = _submit(draft, _trace_link(), _set_the_value(), human=ScriptedHuman(True))

    assert response["status"] == "committed", response
    written = yaml.safe_load(
        (project / "model" / "swc" / "components" / f"{FOREIGN_SWC}.yaml").read_text(encoding="utf-8")
    )
    assert written[FIELD] == "no_support"
