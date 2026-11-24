"""The additive byte gate of a runnable's budget.

The budget's standard place is one ``SWC-TIMING`` ``{swc}_Timing`` per component with a budgeted runnable, holding
one ``EXECUTION-TIME-CONSTRAINT`` ``{runnable}_Budget`` under ``TIMING-REQUIREMENTS``. Two halves, as in
``test_external_stubs_byte_identity.py``:

* **without a budget, against the record** (the independent half): ``examples/basic/model`` copied unchanged gives
  the ARXML at 4.2.2 and 4.3.0 that ``RECORDED`` holds (after ``termination_strip_support.strip_recorded``) and the
  diagrams' ``(file count, digest)`` of ``WITHOUT_THE_FEATURE``, measured before the budget was written;
* **with a budget, exactly one block more**: ``ExampleConsumerRun`` given ``wcet: 850ns`` and ``wcet_type: gross``
  gives the budget-free ARXML with :data:`SWC_TIMING_BLOCK` inserted right after
  ``ExampleConsumerSWC_Implementation``'s ``SWC-IMPLEMENTATION`` and nothing else; ``needs.json`` and the diagrams
  are unchanged.

**The block is a reviewed manual derivation**, never copied from the writer's output: its element order is the XSD
content models' -- ``SWC-TIMING`` = ``SHORT-NAME`` (group ``REFERRABLE``, 4.2.2 :52626, 4.3.0 :59749),
``TIMING-REQUIREMENTS`` (group ``TIMING-EXTENSION``), ``BEHAVIOR-REF`` (group ``SWC-TIMING``, last);
``EXECUTION-TIME-CONSTRAINT`` = ``SHORT-NAME``, ``EXECUTABLE-REF``, ``EXECUTION-TIME-TYPE``, ``MAXIMUM``;
``MAXIMUM`` = ``CSE-CODE``, ``CSE-CODE-FACTOR`` (4.2.2 :61437, :29604, :45904; 4.3.0 :70828, :35116, :52526) --
its ``DEST`` values are the enumerations' (``RUNNABLE-ENTITY``, ``SWC-INTERNAL-BEHAVIOR``), and its ``CSE-CODE``
``26`` is 1 ns in TPS GST R4.3.0 Table 4.92 (R4.2.2 ``[TPS_GST_00354]``), the factor the integer as written. A
premise test asserts, off the copied file, that the copy carries a runnable ``wcet``, so neither half is inert.

**What it does not reach:** one tree and one runnable (the other shapes are ``test_swc_timing_writer.py``'s); the
generators reached through the CLI and ``validate``; ``generate arxml --component``; the importer; and the field's
other consumers, which their own suites witness.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs
from termination_strip_support import strip_recorded
from test_application_types_byte_identity import WITHOUT_THE_FEATURE
from test_diagram_switch_byte_identity import RECORDED
from test_external_stubs_byte_identity import PROJECT_NAME, _diagram_digest

#: The recorded tree the copy is taken from, by its path from the repository root.
TREE = "examples/basic/model"

#: The component file, the runnable given a budget, and the budget. The runnable is mapped to
#: ``Task_10ms`` (``wcet: 1200us``), so the budget stays below its task's and the model stays valid
#: under ``rule:task.runnable-cost-exceeds-task``.
SWC_FILE = Path("swc") / "example" / "ExampleConsumerSWC.yaml"
RUNNABLE = "ExampleConsumerRun"
WCET = "850ns"

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_ANCHOR = f"  - name: {RUNNABLE}\n"

#: Where the block goes: right after the component's ``SWC-IMPLEMENTATION``.
IMPLEMENTATION_END = (
    '          <BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR">'
    "/ComponentTypes/ExampleConsumerSWC/ExampleConsumerSWC_InternalBehavior</BEHAVIOR-REF>\n"
    "        </SWC-IMPLEMENTATION>\n"
)

#: The reviewed manual derivation (module docstring): typed from the XSD content models and TPS
#: GST Table 4.92, identical at 4.2.2 and 4.3.0.
SWC_TIMING_BLOCK = (
    "        <SWC-TIMING>\n"
    "          <SHORT-NAME>ExampleConsumerSWC_Timing</SHORT-NAME>\n"
    "          <TIMING-REQUIREMENTS>\n"
    "            <EXECUTION-TIME-CONSTRAINT>\n"
    "              <SHORT-NAME>ExampleConsumerRun_Budget</SHORT-NAME>\n"
    '              <EXECUTABLE-REF DEST="RUNNABLE-ENTITY">'
    "/ComponentTypes/ExampleConsumerSWC/ExampleConsumerSWC_InternalBehavior/ExampleConsumerRun"
    "</EXECUTABLE-REF>\n"
    "              <EXECUTION-TIME-TYPE>GROSS</EXECUTION-TIME-TYPE>\n"
    "              <MAXIMUM>\n"
    "                <CSE-CODE>26</CSE-CODE>\n"
    "                <CSE-CODE-FACTOR>850</CSE-CODE-FACTOR>\n"
    "              </MAXIMUM>\n"
    "            </EXECUTION-TIME-CONSTRAINT>\n"
    "          </TIMING-REQUIREMENTS>\n"
    '          <BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR">'
    "/ComponentTypes/ExampleConsumerSWC/ExampleConsumerSWC_InternalBehavior</BEHAVIOR-REF>\n"
    "        </SWC-TIMING>\n"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy(destination: Path, *, with_cost: bool) -> Path:
    """Copy the recorded tree to *destination*; with *with_cost*, give the runnable its ``wcet``."""
    shutil.copytree(REPO_ROOT / TREE, destination)
    if with_cost:
        path = destination / SWC_FILE
        text = path.read_text(encoding="utf-8")
        assert text.count(_ANCHOR) == 1, f"{SWC_FILE.as_posix()} no longer declares {RUNNABLE} as the edit expects"
        path.write_text(
            text.replace(_ANCHOR, f"{_ANCHOR}    wcet: {WCET}\n    wcet_type: gross\n"), encoding="utf-8", newline="\n"
        )
    return destination


def _generate(model_root: Path, artifact: str, output: Path) -> None:
    if artifact == "needs":
        generate_needs(str(model_root), str(output), PROJECT_NAME)
    else:
        generate_arxml(str(model_root), str(output), version=artifact.removeprefix("arxml-"))


def _diagram_files(model_root: Path, views_root: Path) -> dict[str, bytes]:
    generate_diagrams(str(model_root), str(views_root), PROJECT_NAME, node=requires_node())
    return {entry.name: entry.read_bytes() for entry in sorted(views_root.iterdir(), key=lambda p: p.name)}


def test_the_copy_carries_a_runnable_cost(tmp_path: Path) -> None:
    """The premise, read off the copied file: the runnable carries ``wcet``, the original does not."""
    copy = _copy(tmp_path / "model", with_cost=True)
    copied = yaml.safe_load((copy / SWC_FILE).read_text(encoding="utf-8"))
    original = yaml.safe_load((REPO_ROOT / TREE / SWC_FILE).read_text(encoding="utf-8"))

    assert [r.get("wcet") for r in copied["runnables"] if r["name"] == RUNNABLE] == [WCET]
    assert all("wcet" not in r for r in original["runnables"])


@pytest.mark.parametrize("index", range(len(_ARTIFACTS)), ids=list(_ARTIFACTS))
def test_without_a_budget_the_arxml_is_the_branch_points(index: int, tmp_path: Path) -> None:
    artifact = _ARTIFACTS[index]
    copy = _copy(tmp_path / "model", with_cost=False)
    output = tmp_path / artifact
    _generate(copy, artifact, output)

    assert _sha(strip_recorded(TREE, artifact, output.read_bytes())) == RECORDED[TREE][index]


def test_without_a_budget_the_diagrams_are_the_branch_points(tmp_path: Path) -> None:
    copy = _copy(tmp_path / "model", with_cost=False)

    assert _diagram_digest(copy, tmp_path / "views") == WITHOUT_THE_FEATURE[TREE][3:]


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_a_budget_adds_exactly_the_hand_written_block(version: str, tmp_path: Path) -> None:
    """The budgeted ARXML is the budget-free one plus :data:`SWC_TIMING_BLOCK`, after the implementation."""
    artifact = f"arxml-{version}"
    without = _copy(tmp_path / "without", with_cost=False)
    with_cost = _copy(tmp_path / "with", with_cost=True)
    _generate(without, artifact, tmp_path / "without.arxml")
    _generate(with_cost, artifact, tmp_path / "with.arxml")
    expected_base = (tmp_path / "without.arxml").read_text(encoding="utf-8")
    produced = (tmp_path / "with.arxml").read_text(encoding="utf-8")

    assert expected_base.count(IMPLEMENTATION_END) == 1, "the budget-free ARXML no longer ends the implementation so"
    assert produced == expected_base.replace(IMPLEMENTATION_END, IMPLEMENTATION_END + SWC_TIMING_BLOCK)


@pytest.mark.parametrize("artifact", ["needs"])
def test_the_needs_json_does_not_move_with_the_budget(artifact: str, tmp_path: Path) -> None:
    without = _copy(tmp_path / "without", with_cost=False)
    with_cost = _copy(tmp_path / "with", with_cost=True)
    _generate(without, artifact, tmp_path / f"without-{artifact}")
    _generate(with_cost, artifact, tmp_path / f"with-{artifact}")

    assert (tmp_path / f"with-{artifact}").read_bytes() == (tmp_path / f"without-{artifact}").read_bytes()


def test_the_diagrams_do_not_move_with_the_budget(tmp_path: Path) -> None:
    without = _copy(tmp_path / "without", with_cost=False)
    with_cost = _copy(tmp_path / "with", with_cost=True)

    expected = _diagram_files(without, tmp_path / "without-views")
    assert expected, "the generator wrote no diagram file"
    assert _diagram_files(with_cost, tmp_path / "with-views") == expected
