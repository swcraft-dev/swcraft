"""The byte-identity gate: a model without an application type generates what it always did.

For a model that declares no application type and no mapping set, the ARXML at both versions, mapped back as below,
matches the digests the generators of :data:`BRANCH_POINT` wrote, except in a row whose fixture was edited; every file
``swcraft generate diagrams`` writes is what the last deliberate diagram change recorded. Where this record and
``test_diagram_switch_byte_identity.RECORDED`` share a tree, their ARXML and RST digests agree. The trees that declare
the feature are absent from :data:`WITHOUT_THE_FEATURE` and :data:`MIGRATED` holds only their diagrams.

**This is a record, not a golden to refresh.** A change that moves one of these digests changes output for
a model that declares none of the feature and carries its own ``Qualification Impact`` section. A change
that moves a diagram on purpose re-measures the last two columns of every row and of :data:`MIGRATED` in
the same commit, and proves first that the generated files, with exactly that change undone, answer the
digests recorded before it.

The ARXML is compared after ``termination_strip_support.strip_recorded``: ``fidelity_strip_support.strip_fidelity``
applies every writer-fidelity reversal and re-serializes, then, for a tree in ``TERMINATION_ADDED``, the one
``HANDLE-TERMINATION-AND-RESTART`` line per named component goes. The RST column is kept, and no case reads it.

**What it does not reach:**

* which diagram file moved: the digest is one SHA-256 over the sorted ``name\\0digest`` lines of the views
  directory, with the file count beside it;
* a byte a reversal of ``strip_fidelity`` rewrites: the digest is taken after the reversals;
* a model tree that is not committed, such as a real tool export;
* the generators reached through the CLI (``test_walk_reports_byte_identity.py``'s surface);
* the component exports of ``generate arxml --component``, and ``validate``;
* an **edited** writer whose bytes happened not to move: the *with* half
  (``test_arxml_application_types_writer.py`` and the ``composite-4.2.2`` golden) witnesses that the
  emission is additive, and neither oracle discharges the other.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, is_data_fixture_tree
from node_support import requires_node
from swcraft_tools.writer import generate_arxml, generate_diagrams
from termination_strip_support import strip_recorded
from test_diagram_switch_byte_identity import RECORDED

#: The commit whose generators the record was taken with: ``main`` before the feature.
BRANCH_POINT = "ed4f81c7"

#: The project name every diagram generation below is given. One constant, so the record is
#: reproducible; the name reaches the pages' titles and nothing else.
PROJECT_NAME = "byte-identity"

#: Tree by tree: ARXML 4.2.2, ARXML 4.3.0, RST, the number of files ``generate diagrams`` writes,
#: and one digest over all of them; the last two move with a deliberate diagram change, and any column moves
#: when a recorded fixture is edited.
WITHOUT_THE_FEATURE: dict[str, tuple[str, str, str, int, str]] = {
    "examples/basic/model": (
        "83929b4cef3a626d99a30f916a447ab19bb36cce7ddd39ea044774d2404e1564",
        "da62aebffffadcfb21b575951537f24ecbf8e042ba9382cd1846fc58a55868dc",
        "23242c5c8c13fed1478c622b63e33c8d3a3b3598b7116d99213747bc67c8d7bf",
        12,
        "287155f33a127bb255b5d896ac5111060d8fc94f6000ea62cfd4e90ab8d68ceb",
    ),
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "fd4e91236a437658b937f47a678c090d97f7758bae2e69e3b4aaecafe75b7a97",
        "f6546472bfe78f33dd2d329102a0c79f3b74b41b324c596c29f86cac54167d23",
        "1a4235d2e71dcd1e49cd62dc7ab36a622bc0e05af39446ca8122aab8046bac37",
        8,
        "0f1799f4f7cadbcb54d058f18faea1106b854ab8a4a4e1018952591cb7a656a7",
    ),
    "packages/tools/tests/fixtures/access_points/model": (
        "ff4ec89fa9b2746e14bcfd019b8a31dd2f3252c02a9afedb1fb12f08cdf15cab",
        "ba0d1e536e5dfad22b123ab2d7e153c115bc3deafd9649397ecf9d80603344bb",
        "4cbe34cee65a71170a89cb637816b3b4ea9b4544a948747d8d67761f6a5260b3",
        8,
        "8218347ed69f63c219715c8d053055a5fec89d3cf97c303df5607a91f31d0744",
    ),
    "packages/tools/tests/fixtures/compatible_interfaces/model": (
        "34a7f10ed572b6846b981c2daf9a5309356de49f0ea8ce6c9087d69ae4c379c9",
        "516a32ebcb06046d45600bc3a2444f5fc8c26775b777c438d0acff20a51c56a8",
        "9627e1109988b2f232dc51726bae5550535257438a4bc3d68c8ac6c4f4f97424",
        12,
        "8c7ddb0a6a8fd1c9b667a9456136c613d4ebd6ba2e53b8f85d28b514c469298c",
    ),
    # The ARXML and diagram cells are the edited fixture's (its stub's `external_ref`, `Root`'s description).
    "packages/tools/tests/fixtures/compositions/model": (
        "c85aa2a56f109fb97f319a31ca18da491360e3165a2635cce8d3c0e0d09c1cd2",
        "f98f0551903d72e1df3076f7efb275f4b10149134df12f50abd424c24274f018",
        "7e99fed47e450f9d77b2425cbdbd7f6000d5eeff7dfa2f529464db4c729e08ed",
        16,
        "5f2c6d66752d516a85126fa9993877b31454283e8d2a35ce7414d4704b4a553f",
    ),
    "packages/tools/tests/fixtures/diagrams/model": (
        "c9d44b58466bac4dc655b395b6682789bcb067c4037466c930f824ae7231e2f4",
        "d1601b0f828d2b347319efcd43ca0fe873c56656e606534e8b58bfa47474f87e",
        "94512f2b18060e9e495f14f323a5a964ab3005f9497e1224795e2640741df6aa",
        16,
        "15175f6d806f85b1997b7e99b59d1c1b877494e2d899604e36c0d0575681c7ea",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "9e4d2c11f1c9767dee793f3427e34ae0142a68d98a770976f128a752d41753d9",
        "7d0e76701b511afb592956be15737258365f3d4a5e17e6914f326b6089e65917",
        "1a4235d2e71dcd1e49cd62dc7ab36a622bc0e05af39446ca8122aab8046bac37",
        10,
        "1c357045c34789885525ea3ec590e02d6f784ecde59d790d766900befc79fc55",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "5fbc3996361702e4e08bc274461395ef635ae2b34ddfd797ff1a5739f62c8c9b",
        "e8f15a5dabe5f01acb2461613d585760b5009935e0e592a4b80b4e57c563017f",
        "ed3b1f5115e15a112194f4247ce76a020c7846b064b7012e0c9f8d1709b9bafc",
        8,
        "112eaf3e9f09966bbf431b4f6a376b32dd36de740c103e1ab43b8bb166d64ff3",
    ),
    "packages/tools/tests/fixtures/safety/model": (
        "48e3b364c180af694d8cae6f358ba86deaea40501651c92fecb47e9c2a694a3c",
        "5876522deaf4f3d798354cef5d1dd603b210f8a80c595b6150ef98b0c4434240",
        "570cc7793f88d09edebafb374980409509322600cb3325a7d9607ace67fe0a7c",
        10,
        "f38b2c40049d27d69af1e899934aab50144a4da872be3276f66ced684b873951",
    ),
    "packages/tools/tests/fixtures/showcase/model": (
        "0017e93065afb2436f80cee657a7ad49ccceaa2a8c2e63cfe0528ea965dd3536",
        "f20eda2cc76b224e421e077057c1a1b6575c0c288b196acc3adc5be66c9466a0",
        "6555de39961fe542d657ce2138c71b9bb8b783b609e97c2e43a2a975f520de45",
        12,
        "8ccd857cdbdbf70a5a6f56ba663ca6229cf79a8b2d31c2d8cc87c03a3dec87b5",
    ),
}

#: The trees that declare the feature, with ``(file count, digest)`` of **their diagrams**. Their
#: ARXML moved by design and is in ``test_diagram_switch_byte_identity.RECORDED`` (the
#: ``component_exchange`` one is also the golden ``arxml_golden/composite-4.2.2.arxml``); their
#: diagrams did not, which is the direct evidence that a type is not drawn.
MIGRATED: dict[str, tuple[int, str]] = {
    "packages/tools/tests/fixtures/component_exchange/model": (
        8,
        "1f080ff40cf1932248c16c965b3a539a67e7d3079cc925b4b346da6643f478a8",
    ),
    "packages/tools/tests/fixtures/init_values/model": (
        8,
        "460a388f4029aee89c90eba748fcb8299efb2e4b67fe63645aaa4d477b95e456",
    ),
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": (
        20,
        "b2380a6cb3b1c3d988cfd0109db000b7c01206daa542dfcc6fa99b2fcaeab060",
    ),
}

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [
    (tree, artifact, WITHOUT_THE_FEATURE[tree][index])
    for tree in WITHOUT_THE_FEATURE
    for index, artifact in enumerate(_ARTIFACTS)
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _generate(model_root: Path, artifact: str, output: Path) -> None:
    generate_arxml(str(model_root), str(output), version=artifact.removeprefix("arxml-"))


def _diagram_digest(model_root: Path, views_root: Path) -> tuple[int, str]:
    """``(file count, one digest over every file's name and bytes)`` for *model_root*."""
    generate_diagrams(str(model_root), str(views_root), PROJECT_NAME, node=requires_node())
    entries = sorted(views_root.iterdir(), key=lambda path: path.name)
    joined = b"".join(
        entry.name.encode("utf-8") + b"\0" + _sha(entry.read_bytes()).encode("ascii") + b"\n" for entry in entries
    )
    return len(entries), _sha(joined)


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


@pytest.mark.parametrize("tree", sorted(WITHOUT_THE_FEATURE))
def test_every_diagram_file_is_the_branch_points(tree: str, tmp_path: Path) -> None:
    """The half neither record module beside this one reaches."""
    count, digest = _diagram_digest(REPO_ROOT / tree, tmp_path / "views")
    assert (count, digest) == WITHOUT_THE_FEATURE[tree][3:]


@pytest.mark.parametrize("tree", sorted(MIGRATED))
def test_the_migrated_trees_diagrams_did_not_move_either(tree: str, tmp_path: Path) -> None:
    """A type is not drawn: the diagrams of the trees that declare the feature did not move."""
    count, digest = _diagram_digest(REPO_ROOT / tree, tmp_path / "views")
    assert (count, digest) == MIGRATED[tree]


@pytest.mark.parametrize("tree", sorted(MIGRATED))
def test_every_migrated_tree_really_declares_the_feature(tree: str) -> None:
    """Every migrated tree declares the feature, so the exclusion cannot hide a tree that moved otherwise."""
    model_root = REPO_ROOT / tree
    declares_a_set = any((model_root / "data_types" / "mapping_sets").glob("*.yaml"))
    declares_a_type = any(
        (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kind") == "application_type"
        for path in (model_root / "data_types").rglob("*.yaml")
    )
    assert declares_a_set and declares_a_type, tree


@pytest.mark.parametrize("tree", sorted(WITHOUT_THE_FEATURE))
def test_no_recorded_tree_declares_the_feature(tree: str) -> None:
    """No recorded tree declares a mapping set or an application type, read off the files.

    An empty ``mapping_sets/`` layer directory is legal; an element in it is what is refused.
    """
    model_root = REPO_ROOT / tree
    sets = sorted(path.name for path in (model_root / "data_types" / "mapping_sets").glob("*.yaml"))
    assert sets == [], f"{tree} declares a mapping set: {sets}"

    application_types = [
        path.name
        for path in sorted((model_root / "data_types").rglob("*.yaml"), key=lambda p: p.as_posix())
        if (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kind") == "application_type"
    ]
    assert application_types == [], f"{tree} declares an application type: {application_types}"


def test_the_shared_rows_agree_with_the_diagram_switch_record() -> None:
    """Where both records name a tree, their ARXML and RST digests agree."""
    shared = sorted(set(WITHOUT_THE_FEATURE) & set(RECORDED))
    assert shared, "the two records name no tree in common"
    for tree in shared:
        assert WITHOUT_THE_FEATURE[tree][:3] == RECORDED[tree], tree


def test_every_committed_model_tree_is_covered() -> None:
    """Every committed tree is in this record or is named as migrated; none is in neither.

    A tree is a YAML file in a ``compositions/`` directory under the three places the repository keeps
    model trees. Not reached: a tree elsewhere, the gitignored guide payload and
    ``conftest.DATA_FIXTURE_ROOTS``.
    """
    bases = (
        REPO_ROOT / "examples",
        REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates",
        REPO_ROOT / "packages" / "tools" / "tests" / "fixtures",
    )
    found = {
        element.parent.parent.relative_to(REPO_ROOT).as_posix()
        for base in bases
        for element in base.rglob("*.yaml")
        if element.parent.name == "compositions"
    }
    found = {tree for tree in found if not is_data_fixture_tree(tree)}
    # The next two trees postdate the branch point, which could not load them, so this record cannot
    # cover them. This one states the data description elements; test_data_description_writer.py
    # holds its output.
    found.discard("packages/tools/tests/fixtures/data_description/model")
    # Implicit accesses, asynchronous calls and the call-returned event; the tests that read it
    # hold its output.
    found.discard("packages/tools/tests/fixtures/access_forms/model")
    # Not a model tree: the editor-schema limitation corpus test_editor_schema_limits.py drops into
    # a copy of `examples/basic`; its `compositions/Root2.yaml` is one class's second root.
    found.discard("packages/tools/tests/fixtures/editor_schema/accepted_but_invalid")
    # Not a model tree: test_unknown_key_oracle.py's per-file corpus, whose references name
    # elements it does not hold.
    found.discard("packages/tools/tests/fixtures/unknown_key_oracle/model")
    # Postdates the branch point: the names a drawn element's model entry must tell apart;
    # test_diagram_generation.py checks each entry it writes.
    found.discard("packages/tools/tests/fixtures/diagram_collisions/model")
    assert found == set(WITHOUT_THE_FEATURE) | set(MIGRATED)
    assert set(WITHOUT_THE_FEATURE).isdisjoint(MIGRATED)
