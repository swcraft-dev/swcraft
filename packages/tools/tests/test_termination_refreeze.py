"""The termination handling re-freeze is gated, not trusted: every moved byte is one line per component.

SWCraft refuses a component whose behaviour is emitted and that sets no
``handle_termination_and_restart`` (``rule:swc.undeclared-termination-and-restart``), so every
component of the repository's own model trees now declares ``no_support`` -- a value of the
standard's own enumeration, and one that claims no capability. The writer emits one
``<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>`` line into each
one's ``SWC-INTERNAL-BEHAVIOR``, after ``EVENTS``, which moves the two populated ARXML goldens and
every byte-identity record over those trees.

**How the re-freeze is checked.** Each re-frozen golden is mapped *back* by
``termination_strip_support.strip_added_termination``: exactly that line is removed from each named
component's behaviour -- a text substitution on the line, never an XML re-serialization, so every
other byte is the writer's own -- and the result must hash to the digest the golden had **before**
the change (the golden README's *The termination handling regeneration*). The number of removed
lines must equal the number of components the model declares, counted from its YAML. Together they
say the change moved exactly one line per component and nothing else. The byte-identity records
route through the same helper and keep their recorded digests.

**What it does not reach.** It is evidence over the committed trees, not a proof over every model;
it says which bytes moved, never that ``NO-SUPPORT`` is the right value for a given component --
that is each model's statement, and the standard's multiplicity is the rule's
(``rule:swc.undeclared-termination-and-restart``).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT,
    REPO_ROOT,
    is_data_fixture_tree,
)
from fidelity_strip_support import strip_fidelity
from swcraft_tools.reader import read_swc_model
from swcraft_tools.writer import generate_arxml
from termination_strip_support import TERMINATION_ADDED, TerminationStripError, strip_added_termination

GOLDEN_DIR = REPO_ROOT / "packages/tools/tests/fixtures/arxml_golden"

#: The SHA-256 each populated golden had before the re-freeze; fixed, nothing later moves it.
PRE_CHANGE: dict[str, str] = {
    "basic-4.2.2": "83929b4cef3a626d99a30f916a447ab19bb36cce7ddd39ea044774d2404e1564",
    "composite-4.2.2": "8d1ff2fb9f850c063623d66ef85cfc0c1b0181aa2606be3efb4fe900956f0a4f",
}

LINE = b"<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>"


def _tree_of(golden: str) -> str:
    return Path(GOLDEN_MODEL_ROOTS[golden]).resolve().relative_to(REPO_ROOT).as_posix()


def _behaviour_components(model_root: Path) -> list[str]:
    """The components whose behaviour the writer emits: every non-external one, from the YAML."""
    return sorted(swc.name for swc in read_swc_model(str(model_root)) if not swc.external)


def _declaring(model_root: Path, value: str) -> set[str]:
    """The components of *model_root* whose file declares *value*, read off the files."""
    found = set()
    for path in (model_root / "swc").rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if data.get("handle_termination_and_restart") == value:
            found.add(data["name"])
    return found


# ---------------------------------------------------------------------------
# The two goldens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("golden", sorted(PRE_CHANGE))
def test_the_golden_strips_to_its_pre_change_digest(golden: str) -> None:
    tree = _tree_of(golden)
    # The writer-fidelity changes came after this re-freeze: their harness maps them back first
    # (``fidelity_strip_support``, gated by ``test_writer_fidelity_refreeze.py``), the order
    # ``termination_strip_support.strip_recorded`` chains the two in.
    data = strip_fidelity((GOLDEN_DIR / f"{golden}.arxml").read_bytes())
    components = _behaviour_components(REPO_ROOT / tree)

    stripped = strip_added_termination(data, components)

    assert data.count(LINE) == len(components) == len(TERMINATION_ADDED[tree])
    assert sorted(TERMINATION_ADDED[tree]) == components
    assert len(data.splitlines()) - len(stripped.splitlines()) == len(components)
    assert hashlib.sha256(stripped).hexdigest() == PRE_CHANGE[golden]


@pytest.mark.parametrize("golden", sorted(PRE_CHANGE))
def test_the_golden_is_what_the_writer_emits(golden: str, tmp_path: Path) -> None:
    """The re-frozen bytes are the writer's own, regenerated through ``generate_arxml()``."""
    output = tmp_path / "model.arxml"
    generate_arxml(GOLDEN_MODEL_ROOTS[golden], str(output))
    assert output.read_bytes() == (GOLDEN_DIR / f"{golden}.arxml").read_bytes()


