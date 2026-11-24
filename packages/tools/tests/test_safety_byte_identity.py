"""The byte-identity gate: the safety fields and a requirement's provenance reach no ARXML.

The AUTOSAR Classic component template carries no ASIL attribute, and the one standardized spelling is defined by
a template marked obsolete in its own last edition, so every `asil` field stays out of the ARXML writer and reader.

A model declaring the fields on every element that can hold one is compared with the same model stripped of them:
the document is byte-identical at 4.2.2 and 4.3.0, and every diagram file is compared in its place. Two exceptions
are asserted as difference sets, so a second difference fails:

* **the `deployment` view** labels a core with its `asil_capability` and a component with its effective level, so
  exactly that view's two files and its size lines move (:func:`_assert_only_the_deployment_levels_moved`);
* **`generate needs`** writes an `asil` key on an architecture requirement declaring a non-`QM` level.

`trees` keeps what `all_five_trees` cannot state: the word ASIL is absent from the document, and a declared `QM`
writes nothing even in `needs.json`. `coexistence_trees` is the acceptance fixture.

**What this gate does not reach**: the model files (`test_writer_elements.py`), and an edited writer whose bytes
happened not to move.
"""

from __future__ import annotations

import difflib
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import GOLDEN_MODEL_ROOTS, SAFETY_MODEL_ROOT
from lxml import etree
from node_support import requires_node
from swcraft_tools.safety import ASIL_LEVELS
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE as AR_NS

#: The model every case is built from: the committed example, which declares two
#: components and one requirement linking both. Copied rather than used in place
#: — this module writes `asil` into every element of its copy.
_SOURCE = GOLDEN_MODEL_ROOTS["basic-4.2.2"]

#: The level written into every element of the "declared" tree. `D` and not
#: `QM`: it is the value that would be most visible if it leaked into an
#: emitted `SHORT-NAME`, an `ADMIN-DATA` group or a LikeC4 node, and it is the
#: one the `needs.json` exception is stated for.
_LEVEL = "D"


def _needs(root: Path, output: Path) -> bytes:
    """The ``needs.json`` ``generate needs`` writes for *root*."""
    generate_needs(str(root), str(output), "safety")
    return output.read_bytes()


def _line_difference(before: bytes, after: bytes) -> tuple[list[str], list[str]]:
    """``(added, removed)`` lines of a line diff (``difflib.ndiff``), so order counts.

    A line that moved is one removal and one addition, and a doubled line is one
    addition: each extra occurrence is reported once.
    """
    old = before.decode("utf-8").splitlines()
    new = after.decode("utf-8").splitlines()
    diff = list(difflib.ndiff(old, new))
    added = [line[2:] for line in diff if line.startswith("+ ")]
    removed = [line[2:] for line in diff if line.startswith("- ")]
    assert len(new) == len(old) + len(added) - len(removed)
    return added, removed


def _elements(model_root: Path, layer: str) -> list[Path]:
    return sorted((model_root / layer).rglob("*.yaml"), key=lambda path: path.as_posix())


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """`(bare, declared)` — the same model, one of them declaring `asil` everywhere."""
    bare = tmp_path / "bare"
    declared = tmp_path / "declared"
    shutil.copytree(_SOURCE, bare)
    shutil.copytree(_SOURCE, declared)

    written = 0
    for layer in ("swc", "requirements"):
        for path in _elements(declared, layer):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["asil"] = _LEVEL
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
            written += 1
    # The fixture is only evidence if it really declared something: a copy that
    # silently wrote no field would make every assertion below vacuous.
    assert written >= 3, written
    assert _LEVEL in ASIL_LEVELS
    return bare, declared


def _arxml(model_root: Path, out: Path, version: str) -> bytes:
    generate_arxml(str(model_root), str(out), version=version)
    return out.read_bytes()


