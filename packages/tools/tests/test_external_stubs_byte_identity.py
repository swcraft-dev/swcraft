"""The byte-identity gate: a model without a stub generates what it always did.

For every committed model tree, none of which declares a stub (``external_interface`` / ``external_type``),
the ARXML at 4.2.2 and 4.3.0 and every file ``swcraft generate diagrams`` writes are byte for byte the
digests recorded here. They equal the two records beside this module
(``test_diagram_switch_byte_identity.RECORDED`` and ``test_application_types_byte_identity``'s
``WITHOUT_THE_FEATURE`` / ``MIGRATED``), which :func:`test_the_record_agrees_with_the_two_records_beside_it`
asserts; they are written out in full so a re-measurement of either cannot move this claim silently. A tree
the ARXML writer refuses carries the refusal's text instead of a digest.

**A record, not a golden to refresh.** A change that moves a digest changes ARXML or diagram output for a
model without a stub; it carries its own ``Qualification Impact`` section and re-measures the digest with the
reason, never to make this module pass. The ``HANDLE-TERMINATION-AND-RESTART`` line every component gains is
removed by ``termination_strip_support.strip_recorded`` before hashing.

What it does not reach: models beyond the trees named (regression evidence, not a proof); which file moved
(the aggregate diagram digest names the tree); uncommitted trees; the CLI, ``generate arxml --component``,
``validate`` and the importer; the *with* half, which is the writer's, the diagrams' and the XSD suites',
except :data:`WITH_A_STUB_THAT_LISTS_NOTHING` below.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, is_data_fixture_tree
from external_stubs_support import component_stub_ports_model_root, external_stubs_model_root
from node_support import requires_node
from swcraft_tools.contract import load_contract
from swcraft_tools.writer import generate_arxml, generate_diagrams
from termination_strip_support import strip_recorded
from test_application_types_byte_identity import MIGRATED, WITHOUT_THE_FEATURE
from test_diagram_switch_byte_identity import RECORDED

#: The commit the record was taken at, before the stub kinds existed.
BRANCH_POINT = "fdf29ba0"

#: The project name every diagram generation is given, the one the two other records use;
#: it reaches the pages' titles and nothing else.
PROJECT_NAME = "byte-identity"

#: The two stub kinds, by contract id; the premise test reads each one's layer and ``kind`` off the contract.
STUB_KIND_IDS = ("kind:interfaces.external_interface", "kind:implementation_types.external_type")

#: Per tree: ARXML 4.2.2, ARXML 4.3.0, RST (kept, not read), the number of files
#: ``generate diagrams`` writes, and one digest over them. A change to the diagram
#: drawing moves the diagram digests; it is re-measured in that change, with its reason.
WITHOUT_A_STUB: dict[str, tuple[str, str, str, int, str]] = {
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
    "packages/tools/tests/fixtures/component_exchange/model": (
        "8d1ff2fb9f850c063623d66ef85cfc0c1b0181aa2606be3efb4fe900956f0a4f",
        "42f678b4ef92c5d00e5e4b1247278e13f909875f5572919aa804c1fe3a71c11c",
        "65a29a30a81d62e91183cf8a2a61964b46028aa5a713e70d8b90c2e9434261fb",
        8,
        "1f080ff40cf1932248c16c965b3a539a67e7d3079cc925b4b346da6643f478a8",
    ),
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
    "packages/tools/tests/fixtures/init_values/model": (
        "a80b613c89b9844cf32b36af31bd2d1ffc83e15d389ad76d8cd58fddbc1eb6e1",
        "bb588bfc553e662d98d448067904ee3912e2d68e9a10d3cb3ef45d0f9691adac",
        "b66da8b2759096bc82be3a11dbc71fef532bad7dd5d9c7cd29113d3cb8c8f1a9",
        8,
        "460a388f4029aee89c90eba748fcb8299efb2e4b67fe63645aaa4d477b95e456",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "5fbc3996361702e4e08bc274461395ef635ae2b34ddfd797ff1a5739f62c8c9b",
        "e8f15a5dabe5f01acb2461613d585760b5009935e0e592a4b80b4e57c563017f",
        "ed3b1f5115e15a112194f4247ce76a020c7846b064b7012e0c9f8d1709b9bafc",
        8,
        "112eaf3e9f09966bbf431b4f6a376b32dd36de740c103e1ab43b8bb166d64ff3",
    ),
    # Refused at both versions: the first refusal is the unmapped application type of
    # `SpeedControl`'s variable, raised by the writer's up-front guard.
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": (
        "refused: 'SpeedControl' speaks 'SpeedPhys'",
        "refused: 'SpeedControl' speaks 'SpeedPhys'",
        "25bfbd8565ed02bdf41a9bc9667c11d9165673079316a9fc7fad783e9ff50639",
        20,
        "b2380a6cb3b1c3d988cfd0109db000b7c01206daa542dfcc6fa99b2fcaeab060",
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

#: **A stub that lists nothing writes what it wrote before.** A stub with empty member lists
#: writes no com-spec and no wrapper, so the ARXML of the two stub-bearing test models (built by
#: ``external_stubs_support``, every stub listing nothing) is byte for byte what the writer
#: produced before it derived a stub port's com-specs, at 4.2.2 and 4.3.0. What it does not
#: reach: a model whose stub lists a member (``test_external_stubs_arxml`` reads its output,
#: ``test-xsd`` validates it); a change to the builders' base models moves these digests for a
#: reason of its own and is re-measured with it.
WITH_A_STUB_THAT_LISTS_NOTHING: dict[str, tuple[str, str]] = {
    "external_stubs_model_root": (
        "b8bd3ef89ed66c0b34558e17ca5605fde9f33488b647f039c4d906dcde50967e",
        "5fd21b2d6cc95f69fa761dba3ceeb321d008a7e185c8272f8430b9220e70942f",
    ),
    "component_stub_ports_model_root": (
        "383525254ecd8a30b05fae777bb955bc2945221a6ae4ec920682a8097e83c5bc",
        "65bfa1dc6718b1e054396678aa1a045fb0a8ac901229315fb139d59b6d38cc27",
    ),
}

_STUB_BUILDERS = {
    "external_stubs_model_root": external_stubs_model_root,
    "component_stub_ports_model_root": component_stub_ports_model_root,
}

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [
    (tree, artifact, WITHOUT_A_STUB[tree][index])
    for tree in WITHOUT_A_STUB
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


def _stub_kinds() -> list[tuple[str, str]]:
    """``(layer directory, kind value)`` of each stub kind, read off the contract.

    The directory, never the layer's name: ``implementation_types`` is stored in ``data_types/implementation``.
    """
    found = [
        (layer["storage"]["directory"], kind["kind"])
        for layer in load_contract()["layers"]
        for kind in layer["kinds"]
        if kind["id"] in STUB_KIND_IDS
    ]
    assert len(found) == len(STUB_KIND_IDS), f"the contract does not declare every stub kind: {found}"
    return found


def _declared_stubs(model_root: Path) -> list[str]:
    """Every element file under *model_root* whose ``kind`` is a stub kind's, read off the file."""
    return sorted(
        path.relative_to(model_root).as_posix()
        for layer, kind in _stub_kinds()
        for path in (model_root / layer).rglob("*.yaml")
        if (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kind") == kind
    )


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


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_no_recorded_tree_declares_a_stub(tree: str) -> None:
    """No recorded tree declares a stub, read off the files rather than the loader.

    A tree that gains a stub leaves this record rather than quietly staying in it.
    """
    stubs = _declared_stubs(REPO_ROOT / tree)
    assert stubs == [], f"{tree} declares a stub: {stubs}"


def test_the_premise_check_sees_a_stub_of_each_kind(tmp_path: Path) -> None:
    """The premise check is not vacuous: one stub file of each kind in a scratch tree is found."""
    for layer, kind in _stub_kinds():
        (tmp_path / layer).mkdir(parents=True)
        (tmp_path / layer / "Stub.yaml").write_text(f"kind: {kind}\nname: Stub\n", encoding="utf-8", newline="\n")
    assert _declared_stubs(tmp_path) == sorted(f"{layer}/Stub.yaml" for layer, _ in _stub_kinds())


def test_the_record_agrees_with_the_two_records_beside_it() -> None:
    """The same bytes anchored three times; a re-measurement of either other record fails here and names the tree."""
    assert set(WITHOUT_A_STUB) == set(RECORDED)
    for tree, row in WITHOUT_A_STUB.items():
        assert row[:3] == RECORDED[tree], tree
        diagrams = WITHOUT_THE_FEATURE[tree][3:] if tree in WITHOUT_THE_FEATURE else MIGRATED[tree]
        assert row[3:] == diagrams, tree


def test_every_committed_model_tree_is_recorded() -> None:
    """Every committed tree is in this record; a tree committed later without one fails here.

    A tree is a YAML file's ``compositions/`` directory under the three places named below;
    the gitignored guide payload, trees elsewhere and ``conftest.DATA_FIXTURE_ROOTS`` are not reached.
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
    # Not recorded: it states units, compu methods, data constraints and every reference
    # field, which postdate the record; test_data_description_writer.py and test-xsd hold it.
    found.discard("packages/tools/tests/fixtures/data_description/model")
    # Not recorded: implicit accesses, asynchronous calls and the call-returned event postdate
    # the record; a row would move every sibling's RECORD_DIGEST. Its own tests hold it.
    found.discard("packages/tools/tests/fixtures/access_forms/model")
    # Not a model tree: the editor-schema limitation corpus (test_editor_schema_limits.py);
    # its `compositions/Root2.yaml` is a second-root document, and nothing generates from it.
    found.discard("packages/tools/tests/fixtures/editor_schema/accepted_but_invalid")
    # Not a model tree: test_unknown_key_oracle.py's per-file corpus, whose references name
    # elements it does not hold; nothing generates from it.
    found.discard("packages/tools/tests/fixtures/unknown_key_oracle/model")
    # Not recorded: it postdates the record; test_diagram_generation.py draws it fresh.
    found.discard("packages/tools/tests/fixtures/diagram_collisions/model")
    assert found == set(WITHOUT_A_STUB)


@pytest.mark.parametrize("builder", sorted(WITH_A_STUB_THAT_LISTS_NOTHING))
@pytest.mark.parametrize("index", [0, 1], ids=["arxml-4.2.2", "arxml-4.3.0"])
def test_a_stub_that_lists_nothing_writes_what_it_wrote_before(builder: str, index: int, tmp_path: Path) -> None:
    model_root = _STUB_BUILDERS[builder](tmp_path / "model")
    output = tmp_path / "model.arxml"
    _generate(model_root, _ARTIFACTS[index], output)

    assert _sha(output.read_bytes()) == WITH_A_STUB_THAT_LISTS_NOTHING[builder][index]


def test_the_stub_models_list_nothing(tmp_path: Path) -> None:
    """The premise of the record above, read off the files: no interface stub of either model lists a member."""
    seen = 0
    for builder in _STUB_BUILDERS.values():
        model_root = builder(tmp_path / builder.__name__)
        stubs = [
            document
            for path in (model_root / "interfaces").glob("*.yaml")
            if (document := yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kind") == "external_interface"
        ]
        assert not [d for d in stubs if d.get("operations") or d.get("data_elements")]
        seen += len(stubs)
    assert seen == 4, "the premise check reads the four interface stubs external_stubs_support declares"