def test_the_empty_golden_is_not_touched() -> None:
    assert LINE not in (GOLDEN_DIR / "empty-4.2.2.arxml").read_bytes()


# ---------------------------------------------------------------------------
# The record of which components were given the value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tree", sorted(TERMINATION_ADDED))
def test_each_named_set_is_the_trees_no_support_components(tree: str) -> None:
    model_root = REPO_ROOT / tree
    assert TERMINATION_ADDED[tree] == _declaring(model_root, "no_support")
    assert set(_behaviour_components(model_root)) <= _declaring(model_root, "no_support")


def test_every_committed_tree_with_an_added_value_is_named() -> None:
    """A tree whose components declare ``no_support`` is in the record, but for those that own theirs.

    A tree is recognised by its ``swc/`` layer directory. ``fixtures/handle_termination_and_restart``
    declares one value of each kind on purpose, and its lines are its own record's bytes, so it is
    never stripped. ``fixtures/editor_schema/accepted_but_invalid`` got the value too (each of its
    components keeps the one finding it is about), but no test generates ARXML from it, so no record
    strips it. The ``init`` template is not searched: it declared a value on every component before
    the re-freeze.
    The trees under ``conftest.DATA_FIXTURE_ROOTS`` (other tests' data) are left out.
    """
    bases = (REPO_ROOT / "examples", REPO_ROOT / "packages" / "tools" / "tests" / "fixtures")
    trees = {
        path.parent.relative_to(REPO_ROOT).as_posix()
        for base in bases
        for path in base.rglob("swc")
        if path.is_dir() and any(path.rglob("*.yaml"))
    }
    trees = {tree for tree in trees if not is_data_fixture_tree(tree)}
    owned = {
        HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT.resolve().relative_to(REPO_ROOT).as_posix(),
        "packages/tools/tests/fixtures/editor_schema/accepted_but_invalid",
        # Written after the re-freeze with the value their components declare: no record strips
        # them, and each one's output is held by its own test (test_data_description_writer.py,
        # test_inter_runnable_variable_arxml.py, test_exclusive_area_arxml.py, test_data_event_arxml.py,
        # test_access_forms_arxml.py; the diagram collisions by test_diagram_generation.py and
        # test_diagram_page_script.py, which read its diagram pages and never its ARXML).
        "packages/tools/tests/fixtures/data_description/model",
        "packages/tools/tests/fixtures/inter_runnable_variables/model",
        "packages/tools/tests/fixtures/exclusive_areas/model",
        "packages/tools/tests/fixtures/data_events/model",
        "packages/tools/tests/fixtures/access_forms/model",
        "packages/tools/tests/fixtures/diagram_collisions/model",
    }
    assert owned <= trees
    declaring = {tree for tree in trees - owned if _declaring(REPO_ROOT / tree, "no_support")}
    assert declaring == set(TERMINATION_ADDED)


# ---------------------------------------------------------------------------
# The helper's own tests
# ---------------------------------------------------------------------------