def _arxml_without_documentation(model_root: Path, out: Path) -> str:
    """The 4.2.2 document with every ``DESC`` and ``LONG-NAME`` removed, as text.

    The writer writes a declared ``description`` (and a composition's ``title``)
    exactly as held, and a fixture's text may name the
    level it documents. The spelling gates are about the **fields**, which is what
    the byte-identity cases prove; the author's free text is not a field of the
    capability, so it is taken out before the word is looked for.
    """
    generate_arxml(str(model_root), str(out), version="4.2.2")
    tree = etree.parse(str(out))
    for tag in ("DESC", "LONG-NAME"):
        for element in list(tree.iter(f"{{{AR_NS}}}{tag}")):
            element.getparent().remove(element)
    return etree.tostring(tree, encoding="unicode")


def _diagrams(model_root: Path, views_root: Path) -> dict[str, bytes]:
    """Every file the diagram generator writes for *model_root*, by name."""
    generate_diagrams(str(model_root), str(views_root), "safety", node=requires_node())
    files = {entry.name: entry.read_bytes() for entry in views_root.iterdir()}
    assert files, views_root
    return files


#: The files the `deployment` view's levels may move: its own two, and the report
#: and the index, whose size line for the view carries its byte count.
_DEPLOYMENT_FILES = frozenset({"deployment.svg", "deployment.html", "report.txt", "index.html"})


def _assert_only_the_deployment_levels_moved(
    declared: dict[str, bytes], bare: dict[str, bytes], suffixes: tuple[str, ...]
) -> None:
    """The one diagram exception: the levels move the `deployment` view and its size line, nothing else.

    Every other file is byte-identical; the report and the index differ only on the
    view's own size line; the view's SVG carries each of *suffixes* as a label
    suffix where the bare one carries no level at all.
    """
    assert sorted(declared) == sorted(bare)
    moved = {name for name in declared if declared[name] != bare[name]}
    assert moved <= _DEPLOYMENT_FILES, sorted(moved - _DEPLOYMENT_FILES)
    assert {"deployment.svg", "deployment.html"} <= moved, sorted(moved)
    for name in ("report.txt", "index.html"):
        left = declared[name].decode("utf-8").splitlines()
        right = bare[name].decode("utf-8").splitlines()
        assert len(left) == len(right), name
        differing = [line for line, other in zip(left, right, strict=True) if line != other]
        assert all(line.lstrip().startswith("deployment ") for line in differing), (name, differing)
    drawn, plain = declared["deployment.svg"].decode("utf-8"), bare["deployment.svg"].decode("utf-8")
    for suffix in suffixes:
        assert drawn.count(f" — {suffix}") > plain.count(f" — {suffix}"), suffix


def test_the_arxml_never_spells_the_level_at_all(trees: tuple[Path, Path], tmp_path: Path) -> None:
    """Not merely equal to the bare document — the word is absent from it."""
    _, declared = trees
    text = _arxml(declared, tmp_path / "d.arxml", "4.2.2").decode("utf-8")

    assert "ASIL" not in text.upper()
    assert "SAFEX" not in text


