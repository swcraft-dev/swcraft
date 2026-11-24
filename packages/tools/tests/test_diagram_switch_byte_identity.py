"""The ARXML did not move when the C4 generator left: a record of every committed model tree's ARXML digests.

:data:`RECORDED` holds, per tree, the SHA-256 of what ``generate_arxml`` writes at 4.2.2 and 4.3.0, compared after
``termination_strip_support.strip_recorded`` removes the ``HANDLE-TERMINATION-AND-RESTART`` lines every component gained
(``test_termination_refreeze.py`` gates the helper). A tree the writer refuses carries the refusal's text instead, so a
refusal that starts generating, or changes its sentence, fails too. The third column (the retired RST generator's) is
kept as recorded and no case reads it.

**This is a record, not a golden to refresh.** A moved digest is a change to ARXML output that carries its own
qualification impact and is re-measured there, with the reason, never to make this module pass. Most rows hold the
bytes written before the switch; three were re-measured under an authorised output change: ``component_exchange`` and
``init_values`` (the application types, then the enum category) and ``compositions`` (the stub's ``external_ref``
lost two package names; only that path's bytes moved).
``test_enum_category_refreeze.py`` maps each re-measured ``component_exchange`` and ``init_values`` document back to
its earlier digest, and ``test_application_types_byte_identity.py`` asserts the rows the two records share agree.

What it does not reach: a model tree that is not committed (real tool exports among them), a run through the CLI rather
than the function (``test_cli.py`` owns that surface), and the component exports of ``generate arxml --component``,
which the component-exchange suites pin.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import REPO_ROOT, is_data_fixture_tree
from swcraft_tools.writer import generate_arxml
from termination_strip_support import strip_recorded

#: Per tree: ARXML 4.2.2, ARXML 4.3.0, and the unread RST column (see the module docstring).
RECORDED: dict[str, tuple[str, str, str]] = {
    "examples/basic/model": (
        "83929b4cef3a626d99a30f916a447ab19bb36cce7ddd39ea044774d2404e1564",
        "da62aebffffadcfb21b575951537f24ecbf8e042ba9382cd1846fc58a55868dc",
        "23242c5c8c13fed1478c622b63e33c8d3a3b3598b7116d99213747bc67c8d7bf",
    ),
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "fd4e91236a437658b937f47a678c090d97f7758bae2e69e3b4aaecafe75b7a97",
        "f6546472bfe78f33dd2d329102a0c79f3b74b41b324c596c29f86cac54167d23",
        "1a4235d2e71dcd1e49cd62dc7ab36a622bc0e05af39446ca8122aab8046bac37",
    ),
    "packages/tools/tests/fixtures/access_points/model": (
        "ff4ec89fa9b2746e14bcfd019b8a31dd2f3252c02a9afedb1fb12f08cdf15cab",
        "ba0d1e536e5dfad22b123ab2d7e153c115bc3deafd9649397ecf9d80603344bb",
        "4cbe34cee65a71170a89cb637816b3b4ea9b4544a948747d8d67761f6a5260b3",
    ),
    "packages/tools/tests/fixtures/compatible_interfaces/model": (
        "34a7f10ed572b6846b981c2daf9a5309356de49f0ea8ce6c9087d69ae4c379c9",
        "516a32ebcb06046d45600bc3a2444f5fc8c26775b777c438d0acff20a51c56a8",
        "9627e1109988b2f232dc51726bae5550535257438a4bc3d68c8ac6c4f4f97424",
    ),
    # ARXML re-measured for the application types, then the enum category (the module docstring).
    "packages/tools/tests/fixtures/component_exchange/model": (
        "8d1ff2fb9f850c063623d66ef85cfc0c1b0181aa2606be3efb4fe900956f0a4f",
        "42f678b4ef92c5d00e5e4b1247278e13f909875f5572919aa804c1fe3a71c11c",
        "65a29a30a81d62e91183cf8a2a61964b46028aa5a713e70d8b90c2e9434261fb",
    ),
    # Re-measured: the stub's `external_ref` lost two package names; no other byte moved.
    "packages/tools/tests/fixtures/compositions/model": (
        "c85aa2a56f109fb97f319a31ca18da491360e3165a2635cce8d3c0e0d09c1cd2",
        "f98f0551903d72e1df3076f7efb275f4b10149134df12f50abd424c24274f018",
        "7e99fed47e450f9d77b2425cbdbd7f6000d5eeff7dfa2f529464db4c729e08ed",
    ),
    "packages/tools/tests/fixtures/diagrams/model": (
        "c9d44b58466bac4dc655b395b6682789bcb067c4037466c930f824ae7231e2f4",
        "d1601b0f828d2b347319efcd43ca0fe873c56656e606534e8b58bfa47474f87e",
        "94512f2b18060e9e495f14f323a5a964ab3005f9497e1224795e2640741df6aa",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "9e4d2c11f1c9767dee793f3427e34ae0142a68d98a770976f128a752d41753d9",
        "7d0e76701b511afb592956be15737258365f3d4a5e17e6914f326b6089e65917",
        "1a4235d2e71dcd1e49cd62dc7ab36a622bc0e05af39446ca8122aab8046bac37",
    ),
    # ARXML re-measured for the application types, then the enum category (the module docstring).
    "packages/tools/tests/fixtures/init_values/model": (
        "a80b613c89b9844cf32b36af31bd2d1ffc83e15d389ad76d8cd58fddbc1eb6e1",
        "bb588bfc553e662d98d448067904ee3912e2d68e9a10d3cb3ef45d0f9691adac",
        "b66da8b2759096bc82be3a11dbc71fef532bad7dd5d9c7cd29113d3cb8c8f1a9",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "5fbc3996361702e4e08bc274461395ef635ae2b34ddfd797ff1a5739f62c8c9b",
        "e8f15a5dabe5f01acb2461613d585760b5009935e0e592a4b80b4e57c563017f",
        "ed3b1f5115e15a112194f4247ce76a020c7846b064b7012e0c9f8d1709b9bafc",
    ),
    # Refused at both versions: the writer's first refusal is the unmapped application type of ``SpeedControl``'s
    # variable, raised by the up-front guard before the ``service`` component is reached.
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": (
        "refused: 'SpeedControl' speaks 'SpeedPhys'",
        "refused: 'SpeedControl' speaks 'SpeedPhys'",
        "25bfbd8565ed02bdf41a9bc9667c11d9165673079316a9fc7fad783e9ff50639",
    ),
    "packages/tools/tests/fixtures/safety/model": (
        "48e3b364c180af694d8cae6f358ba86deaea40501651c92fecb47e9c2a694a3c",
        "5876522deaf4f3d798354cef5d1dd603b210f8a80c595b6150ef98b0c4434240",
        "570cc7793f88d09edebafb374980409509322600cb3325a7d9607ace67fe0a7c",
    ),
    "packages/tools/tests/fixtures/showcase/model": (
        "0017e93065afb2436f80cee657a7ad49ccceaa2a8c2e63cfe0528ea965dd3536",
        "f20eda2cc76b224e421e077057c1a1b6575c0c288b196acc3adc5be66c9466a0",
        "6555de39961fe542d657ce2138c71b9bb8b783b609e97c2e43a2a975f520de45",
    ),
}

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [(tree, artifact, RECORDED[tree][index]) for tree in RECORDED for index, artifact in enumerate(_ARTIFACTS)]


def _generate(model_root: Path, artifact: str, output: Path) -> None:
    generate_arxml(str(model_root), str(output), version=artifact.removeprefix("arxml-"))


@pytest.mark.parametrize(("tree", "artifact", "recorded"), _CASES, ids=[f"{t}:{a}" for t, a, _ in _CASES])
def test_the_generated_bytes_are_the_branch_points(tree: str, artifact: str, recorded: str, tmp_path: Path) -> None:
    model_root = REPO_ROOT / tree
    assert model_root.is_dir(), f"{tree} is recorded but not committed"
    output = tmp_path / artifact
    if recorded.startswith("refused: "):
        with pytest.raises(ValueError, match=recorded.removeprefix("refused: ")):
            _generate(model_root, artifact, output)
        return
    _generate(model_root, artifact, output)
    assert hashlib.sha256(strip_recorded(tree, artifact, output.read_bytes())).hexdigest() == recorded


def test_every_committed_model_tree_is_recorded() -> None:
    """A model tree committed later without a record would be outside the claim above; name it here.

    A tree is recognised by a YAML file in its ``compositions/`` directory under ``examples/``, the ``init`` template
    and ``packages/tools/tests/fixtures/``. Not reached: the gitignored guide payload and a tree outside these three
    places; the trees under ``conftest.DATA_FIXTURE_ROOTS`` are left out.
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
    # Not recorded: the fixture states units, compu methods, data constraints and every reference field, which no
    # recorded tree's "before" bytes can cover; test_data_description_writer.py and test-xsd hold its output.
    found.discard("packages/tools/tests/fixtures/data_description/model")
    # Not recorded: the access forms fixture states implicit accesses, asynchronous calls and the call-returned event,
    # which no earlier generator could load; it is the *with* half's subject of the access-forms tests.
    found.discard("packages/tools/tests/fixtures/access_forms/model")
    # Not a model tree: `accepted_but_invalid/` is test_editor_schema_limits.py's limitation corpus, and its
    # `compositions/Root2.yaml` is one class's second-root document. Nothing generates from it.
    found.discard("packages/tools/tests/fixtures/editor_schema/accepted_but_invalid")
    # Not a model tree: `unknown_key_oracle/` is test_unknown_key_oracle.py's per-file corpus; its references name
    # elements it does not hold, so nothing runs check-model or generates from it.
    found.discard("packages/tools/tests/fixtures/unknown_key_oracle/model")
    # Not recorded: `diagram_collisions/` holds the names a drawn element's model entry must tell apart; it postdates
    # the generator switch, so it has no "before" bytes; test_diagram_generation.py checks every entry it writes.
    found.discard("packages/tools/tests/fixtures/diagram_collisions/model")
    assert found == set(RECORDED)
