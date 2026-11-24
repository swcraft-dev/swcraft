"""The byte-identity gate: a model without the access forms below generates what it always did.

The forms are ``by: implicit`` on a read, ``by`` on a write, an asynchronous server call
(``asynchronous``, ``poll``, ``result``, ``timeout``) and the ``call_returned`` event. For every committed
model tree the record names that declares none of them, every generated artifact is byte for byte what the
record holds: the ARXML at 4.2.2 and 4.3.0, every file ``swcraft generate diagrams`` writes, and the
resource report's text, Markdown and JSON.

The record is ``test_external_stubs_byte_identity``'s ``WITHOUT_A_STUB``, reused rather than copied;
:data:`RECORD_DIGEST` is pinned as a literal so a re-measured row fails here too. The resource report is a table of its
own (:data:`RESOURCE_REPORT`). The premise -- no recorded tree declares one of these forms -- is asserted, not assumed.
``recorded_oracles/impact_model`` declares inter-runnable variables, so its ARXML cells are excluded
(:data:`DECLARES_A_VARIABLE`). This is a record, not a golden to refresh: a moved digest is an output change that
carries its own ``Qualification Impact`` section. ARXML bytes are compared after
``termination_strip_support.strip_recorded``.

**What it does not reach:**

* a model shape no committed tree holds (twelve trees for ARXML, thirteen for diagrams and the report);
* a tree whose YAML the model loader reads differently from ``load_yaml_document``, the premise's reader
  (a read ``by: value`` is inside the claim);
* the impact model's ARXML, which none of the four sibling gates compares;
* the record's RST column and ``generate needs`` output;
* the report with ``--fits``, the MCP ``overview`` and the tool's reply;
* the access-forms fixture, the *with* half's subject;
* which diagram file moved: the aggregate digest names only the tree;
* uncommitted trees, real tool exports among them;
* ``generate arxml --component``, ``validate``, ``metrics``, the experiments and the importer;
* the *with* half, which the consumers' own suites and the XSD suite witness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from swcraft_tools.contract import load_contract
from swcraft_tools.resource_report import format_report_json, report_markdown, resource_report
from swcraft_tools.validator import check_hardware_topology
from swcraft_tools.yaml_load import load_yaml_document
from termination_strip_support import strip_recorded
from test_external_stubs_byte_identity import WITHOUT_A_STUB, _diagram_digest, _generate
from test_inter_runnable_variable_byte_identity import DECLARES_A_VARIABLE

#: The commit the record and the resource-report column were first measured at. The keys are each tree's
#: repository-relative path, and a cell re-measured later is stated by the change that moved it.
BRANCH_POINT = "50101c74d"

#: One SHA-256 over ``WITHOUT_A_STUB`` (:func:`_record_digest`). Any moved cell or renamed tree key moves
#: it; it is re-pinned only for a move another change states.
RECORD_DIGEST = "08149e87f6e8607f251a2187446a1dbb785d502c39a995662f419b3de061bbb5"

#: The project name the Markdown rendering was captured under.
PROJECT_NAME = "baseline"

#: Per recorded tree: the SHA-256 of the resource report's text, Markdown and JSON, first captured at
#: :data:`BRANCH_POINT`. The text names the model root by its repository-relative path, so moving or renaming a tree
#: moves its text cell; the Markdown and JSON name no path. Five trees share one Markdown and one JSON digest: the
#: report of a model that declares no core and no memory, titled by the one project name.
RESOURCE_REPORT: dict[str, tuple[str, str, str]] = {
    "examples/basic/model": (
        "9f479891b6645a25dc0841fa54099850870ec34f8fbaa5f790a915ea237961b8",
        "7d9affe1aa079bf003bc07411de5fccadaae4dfa89c4b8818ff79ff993e81f47",
        "cd0dfb7c23ac26b39c7148dd58ce50647fa382bccb6cace975ee9729b3827a9a",
    ),
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "7a1a72a43263b17753ca18f1e18fd88ac910ac1c61550ce5ad8140bc67ea38c8",
        "ec0e38f62cf01b3a415c4afcfb2b82dab5ad97d0f252010f7c4e9531aac8f3bc",
        "491b63c142d7e9a4cfba3fd6bd3fa5dfb9eee68b376bd4216de78ccb1100accc",
    ),
    "packages/tools/tests/fixtures/access_points/model": (
        "e6ce97c5a69c5b6de739d6c92c0fb951276ac75b81ce5a00e1bbe68a4d9ff831",
        "f85ca38287251dbd09d613724378c5ebd6d470a386c7151267473126fd60671b",
        "5a4e75677bf0cc3905bbc422f54d0719bfb7c9a129d74acd0b633d693f76b4b2",
    ),
    "packages/tools/tests/fixtures/compatible_interfaces/model": (
        "a3c2ee6e4fca1876894bfbf0b00dfb7671a8661516d692379ba014795d1ff248",
        "f85ca38287251dbd09d613724378c5ebd6d470a386c7151267473126fd60671b",
        "5a4e75677bf0cc3905bbc422f54d0719bfb7c9a129d74acd0b633d693f76b4b2",
    ),
    "packages/tools/tests/fixtures/component_exchange/model": (
        "9ef4a5733ae4799825aceb6beb624d25a1e2467d4fca05d50768272166b7b437",
        "8d3160a6fcff8fa666cca2f81e0b7bd0cafb0ed278043cf2acf7dcec85a4eb42",
        "f5abda88f5f4934cb2d5c72768cf457924592fa21d3d2ba2805cbf10d925ea37",
    ),
    "packages/tools/tests/fixtures/compositions/model": (
        "2012c3f6c9c8a9cac4e2e2ecb4ffe27e87af1a224df63ea854f6adce9dd37531",
        "f85ca38287251dbd09d613724378c5ebd6d470a386c7151267473126fd60671b",
        "5a4e75677bf0cc3905bbc422f54d0719bfb7c9a129d74acd0b633d693f76b4b2",
    ),
    "packages/tools/tests/fixtures/diagrams/model": (
        "81487b78f5f78cbcebcafd35db5a98a6bc758f5437849c9a89304403ee25c9ca",
        "97beb1f2913c4b39e4e4f691f677bfab73b86e283841eea6872260b56a102b7e",
        "f2ff7fb68c5aca8edc53c2945ba738abf78679748b1518370765ca564a997abc",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "b823a6b4a8f42e69b47f818f06cd7d5a7810c7f30441d9825b2c7033b64ba009",
        "901a58a8f8307f509b4904002dde0dfa9486512ec2a76f595ce4576dd35517f3",
        "caf5e9356e2b6fa69d5a916b8d6c4f5112ff8d8fecdf8e1cea25450ce97a4316",
    ),
    "packages/tools/tests/fixtures/init_values/model": (
        "64d3edad1fb68cf9a70c2a61e37ce214e1b707cd0021dfa0e2cc31bbac1a7caa",
        "f85ca38287251dbd09d613724378c5ebd6d470a386c7151267473126fd60671b",
        "5a4e75677bf0cc3905bbc422f54d0719bfb7c9a129d74acd0b633d693f76b4b2",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "2bdddfe2c2acd3cedddb9321115108202fd3c5ed114f9cea4bbc5e8f8cef3b14",
        "f85ca38287251dbd09d613724378c5ebd6d470a386c7151267473126fd60671b",
        "5a4e75677bf0cc3905bbc422f54d0719bfb7c9a129d74acd0b633d693f76b4b2",
    ),
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": (
        "2bed4c0d63913a88d40c12dc61e5f8e8be3a94251bf97f95b77002607b64b508",
        "4c02403e44d363baa7f9f5100b70ca8428e830d6851c496cc45b09c539c24502",
        "3cdbd6cae008a89e05a6299ccbf336e74af7c6b5fae3299beb48ae752dc89a67",
    ),
    "packages/tools/tests/fixtures/safety/model": (
        "8bb031c47b052536bdf3fbbba9c022d28f5d3bffa190faa45ef068926e8a7b7e",
        "cabf80443fe1f129c5ad655eda8332fc420e287e08e70f5867fb82f08dd0d26d",
        "1e811c75d6529062273bfd1f57357c123ae12a590e8b19bfea0ce9856607a927",
    ),
    "packages/tools/tests/fixtures/showcase/model": (
        "26a8383100dbc1b4d820d24b8addbe41701ff633b66b86593827d18d189f5cd0",
        "7e52fb773040a2cc5be7c238bae705547c3e063ec14fce1604a21383bf850263",
        "5e0ca2bc286da8ad0dbb6861966861571e6683eee35743122b8541406344d793",
    ),
}

#: One SHA-256 over :data:`RESOURCE_REPORT` (:func:`_resource_report_digest`). The text column spells the
#: model root repository-relative, so it holds on any checkout; a moved or renamed tree moves its text cell
#: and this digest.
RESOURCE_REPORT_DIGEST = "39be6d0d1760624e86b9b3bd579ed7285d9513d06a7f63c0004e8c11ec920fcd"

_ARXML = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [
    (tree, artifact, WITHOUT_A_STUB[tree][index])
    for tree in WITHOUT_A_STUB
    for index, artifact in enumerate(_ARXML)
    if tree not in DECLARES_A_VARIABLE
]

_RENDERINGS = ("text", "Markdown", "JSON")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_digest() -> str:
    canonical = json.dumps({tree: list(row) for tree, row in WITHOUT_A_STUB.items()}, sort_keys=True)
    return _sha(canonical.encode("utf-8"))


def _resource_report_digest() -> str:
    canonical = json.dumps({tree: list(row) for tree, row in RESOURCE_REPORT.items()}, sort_keys=True)
    return _sha(canonical.encode("utf-8"))


def _field(field_id: str) -> dict:
    """A field's contract entry, read off the contract rather than spelled a second time."""
    for layer in load_contract()["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field
    raise AssertionError(f"the contract declares no {field_id}")


def _swc_directory() -> str:
    (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
    return layer["storage"]["directory"]


def _new_forms(model_root: Path) -> list[str]:
    """Every use of a listed access form in *model_root*'s component files, as ``<storage name>:<runnable>:<what>``."""
    runnables = _field("field:swc.swc.runnables")["name"]
    events = _field("field:swc.runnable.events")["name"]
    reads = _field("field:swc.runnable.reads")["name"]
    writes = _field("field:swc.runnable.writes")["name"]
    calls = _field("field:swc.runnable.calls")["name"]
    read_by = _field("field:swc.data_read.by")
    implicit = "implicit"
    assert implicit in read_by["enum"], "the contract no longer offers a read by implicit"
    write_by = _field("field:swc.data_write.by")["name"]
    call_fields = [
        _field(f"field:swc.operation_call.{name}")["name"] for name in ("asynchronous", "poll", "result", "timeout")
    ]
    event_type = _field("field:swc.call_returned_event.type")
    (call_returned,) = event_type["enum"]
    found: list[str] = []
    for path in sorted((model_root / _swc_directory()).rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = load_yaml_document(path.read_text(encoding="utf-8")) or {}
        storage = path.relative_to(model_root).as_posix()
        for runnable in element.get(runnables) or []:
            where = f"{storage}:{runnable.get('name')}"
            found.extend(
                f"{where}:a read by implicit"
                for read in runnable.get(reads) or []
                if isinstance(read, dict) and read.get(read_by["name"]) == implicit
            )
            found.extend(
                f"{where}:a write stating {write_by}"
                for write in runnable.get(writes) or []
                if isinstance(write, dict) and write_by in write
            )
            found.extend(
                f"{where}:a call stating {name}"
                for call in runnable.get(calls) or []
                if isinstance(call, dict)
                for name in call_fields
                if name in call
            )
            found.extend(
                f"{where}:a call-returned event"
                for event in runnable.get(events) or []
                if isinstance(event, dict) and event.get(event_type["name"]) == call_returned
            )
    return found


def _report_renderings(model_root: Path) -> tuple[str, str, str]:
    """The SHA-256 of the report's text, Markdown and JSON, rendered as the table records them."""
    report = resource_report(str(model_root), check_hardware_topology(str(model_root)).records)
    # The report's first line names the root as it was given; the gate spells it as the repository-relative
    # tree, so the digest is the same on every checkout.
    text = report.render().replace(str(model_root), model_root.relative_to(REPO_ROOT).as_posix()) + "\n"
    markdown = report_markdown(report, PROJECT_NAME)
    document = format_report_json(report)
    return (_sha(text.encode("utf-8")), _sha(markdown.encode("utf-8")), _sha(document.encode("utf-8")))


def test_the_record_is_the_branch_points() -> None:
    """The sibling module's record is still the one :data:`RECORD_DIGEST` pins."""
    assert _record_digest() == RECORD_DIGEST


def test_the_resource_report_table_is_the_branch_points() -> None:
    """The resource-report table is the one :data:`RESOURCE_REPORT_DIGEST` pins, naming exactly the recorded trees."""
    assert _resource_report_digest() == RESOURCE_REPORT_DIGEST
    assert sorted(RESOURCE_REPORT) == sorted(WITHOUT_A_STUB)


@pytest.mark.parametrize(("tree", "artifact", "recorded"), _CASES, ids=[f"{t}:{a}" for t, a, _ in _CASES])
def test_the_arxml_is_the_branch_points(tree: str, artifact: str, recorded: str, tmp_path: Path) -> None:
    model_root = REPO_ROOT / tree
    assert model_root.is_dir(), f"{tree} is recorded but not committed"
    output = tmp_path / artifact
    if recorded.startswith("refused: "):
        with pytest.raises(ValueError, match=recorded.removeprefix("refused: ")):
            _generate(model_root, artifact, output)
        return
    _generate(model_root, artifact, output)
    assert _sha(strip_recorded(tree, artifact, output.read_bytes())) == recorded


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_every_diagram_file_is_the_branch_points(tree: str, tmp_path: Path) -> None:
    count, digest = _diagram_digest(REPO_ROOT / tree, tmp_path / "views")
    assert (count, digest) == WITHOUT_A_STUB[tree][3:]


@pytest.mark.parametrize("tree", sorted(RESOURCE_REPORT))
def test_the_resource_report_is_the_branch_points(tree: str) -> None:
    model_root = REPO_ROOT / tree
    assert model_root.is_dir(), f"{tree} is recorded but not committed"
    rendered = _report_renderings(model_root)
    moved = [name for name, now, then in zip(_RENDERINGS, rendered, RESOURCE_REPORT[tree], strict=True) if now != then]
    assert moved == []


def test_no_recorded_tree_declares_a_new_form() -> None:
    """The premise, asserted rather than assumed: no recorded tree holds a listed access form."""
    declaring = {tree: forms for tree in WITHOUT_A_STUB if (forms := _new_forms(REPO_ROOT / tree))}
    assert declaring == {}


def test_the_premise_check_sees_every_shape(tmp_path: Path) -> None:
    """The premise check is not vacuous: each listed form in a scratch tree is found, and no other form is.

    The other forms beside them are a read ``by: value``, a plain read, write and call, and a timing event.
    """
    group = tmp_path / _swc_directory() / "g"
    group.mkdir(parents=True)
    (group / "Client.yaml").write_text(
        "name: Client\nrunnables:\n"
        "  - name: Old\n    events:\n      - type: timing\n        period: 0.01\n"
        "    reads:\n      - port: In\n        element: Speed\n"
        "      - port: In\n        element: Level\n        by: value\n"
        "    writes:\n      - port: Out\n        element: Speed\n"
        "    calls:\n      - port: Srv\n        operation: Apply\n"
        "  - name: New\n    events:\n      - type: call_returned\n        port: Srv\n        operation: Get\n"
        "    reads:\n      - port: In\n        element: Speed\n        by: implicit\n"
        "    writes:\n      - port: Out\n        element: Speed\n        by: implicit\n"
        "    calls:\n      - port: Srv\n        operation: Get\n        asynchronous: true\n"
        "        poll: true\n        result: GetResult\n        timeout: 0.5\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _new_forms(tmp_path) == [
        "swc/g/Client.yaml:New:a read by implicit",
        "swc/g/Client.yaml:New:a write stating by",
        "swc/g/Client.yaml:New:a call stating asynchronous",
        "swc/g/Client.yaml:New:a call stating poll",
        "swc/g/Client.yaml:New:a call stating result",
        "swc/g/Client.yaml:New:a call stating timeout",
        "swc/g/Client.yaml:New:a call-returned event",
    ]