def _behavior(name: str, line: str | None, *, extra: str | None = None) -> str:
    lines = [
        "        <SWC-INTERNAL-BEHAVIOR>",
        f"          <SHORT-NAME>{name}_InternalBehavior</SHORT-NAME>",
        "          <EVENTS/>",
    ]
    if line is not None:
        lines.append(f"          <HANDLE-TERMINATION-AND-RESTART>{line}</HANDLE-TERMINATION-AND-RESTART>")
    if extra is not None:
        lines.append(f"          <HANDLE-TERMINATION-AND-RESTART>{extra}</HANDLE-TERMINATION-AND-RESTART>")
    lines += ["          <PORT-API-OPTIONS/>", "        </SWC-INTERNAL-BEHAVIOR>"]
    return "\n".join(lines) + "\n"


def _document(*behaviors: str) -> bytes:
    return ("<AUTOSAR>\n" + "".join(behaviors) + "</AUTOSAR>\n").encode()


class TestTheHelper:
    def test_it_removes_exactly_one_line_per_named_component(self) -> None:
        document = _document(_behavior("A", "NO-SUPPORT"), _behavior("B", "NO-SUPPORT"), _behavior("C", "NO-SUPPORT"))
        stripped = strip_added_termination(document, ["A", "C"])
        assert stripped == _document(_behavior("A", None), _behavior("B", "NO-SUPPORT"), _behavior("C", None))

    def test_it_fails_when_a_named_component_carries_no_line(self) -> None:
        with pytest.raises(TerminationStripError, match="A_InternalBehavior carries 0"):
            strip_added_termination(_document(_behavior("A", None)), ["A"])

    def test_it_fails_when_a_named_component_carries_two_lines(self) -> None:
        document = _document(_behavior("A", "NO-SUPPORT", extra="NO-SUPPORT"))
        with pytest.raises(TerminationStripError, match="A_InternalBehavior carries 2"):
            strip_added_termination(document, ["A"])

    def test_it_fails_when_a_named_component_has_no_behaviour(self) -> None:
        with pytest.raises(TerminationStripError, match="no SWC-INTERNAL-BEHAVIOR named Z_InternalBehavior"):
            strip_added_termination(_document(_behavior("A", "NO-SUPPORT")), ["A", "Z"])

    def test_it_fails_when_the_named_components_line_is_another_value(self) -> None:
        with pytest.raises(TerminationStripError, match="not NO-SUPPORT"):
            strip_added_termination(_document(_behavior("A", "CAN-BE-TERMINATED")), ["A"])

    def test_an_empty_selection_changes_no_byte(self) -> None:
        document = _document(_behavior("A", "NO-SUPPORT"))
        assert strip_added_termination(document, []) == document

    def test_it_leaves_the_declared_fixtures_lines_alone(self, tmp_path: Path) -> None:
        """``fixtures/handle_termination_and_restart`` keeps the three values it declares on purpose.

        The fixture's ``SilentSwc`` declares none (it is the absence's fixture); the scratch copy
        gives it ``no_support`` the way the re-freeze gave every other component one, and the
        helper -- told to strip ``SilentSwc`` alone -- removes that line and keeps the other three,
        ``NoSupportSwc``'s ``NO-SUPPORT`` among them.
        """
        model_root = tmp_path / "model"
        shutil.copytree(HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT, model_root)
        silent = next((model_root / "swc").rglob("SilentSwc.yaml"))
        silent.write_text(
            silent.read_text(encoding="utf-8") + "handle_termination_and_restart: no_support\n", encoding="utf-8"
        )
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        data = output.read_bytes()

        stripped = strip_added_termination(data, ["SilentSwc"])

        assert len(data.splitlines()) - len(stripped.splitlines()) == 1
        assert stripped.count(b"<HANDLE-TERMINATION-AND-RESTART>") == 3
        for literal in (b"NO-SUPPORT", b"CAN-BE-TERMINATED", b"CAN-BE-TERMINATED-AND-RESTARTED"):
            assert b"<HANDLE-TERMINATION-AND-RESTART>" + literal + b"</" in stripped
        with pytest.raises(TerminationStripError):
            strip_added_termination(stripped, ["SilentSwc"])