def test_a_declared_qm_leaves_even_the_needs_json_byte_identical(trees: tuple[Path, Path], tmp_path: Path) -> None:
    """`QM` is a declaration and still writes nothing, not even in the `needs.json`."""
    bare, declared = trees
    for path in _elements(declared, "requirements"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["asil"] = "QM"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    assert _needs(declared, tmp_path / "d-needs.json") == _needs(bare, tmp_path / "b-needs.json")


# ---------------------------------------------------------------------------
# The acceptance fixture, and the golden `basic` with all FIVE fields injected.
# ---------------------------------------------------------------------------

#: The texts written into a port's `measures` and a task's `coexistence` (a data
#: element's `asil` takes `_LEVEL`): non-blank for the same reason `_LEVEL` is
#: `D`, they are the values that
#: would be most visible if any of them leaked into an emitted `SHORT-NAME`, an
#: `ADMIN-DATA` group, a LikeC4 node or a Sphinx-Needs option.
_MEASURE_TEXT = "End-to-end protection profile 4."
_COEXISTENCE_TEXT = "Separate partitions with timing protection."


def _strip_coexistence_fields(model_root: Path) -> int:
    """Every port `measures`, task `coexistence` and data element `asil` removed from *model_root*; the count.

    The bare tree is derived by REMOVING the fields from the declaring one
    rather than by writing a second fixture: two hand-written trees could differ
    in something else and the comparison would still pass.
    """
    removed = 0
    for path in _elements(model_root, "swc"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for port in document.get("ports", []):
            removed += bool(port.pop("measures", None))
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    for path in _elements(model_root, "tasks"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        removed += bool(document.pop("coexistence", None))
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    for path in _elements(model_root, "interfaces"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for element in document.get("data_elements", []):
            removed += bool(element.pop("asil", None))
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return removed


@pytest.fixture
def coexistence_trees(tmp_path: Path) -> tuple[Path, Path]:
    """`(bare, declared)` — the acceptance fixture, and the same tree stripped."""
    declared = tmp_path / "declared"
    bare = tmp_path / "bare"
    shutil.copytree(SAFETY_MODEL_ROOT, declared)
    shutil.copytree(SAFETY_MODEL_ROOT, bare)

    removed = _strip_coexistence_fields(bare)
    # The fixture is only evidence if it really declared something: a strip that
    # found nothing would make every assertion below vacuous.
    assert removed >= 6, removed
    return bare, declared


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_the_fixtures_arxml_is_byte_identical(
    coexistence_trees: tuple[Path, Path], tmp_path: Path, version: str
) -> None:
    bare, declared = coexistence_trees

    assert _arxml(declared, tmp_path / f"cd-{version}.arxml", version) == _arxml(
        bare, tmp_path / f"cb-{version}.arxml", version
    )


def test_the_fixtures_arxml_never_spells_any_of_the_three(coexistence_trees: tuple[Path, Path], tmp_path: Path) -> None:
    _, declared = coexistence_trees
    text = _arxml_without_documentation(declared, tmp_path / "cd.arxml")

    assert "ASIL" not in text.upper()
    assert "SAFEX" not in text
    assert "measures" not in text.lower()
    assert "coexistence" not in text.lower()
    assert _MEASURE_TEXT not in text
    assert _COEXISTENCE_TEXT not in text


def test_the_fixtures_diagrams_are_byte_identical(coexistence_trees: tuple[Path, Path], tmp_path: Path) -> None:
    bare, declared = coexistence_trees

    assert _diagrams(declared, tmp_path / "cd-views") == _diagrams(bare, tmp_path / "cb-views")


def test_the_fixtures_needs_json_is_byte_identical_with_no_exception(
    coexistence_trees: tuple[Path, Path], tmp_path: Path
) -> None:
    """None of the three sits on a requirement, so the Sphinx-Needs output does not move at all."""
    bare, declared = coexistence_trees
    assert _needs(declared, tmp_path / "cd-needs.json") == _needs(bare, tmp_path / "cb-needs.json")


# ---------------------------------------------------------------------------
# The capability of a core and of a memory: neither field sits on a
# requirement, so the `needs.json` does not move.
# ---------------------------------------------------------------------------


def strip_capability(model_root: Path) -> int:
    """Every `asil_capability` key removed from *model_root*'s hardware, and how many were found."""
    removed = 0
    for path in _elements(model_root, "hardware"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        removed += bool(document.pop("asil_capability", None))
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return removed


@pytest.fixture(params=["acceptance-fixture", "basic-injected"])
def capability_trees(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path]:
    """`(bare, declared)`, over two trees so the two oracles share neither.

    `acceptance-fixture`: the committed fixture, which rates `CORE0` and `RAM0`,
    against the same tree with both capabilities stripped. `basic-injected`: the
    golden `basic` with `D` written onto its core and its memory, against the
    untouched copy.
    """
    declared = tmp_path / "capability-declared"
    bare = tmp_path / "capability-bare"
    if request.param == "acceptance-fixture":
        shutil.copytree(SAFETY_MODEL_ROOT, declared)
        shutil.copytree(SAFETY_MODEL_ROOT, bare)
        # The fixture is only evidence if it really declared both.
        assert strip_capability(bare) == 2
        return bare, declared

    shutil.copytree(_SOURCE, declared)
    shutil.copytree(_SOURCE, bare)
    written = 0
    for path in _elements(declared, "hardware"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if document["kind"] in ("core", "memory"):
            document["asil_capability"] = _LEVEL
            written += 1
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    assert written == 2, written
    return bare, declared


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_the_capabilitys_arxml_is_byte_identical(
    capability_trees: tuple[Path, Path], tmp_path: Path, version: str
) -> None:
    bare, declared = capability_trees

    assert _arxml(declared, tmp_path / f"kd-{version}.arxml", version) == _arxml(
        bare, tmp_path / f"kb-{version}.arxml", version
    )


def test_the_capabilitys_arxml_never_spells_the_field(capability_trees: tuple[Path, Path], tmp_path: Path) -> None:
    _, declared = capability_trees
    text = _arxml_without_documentation(declared, tmp_path / "kd.arxml")

    assert "asil_capability" not in text.lower()
    assert "ASIL" not in text.upper()


def test_the_capabilitys_diagrams_move_only_the_deployment_views_core_labels(
    capability_trees: tuple[Path, Path], tmp_path: Path
) -> None:
    """The core's capability is drawn on its box in the `deployment` view; a memory's is drawn nowhere."""
    bare, declared = capability_trees

    _assert_only_the_deployment_levels_moved(
        _diagrams(declared, tmp_path / "kd-views"), _diagrams(bare, tmp_path / "kb-views"), ("ASIL capability",)
    )


def test_the_capabilitys_needs_json_is_byte_identical_with_no_exception(
    capability_trees: tuple[Path, Path], tmp_path: Path
) -> None:
    """`generate needs` writes nothing of the hardware's level, nor of its budgets."""
    bare, declared = capability_trees
    assert _needs(declared, tmp_path / "kd-needs.json") == _needs(bare, tmp_path / "kb-needs.json")


@pytest.fixture
def all_five_trees(tmp_path: Path) -> tuple[Path, Path]:
    """`(bare, declared)` over the golden `basic`, with ALL FIVE fields injected.

    The acceptance fixture proves the shipped model; this proves the shape over
    the example every other gate measures, so the two oracles do not share a
    tree.
    """
    bare = tmp_path / "five-bare"
    declared = tmp_path / "five-declared"
    shutil.copytree(_SOURCE, bare)
    shutil.copytree(_SOURCE, declared)

    written = 0
    for layer in ("swc", "requirements"):
        for path in _elements(declared, layer):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["asil"] = _LEVEL
            if layer == "swc":
                for port in document.get("ports", []):
                    port["measures"] = {"value": _MEASURE_TEXT}
                    written += 1
            written += 1
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    for path in _elements(declared, "interfaces"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for element in document.get("data_elements", []):
            element["asil"] = _LEVEL
            written += 1
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    for path in _elements(declared, "tasks"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["coexistence"] = _COEXISTENCE_TEXT
        written += 1
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    # Measured on `examples/basic`: two components (one with a port each), one
    # requirement, one interface element and one task.
    assert written >= 7, written
    return bare, declared


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_all_five_fields_leave_the_arxml_byte_identical(
    all_five_trees: tuple[Path, Path], tmp_path: Path, version: str
) -> None:
    bare, declared = all_five_trees

    assert _arxml(declared, tmp_path / f"5d-{version}.arxml", version) == _arxml(
        bare, tmp_path / f"5b-{version}.arxml", version
    )


def test_all_five_fields_move_only_the_deployment_views_component_labels(
    all_five_trees: tuple[Path, Path], tmp_path: Path
) -> None:
    """A component's effective level is drawn on its box in the `deployment` view, and nothing else moves."""
    bare, declared = all_five_trees

    _assert_only_the_deployment_levels_moved(
        _diagrams(declared, tmp_path / "5d-views"), _diagrams(bare, tmp_path / "5b-views"), (f"ASIL {_LEVEL}",)
    )


def test_all_five_fields_move_the_needs_json_by_exactly_the_asil_lines(
    all_five_trees: tuple[Path, Path], tmp_path: Path
) -> None:
    """The one exception and no other: the three further fields move nothing.

    This is the whole difference-set assertion, `removed == []` included. The
    exception is the `asil` key of each architecture requirement's need; an
    element's need carries no level, so a component's `asil` moves nothing here.
    """
    bare, declared = all_five_trees
    added, removed = _line_difference(
        _needs(bare, tmp_path / "5b-needs.json"), _needs(declared, tmp_path / "5d-needs.json")
    )

    assert removed == []
    assert [line.strip() for line in added] == [f'"asil": "{_LEVEL}",' for _ in _elements(declared, "requirements")]


# ---------------------------------------------------------------------------
# A requirement's provenance (`document`, `baseline`, `revision`) and its `stub`
# flag. No ARXML exception: the ARXML writer reads no requirement. The
# `needs.json` writes none of the four: an architecture requirement's need
# carries no provenance, an imported requirement
# -- a stub included -- is never published, and the delivery the file names is
# read off a non-stub imported requirement's `baseline` alone. So the claim is
# byte identity again. What this does not reach: the diagrams (Node.js-gated,
# covered by `test_diagram_switch_byte_identity.RECORDED` over a tree declaring
# none of the four), the delivery an imported requirement's `baseline` names
# (`test_needs_export.py`), and a writer edited so that its bytes happen not to
# move.
# ---------------------------------------------------------------------------

_PROVENANCE = {"document": "SRS-Brakes", "baseline": "BL-7", "revision": "1.3"}


def _write_requirement(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


@pytest.fixture
def provenance_trees(tmp_path: Path) -> dict[str, Path]:
    """`bare`; `provenance` (all three on every requirement); `stub` and its flag-free `twin`."""
    trees = {name: tmp_path / name for name in ("bare", "provenance", "stub", "twin")}
    for root in trees.values():
        shutil.copytree(_SOURCE, root)
    written = 0
    for name in ("provenance", "stub", "twin"):
        for path in _elements(trees[name], "requirements"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            _write_requirement(path, {**document, **_PROVENANCE})
            written += 1
    parent = {"id": "SG_1", "title": "Placeholder", "description": "Placeholder.", "version": 1}
    _write_requirement(trees["stub"] / "requirements" / "SG_1.yaml", {**parent, "source": "external", "stub": True})
    _write_requirement(trees["twin"] / "requirements" / "SG_1.yaml", {**parent, "source": "external"})
    # Evidence only if something was really declared: the three were written
    # into every requirement of three trees, and the source holds at least one.
    source_requirements = len(_elements(_SOURCE, "requirements"))
    assert source_requirements >= 1 and written == 3 * source_requirements, (source_requirements, written)
    return trees


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_provenance_and_the_stub_leave_the_arxml_byte_identical(
    provenance_trees: dict[str, Path], tmp_path: Path, version: str
) -> None:
    bare = _arxml(provenance_trees["bare"], tmp_path / "pb.arxml", version)
    assert _arxml(provenance_trees["provenance"], tmp_path / "pp.arxml", version) == bare
    assert _arxml(provenance_trees["stub"], tmp_path / "ps.arxml", version) == bare


def test_provenance_and_the_stub_flag_leave_the_needs_json_byte_identical(
    provenance_trees: dict[str, Path], tmp_path: Path
) -> None:
    """The `needs.json` writes none of the four, so the claim is byte identity.

    The provenance sits on the example's architecture requirement, whose need carries
    no provenance key, and `SG_1` is an imported requirement, which is never a need;
    the twin's `SG_1` declares no `baseline`, so both files name no delivery (`""`).
    """
    export = {name: _needs(root, tmp_path / f"p{name}-needs.json") for name, root in provenance_trees.items()}
    assert export["provenance"] == export["bare"]
    assert export["stub"] == export["twin"]
