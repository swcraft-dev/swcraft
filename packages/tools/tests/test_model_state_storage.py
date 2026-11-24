"""The store keeps every name: ``model_state.free_storage_path`` and the element order.

**One function decides a free file name.** A collision between two file names never touches
the element: the first writer gets the plain name, the next the nearest free ``~1``, ``~2``, ….
Occupancy is decided by SWCraft's own case-insensitive comparison of the directory's *listing*
-- every entry, not the reader's ``*.yaml`` view -- and never by asking the filesystem, which is
why the answer is asserted identical on a case-sensitive and a case-insensitive root.

**Order is by identity, never by file name.** The loader sorts each layer's parsed elements by
their composed storage path (placement, then identity), ordinally; ``scan_layer`` keeps answering
its files in path order. Both loading paths, :func:`~swcraft_tools.model_state.load_layer` and
:func:`~swcraft_tools.model_state.load_model_state`, and the SWC placement reader are asserted.

**What these tests do not reach.**

- Unicode normalisation: two identities differing only in normalisation form collide on APFS
  and not on NTFS, and ``casefold()`` is not asked about them. No case here builds one.
- The writers: nothing here proves that any caller asks :func:`free_storage_path`; this module
  pins what it answers when asked.
- The rename gate covers ``examples/basic`` only. A generator reading some fact of a file
  *other* than its position (its name inside a message, say) is outside what bytes of a
  successful generation can show.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, _root_composition, element_path
from filesystem_support import case_sensitive_under, listing_refusal_for_a_non_directory
from node_support import requires_node
from swcraft_tools import model_state as ms
from swcraft_tools.editing import registry as editing_registry
from swcraft_tools.model_registry import ModelRegistryError, compose_storage_path, production_registry
from swcraft_tools.model_state import load_layer, load_model_state, scan_layer
from swcraft_tools.reader import read_swc_model_with_placement
from swcraft_tools.writer import mutations
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.diagrams import generate_diagrams
from swcraft_tools.writer.needs_export import generate_needs

HARDWARE_FIXTURE_MODEL = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "hardware" / "model"
EXAMPLE_MODEL = REPO_ROOT / "examples" / "basic" / "model"


def _write_element(path: Path, document: dict[str, Any]) -> None:
    """One YAML file at *path*, whatever its name says."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _primitive(root: Path, filename: str, identity: str, *, directory: str = "data_types/implementation") -> Path:
    """A base type declaring *identity*, stored under *filename* in *directory*.

    The storage-name tests read names only, so they keep the ``implementation_types`` directory;
    a test that parses the layer passes ``directory=BASE_DIRECTORY``, where a base type parses.
    """
    path = root / directory / filename
    _write_element(path, {"name": identity, "kind": "base_type", "size": 8, "encoding": "unsigned"})
    return path


#: The ``base_types`` layer's directory, read off the contract.
BASE_DIRECTORY = production_registry().storage("base_types").directory


def _free(root: Path, identity: str, **extra: Any) -> ms.FreePath:
    return ms.free_storage_path(root, "implementation_types", identity, registry=production_registry(), **extra)


def _scan_layer_files(model_root: Path) -> list[Path]:
    """Every element file of every layer, through the one traversal."""
    registry = production_registry()
    return [entry.path for layer in registry.layer_names for entry in scan_layer(model_root, layer).files]


# ---------------------------------------------------------------------------
# The signature
# ---------------------------------------------------------------------------


def test_the_registry_is_keyword_only_and_required() -> None:
    """No ambient default: a caller inside ``editing/`` passes its projection's kernel registry."""
    parameter = inspect.signature(ms.free_storage_path).parameters["registry"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_a_projection_with_no_kernel_registry_is_an_engine_defect_and_raises(tmp_path: Path) -> None:
    """``reg.Registry.kernel_model`` may be ``None``; passing it on is refused, never defaulted."""
    projection = editing_registry.build_registry()
    assert projection.kernel_model is None

    with pytest.raises(TypeError, match="kernel"):
        ms.free_storage_path(tmp_path, "implementation_types", "Speed", registry=projection.kernel_model)


# ---------------------------------------------------------------------------
# The plain name, occupied by self, and the suffix
# ---------------------------------------------------------------------------


def test_an_empty_or_absent_directory_answers_the_plain_path(tmp_path: Path) -> None:
    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32.yaml", suffixed=False, occupied_by_self=False
    )
    (tmp_path / "data_types" / "implementation").mkdir(parents=True)
    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32.yaml", suffixed=False, occupied_by_self=False
    )


def test_a_same_name_file_declaring_the_same_identity_is_occupied_by_self(tmp_path: Path) -> None:
    _primitive(tmp_path, "Float32.yaml", "Float32")

    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32.yaml", suffixed=False, occupied_by_self=True
    )


def test_a_same_name_file_declaring_another_identity_gives_the_first_suffix(tmp_path: Path) -> None:
    _primitive(tmp_path, "Float32.yaml", "VehicleSpeed")

    answer = _free(tmp_path, "Float32")

    assert answer == ms.FreePath("data_types/implementation/Float32~1.yaml", suffixed=True, occupied_by_self=False)
    assert answer.path.endswith("~1.yaml"), "the suffix precedes the file suffix"


def test_a_file_differing_only_in_case_occupies_the_name_on_every_os(tmp_path: Path) -> None:
    _primitive(tmp_path, "float32.yaml", "float32")

    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32~1.yaml", suffixed=True, occupied_by_self=False
    )


def test_a_taken_first_suffix_gives_the_second(tmp_path: Path) -> None:
    _primitive(tmp_path, "Float32.yaml", "float32")
    _primitive(tmp_path, "Float32~1.yaml", "FLOAT32")

    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32~2.yaml", suffixed=True, occupied_by_self=False
    )


def test_an_identity_ending_in_a_suffix_shape_collides_with_nothing(tmp_path: Path) -> None:
    """``~`` and digits at the end of an identity are not reserved — nothing parses a suffix."""
    _primitive(tmp_path, "Boot.yaml", "Boot")

    assert _free(tmp_path, "Boot~3") == ms.FreePath(
        "data_types/implementation/Boot~3.yaml", suffixed=False, occupied_by_self=False
    )


def test_a_suffix_candidate_held_by_an_element_genuinely_named_so_is_skipped(tmp_path: Path) -> None:
    _primitive(tmp_path, "Boot.yaml", "boot")
    _primitive(tmp_path, "Boot~1.yaml", "Boot~1")

    assert _free(tmp_path, "Boot") == ms.FreePath(
        "data_types/implementation/Boot~2.yaml", suffixed=True, occupied_by_self=False
    )


def test_a_nesting_layer_composes_and_lists_inside_its_placement_directory(tmp_path: Path) -> None:
    registry = production_registry()
    _write_element(element_path(tmp_path, "swc", "Other", group="elsewhere"), {"name": "Foo", "type": "application"})
    _write_element(tmp_path / "swc" / "elsewhere" / "Foo.yaml", {"name": "Bar", "type": "application"})

    assert ms.free_storage_path(tmp_path, "swc", "Foo", {"group": "sensors"}, registry=registry) == ms.FreePath(
        "swc/sensors/Foo.yaml", suffixed=False, occupied_by_self=False
    ), "a file in another group's directory occupies nothing here"

    _write_element(tmp_path / "swc" / "sensors" / "foo.yaml", {"name": "foo", "type": "application"})
    assert ms.free_storage_path(tmp_path, "swc", "Foo", {"group": "sensors"}, registry=registry) == ms.FreePath(
        "swc/sensors/Foo~1.yaml", suffixed=True, occupied_by_self=False
    )


def test_the_plain_path_is_path_joined_so_a_placement_value_that_is_no_segment_drops(tmp_path: Path) -> None:
    """An empty or ``.`` placement value contributes no segment: ``swc/Foo.yaml``, never ``swc//Foo.yaml``.

    So the string that reaches the audit entry, the ``reserved`` comparison and the report line
    is the file written.
    """
    registry = production_registry()

    for group in ("", "."):
        assert ms.free_storage_path(tmp_path, "swc", "Foo", {"group": group}, registry=registry) == ms.FreePath(
            "swc/Foo.yaml", suffixed=False, occupied_by_self=False
        ), group

    _write_element(tmp_path / "swc" / "Foo.yaml", {"name": "Other", "type": "application"})

    for group in ("", "."):
        assert ms.free_storage_path(tmp_path, "swc", "Foo", {"group": group}, registry=registry) == ms.FreePath(
            "swc/Foo~1.yaml", suffixed=True, occupied_by_self=False
        ), group


def test_the_plain_path_is_the_one_function_the_order_key_and_the_free_path_share(tmp_path: Path) -> None:
    """:func:`~swcraft_tools.model_registry.compose_storage_path` is the one spelling of the plain path."""
    swc = production_registry().storage("swc")

    assert compose_storage_path(swc, "Foo", {"group": "sensors"}) == "swc/sensors/Foo.yaml"
    assert compose_storage_path(swc, "Foo", {"group": ""}) == "swc/Foo.yaml"
    assert compose_storage_path(swc, "Foo", {"group": "."}) == "swc/Foo.yaml"
    assert compose_storage_path(production_registry().storage("implementation_types"), "Float32") == (
        "data_types/implementation/Float32.yaml"
    )
    assert not hasattr(ms, "plain_storage_path")
    # A nesting record with no placement value at all is a caller with no directory to write
    # into, not a spelling to normalise.
    with pytest.raises(ModelRegistryError):
        compose_storage_path(swc, "Foo")


def test_the_decision_is_taken_against_the_tree_as_it_stands_at_each_call(tmp_path: Path) -> None:
    """Never a pre-pass: two writes of a case-only pair get the plain name and ``~1``."""
    first = _free(tmp_path, "float32")
    assert first.path == "data_types/implementation/float32.yaml"
    _primitive(tmp_path, Path(first.path).name, "float32")

    second = _free(tmp_path, "Float32")
    assert second == ms.FreePath("data_types/implementation/Float32~1.yaml", suffixed=True, occupied_by_self=False)


def test_the_answer_does_not_depend_on_the_filesystems_case_sensitivity(tmp_path: Path) -> None:
    """The same answer on both kinds of root, because the filesystem is never asked.

    Probe-gated and never skipped: each branch asserts what the filesystem itself would answer,
    so the function's single answer is shown to be its own.
    """
    _primitive(tmp_path, "float32.yaml", "float32")
    filesystem_says_taken = (tmp_path / "data_types" / "implementation" / "Float32.yaml").exists()

    if case_sensitive_under(tmp_path):
        assert not filesystem_says_taken, "a case-sensitive root holds no Float32.yaml"
        # The shape only this root can hold: both spellings on disk at once.
        _primitive(tmp_path, "Float32.yaml", "Float32_other")
        assert _free(tmp_path, "FLOAT32") == ms.FreePath(
            "data_types/implementation/FLOAT32~1.yaml", suffixed=True, occupied_by_self=False
        )
    else:
        assert filesystem_says_taken, "a case-insensitive root answers for float32.yaml"

    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32~1.yaml", suffixed=True, occupied_by_self=False
    )


# ---------------------------------------------------------------------------
# Occupancy sees every entry, and what is reserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "occupant",
    ["Foo.YAML", "foo.yaml", "a directory named Foo.yaml"],
)
def test_every_entry_of_the_directory_occupies_its_name(tmp_path: Path, occupant: str) -> None:
    """Not ``scan_layer``'s ``*.yaml`` view: a ``Foo.YAML`` or a directory is the same name on APFS."""
    directory = tmp_path / "data_types" / "implementation"
    directory.mkdir(parents=True)
    if occupant.startswith("a directory"):
        (directory / "Foo.yaml").mkdir()
    else:
        (directory / occupant).write_text("name: Bar\n", encoding="utf-8", newline="\n")

    assert _free(tmp_path, "Foo") == ms.FreePath(
        "data_types/implementation/Foo~1.yaml", suffixed=True, occupied_by_self=False
    )


def test_reserved_names_occupy_like_files(tmp_path: Path) -> None:
    """A planning caller passes what it has allocated and not yet written."""
    assert _free(tmp_path, "Foo", reserved=("data_types/implementation/foo.yaml",)) == ms.FreePath(
        "data_types/implementation/Foo~1.yaml", suffixed=True, occupied_by_self=False
    )
    assert _free(
        tmp_path, "Foo", reserved=("data_types/implementation/Foo.yaml", "data_types/implementation/FOO~1.yaml")
    ) == ms.FreePath("data_types/implementation/Foo~2.yaml", suffixed=True, occupied_by_self=False)


def test_a_reserved_name_in_another_directory_occupies_nothing(tmp_path: Path) -> None:
    assert _free(tmp_path, "Foo", reserved=("interfaces/Foo.yaml",)) == ms.FreePath(
        "data_types/implementation/Foo.yaml", suffixed=False, occupied_by_self=False
    )


# ---------------------------------------------------------------------------
# PlannedStorage -- the same answers, one listing per planning pass
# ---------------------------------------------------------------------------


def _one_shot(root: Path, layer: str, identities: list[str], placement: dict[str, str] | None = None) -> list[str]:
    """Every identity asked of :func:`free_storage_path` with the earlier answers reserved, as callers did."""
    reserved: list[str] = []
    for identity in identities:
        reserved.append(
            ms.free_storage_path(
                root, layer, identity, placement, registry=production_registry(), reserved=reserved
            ).path
        )
    return reserved


def _planned(root: Path, layer: str, identities: list[str], placement: dict[str, str] | None = None) -> list[str]:
    storage = ms.PlannedStorage(root, registry=production_registry())
    return [storage.allocate(layer, identity, placement).path for identity in identities]


#: Case variants held on disk, case variants reserved in the run, a suffix-shaped identity, the
#: element's own file -- every way a name is held, in one pass.
_PLANNED_IDENTITIES = ["Foo", "foo", "FOO", "Bar", "Foo~1", "fOO", "Baz", "Own", "OWN", "bar"]


def test_planned_storage_answers_case_collisions_with_existing_files_and_within_the_run(tmp_path: Path) -> None:
    _primitive(tmp_path, "foo.yaml", "foo-on-disk")
    _primitive(tmp_path, "FOO~2.yaml", "suffix-on-disk")
    _primitive(tmp_path, "own.yaml", "Own")
    assert _planned(tmp_path, "implementation_types", _PLANNED_IDENTITIES) == [
        "data_types/implementation/Foo~1.yaml",  # foo.yaml holds the plain name
        "data_types/implementation/foo~3.yaml",  # ~1 reserved in the run, ~2 on disk
        "data_types/implementation/FOO~4.yaml",
        "data_types/implementation/Bar.yaml",
        "data_types/implementation/Foo~1~1.yaml",  # Foo~1.yaml reserved by the first answer
        "data_types/implementation/fOO~5.yaml",
        "data_types/implementation/Baz.yaml",
        "data_types/implementation/own.yaml",  # occupied by self, spelled as on disk
        "data_types/implementation/OWN~1.yaml",
        "data_types/implementation/bar~1.yaml",
    ]


def test_planned_storage_and_the_one_shot_path_agree_on_the_same_inputs(tmp_path: Path) -> None:
    _primitive(tmp_path, "foo.yaml", "foo-on-disk")
    _primitive(tmp_path, "FOO~2.yaml", "suffix-on-disk")
    _primitive(tmp_path, "own.yaml", "Own")
    identities = [*_PLANNED_IDENTITIES, *(f"Foo~{n}" for n in range(1, 6)), *(f"foo{n % 3}" for n in range(12))]
    assert _planned(tmp_path, "implementation_types", identities) == _one_shot(
        tmp_path, "implementation_types", identities
    )


def test_planned_storage_keeps_one_index_per_directory(tmp_path: Path) -> None:
    """A nesting layer's answers in one group reserve nothing in another, as a reserved path does not."""
    registry = production_registry()
    storage = ms.PlannedStorage(tmp_path, registry=registry)
    sequence = [("sensors", "Foo"), ("actuators", "foo"), ("sensors", "FOO"), ("actuators", "Foo")]
    answers = [storage.allocate("swc", identity, {"group": group}).path for group, identity in sequence]
    reserved: list[str] = []
    for group, identity in sequence:
        reserved.append(
            ms.free_storage_path(tmp_path, "swc", identity, {"group": group}, registry=registry, reserved=reserved).path
        )
    assert (
        answers
        == reserved
        == ["swc/sensors/Foo.yaml", "swc/actuators/foo.yaml", "swc/sensors/FOO~1.yaml", "swc/actuators/Foo~1.yaml"]
    )


def test_planned_storage_reserves_nothing_for_a_refused_allocation(tmp_path: Path) -> None:
    long_identity = "L" * 198
    storage = ms.PlannedStorage(tmp_path, registry=production_registry())
    assert (
        storage.allocate("implementation_types", long_identity).path
        == f"data_types/implementation/{long_identity}.yaml"
    )
    with pytest.raises(ms.StorageNameRefusal):
        storage.allocate("implementation_types", long_identity)
    assert storage.allocate("implementation_types", "Foo").path == "data_types/implementation/Foo.yaml"


def test_planned_storage_needs_the_kernel_registry(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="PlannedStorage needs the kernel ModelRegistry"):
        ms.PlannedStorage(tmp_path, registry=None)  # type: ignore[arg-type]


def test_a_suffixed_candidate_is_held_to_the_file_name_bound(tmp_path: Path) -> None:
    """An identity with no room for ``~1`` is refused, naming the bound.

    ``model_registry`` admits an identity of up to 198 bytes, because ``.yaml`` plus the commit's
    ``.<32 hex>.swcraft-commit.tmp`` fills a 255-byte component exactly. ``~1`` adds two bytes.
    """
    at_the_bound = "A" * 197
    _primitive(tmp_path, f"{at_the_bound}.yaml", "Other")

    with pytest.raises(ms.StorageNameRefusal) as refused:
        _free(tmp_path, at_the_bound)
    assert "198" in str(refused.value)
    assert "'implementation_types'" in str(refused.value)


def test_the_bound_leaves_room_for_a_suffix_that_fits(tmp_path: Path) -> None:
    fits = "B" * 196
    assert _free(tmp_path, "A" * 197) == ms.FreePath(
        f"data_types/implementation/{'A' * 197}.yaml", suffixed=False, occupied_by_self=False
    ), "a plain name is the identity guard's question, not this one's"

    _primitive(tmp_path, f"{fits}.yaml", "Other")
    assert _free(tmp_path, fits) == ms.FreePath(
        f"data_types/implementation/{fits}~1.yaml", suffixed=True, occupied_by_self=False
    )


def test_the_bound_is_asked_again_of_a_two_digit_suffix(tmp_path: Path) -> None:
    """A 196-byte identity fits ``~1`` … ``~9`` (198 bytes) and not ``~10`` (199)."""
    identity = "C" * 196
    _primitive(tmp_path, f"{identity}.yaml", "Other")
    for n in range(1, 10):
        _primitive(tmp_path, f"{identity}~{n}.yaml", f"Other{n}")

    with pytest.raises(ms.StorageNameRefusal, match="'~10'"):
        _free(tmp_path, identity)


def test_the_bound_counts_bytes_not_characters(tmp_path: Path) -> None:
    """``é`` is two UTF-8 bytes: 98 of them take ``~1`` at 198 bytes, 99 of them cannot (200 bytes, 101 characters)."""
    fits, too_long = "é" * 98, "é" * 99
    _primitive(tmp_path, f"{fits}.yaml", "Other")
    _primitive(tmp_path, f"{too_long}.yaml", "Another")

    assert _free(tmp_path, fits) == ms.FreePath(
        f"data_types/implementation/{fits}~1.yaml", suffixed=True, occupied_by_self=False
    )
    with pytest.raises(ms.StorageNameRefusal):
        _free(tmp_path, too_long)


def test_a_case_variant_occupant_declaring_the_identity_is_occupied_by_self_spelled_as_on_disk(tmp_path: Path) -> None:
    """``float32.yaml`` declaring ``Float32`` is that element's file: never a second one at ``Float32~1.yaml``."""
    _primitive(tmp_path, "float32.yaml", "Float32")

    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/float32.yaml", suffixed=False, occupied_by_self=True
    )


def test_the_exact_spelling_is_preferred_among_occupants_declaring_the_identity(tmp_path: Path) -> None:
    """The composed spelling wins among occupants declaring the identity, whichever lists first.

    Probe-gated and never skipped: both spellings on disk exist only on a case-sensitive root; on a
    case-insensitive one the one file is the answer, spelled as the root holds it.
    """
    _primitive(tmp_path, "foo.yaml", "Foo")
    if case_sensitive_under(tmp_path):
        _primitive(tmp_path, "Foo.yaml", "Foo")
        assert sorted(path.name for path in (tmp_path / "data_types" / "implementation").iterdir()) == [
            "Foo.yaml",
            "foo.yaml",
        ]
        expected = "data_types/implementation/Foo.yaml"
    else:
        assert [path.name for path in (tmp_path / "data_types" / "implementation").iterdir()] == ["foo.yaml"]
        expected = "data_types/implementation/foo.yaml"

    assert _free(tmp_path, "Foo") == ms.FreePath(expected, suffixed=False, occupied_by_self=True)


@pytest.mark.parametrize(
    "content",
    [b"name: [unclosed\n", b"- a list, not a mapping\n", b"name: 17\n", b"name: \xff\xfe not UTF-8\n"],
    ids=["unparseable", "not-a-mapping", "not-a-string", "not-utf-8"],
)
def test_an_occupant_that_declares_nothing_readable_gives_the_first_suffix(tmp_path: Path, content: bytes) -> None:
    """A file at the plain name that declares no identity is held by something else, never by the element."""
    directory = tmp_path / "data_types" / "implementation"
    directory.mkdir(parents=True)
    (directory / "Foo.yaml").write_bytes(content)

    assert _free(tmp_path, "Foo") == ms.FreePath(
        "data_types/implementation/Foo~1.yaml", suffixed=True, occupied_by_self=False
    )
    assert ms.declared_identity(directory / "Foo.yaml", "name") is None


# ---------------------------------------------------------------------------
# The store's three trees: a directory, no directory, and a file
# ---------------------------------------------------------------------------
#
# Three answers: the directory's entries, nothing held, and a refusal naming the model, not the
# absolute path the OS was handed.


#: The two layer shapes, each with the create that reaches the store for it: a flat layer and a
#: nesting layer whose directory is composed from a placement value.
_A_CREATE_PER_LAYER_SHAPE = (
    pytest.param(
        "tasks",
        "tasks",
        lambda root: mutations.create_task(str(root), "Probe", 0.01, 10, "CORE0"),
        id="flat-layer",
    ),
    pytest.param(
        "swc",
        "swc/sensors",
        lambda root: mutations.create_swc(str(root), "Probe", "sensors"),
        id="nesting-layer",
    ),
)


@pytest.mark.parametrize(("layer", "storage_name", "create"), _A_CREATE_PER_LAYER_SHAPE)
def test_a_file_where_a_layer_directory_belongs_refuses_the_create(
    tmp_path: Path, layer: str, storage_name: str, create: Any
) -> None:
    """The create refuses and writes nothing, on every host, however its listing complains.

    Where the host answers ``ENOTDIR`` the store refuses first; elsewhere the writer's ``mkdir``
    does. Both are ``OSError``.
    """
    model_root = tmp_path / "model"
    occupant = model_root / storage_name
    occupant.parent.mkdir(parents=True, exist_ok=True)
    occupant.write_text("a file, not a layer directory\n", encoding="utf-8", newline="\n")

    with pytest.raises(OSError):
        create(model_root)

    assert occupant.read_text(encoding="utf-8") == "a file, not a layer directory\n"
    assert occupant.is_file(), "the occupant was neither replaced nor turned into a directory"


@pytest.mark.parametrize(("layer", "storage_name", "create"), _A_CREATE_PER_LAYER_SHAPE)
def test_a_file_where_a_layer_directory_belongs_is_refused_naming_the_layer_and_the_storage_name(
    tmp_path: Path, layer: str, storage_name: str, create: Any
) -> None:
    """On a host whose listing of a file is ``ENOTDIR``, the refusal names the layer and storage name.

    It branches on ``filesystem_support.listing_refusal_for_a_non_directory`` rather than assuming
    an unmeasured host's error class; elsewhere it skips naming what the host answered. The
    message carries the model-root-relative storage name and no absolute path.
    """
    model_root = tmp_path / "model"
    occupant = model_root / storage_name
    occupant.parent.mkdir(parents=True, exist_ok=True)
    occupant.write_text("a file, not a layer directory\n", encoding="utf-8", newline="\n")
    probed = listing_refusal_for_a_non_directory(tmp_path)

    if not isinstance(probed, NotADirectoryError):
        # A skip that reports what the host answered, so the run says which half went unchecked;
        # the unconditional twin above carries the platform-independent claim.
        pytest.skip(
            "this host answers "
            + ("no error" if probed is None else type(probed).__name__)
            + " to a listing of a regular file, not ENOTDIR, so the phrased "
            "StorageDirectoryRefusal is not this host's claim"
        )

    with pytest.raises(ms.StorageDirectoryRefusal) as refused:
        create(model_root)

    message = str(refused.value)
    assert f"'{layer}'" in message
    assert f"'{storage_name}'" in message
    assert str(tmp_path) not in message and str(model_root) not in message
    assert isinstance(refused.value, NotADirectoryError), "the OS's own lineage is kept, so an `except OSError` holds"


def test_an_absent_layer_directory_still_holds_nothing_where_a_file_is_refused(tmp_path: Path) -> None:
    """An absent layer directory holds nothing; a file at that name is refused."""
    assert _free(tmp_path, "Float32") == ms.FreePath(
        "data_types/implementation/Float32.yaml", suffixed=False, occupied_by_self=False
    )

    (tmp_path / "data_types").mkdir()
    (tmp_path / "data_types" / "implementation").write_text(
        "a file, not a layer directory\n", encoding="utf-8", newline="\n"
    )

    probed = listing_refusal_for_a_non_directory(tmp_path)
    if not isinstance(probed, NotADirectoryError):
        # Only the refused half is scoped to ENOTDIR; the skip records the host that could not check it.
        pytest.skip(
            "this host answers "
            + ("no error" if probed is None else type(probed).__name__)
            + " to a listing of a regular file, not ENOTDIR, so the refused half "
            "of this pair is not this host's claim"
        )
    with pytest.raises(ms.StorageDirectoryRefusal):
        _free(tmp_path, "Float32")


# ---------------------------------------------------------------------------
# The element order
# ---------------------------------------------------------------------------


def _swapped_primitives(root: Path) -> None:
    """``b.yaml`` declares ``A`` and ``a.yaml`` declares ``B``: the two orders disagree."""
    _primitive(root, "b.yaml", "A", directory=BASE_DIRECTORY)
    _primitive(root, "a.yaml", "B", directory=BASE_DIRECTORY)


def test_both_loading_paths_answer_identity_order(tmp_path: Path) -> None:
    _swapped_primitives(tmp_path)

    assert [element.name for element in load_layer(tmp_path, "base_types")] == ["A", "B"]
    snapshot_state = load_model_state(tmp_path)
    assert [element.name for element in snapshot_state["base_types"]] == ["A", "B"]


def test_scan_layer_still_answers_file_order_and_the_two_orders_are_distinct(tmp_path: Path) -> None:
    _swapped_primitives(tmp_path)

    file_order = [entry.path.name for entry in scan_layer(tmp_path, "base_types").files]
    element_order = [element.name for element in load_layer(tmp_path, "base_types")]

    assert file_order == ["a.yaml", "b.yaml"]
    assert element_order == ["A", "B"]
    declared_in_file_order = [
        yaml.safe_load(entry.path.read_text(encoding="utf-8"))["name"]
        for entry in scan_layer(tmp_path, "base_types").files
    ]
    assert declared_in_file_order == ["B", "A"], "the two orders are distinct on this tree"


def test_the_comparison_is_ordinal_and_case_sensitive(tmp_path: Path) -> None:
    """``Zebra`` before ``alpha`` (``'Z'`` 0x5A < ``'a'`` 0x61): never ``casefold``, never a locale."""
    _primitive(tmp_path, "1.yaml", "alpha", directory=BASE_DIRECTORY)
    _primitive(tmp_path, "2.yaml", "Zebra", directory=BASE_DIRECTORY)

    assert [element.name for element in load_layer(tmp_path, "base_types")] == ["Zebra", "alpha"]


def test_two_files_declaring_one_identity_keep_their_file_order(tmp_path: Path) -> None:
    """The sort is stable: the first-claimant rules (``check_hardware_topology``) rely on file order."""
    _primitive(tmp_path, "b_Twice.yaml", "Twice", directory=BASE_DIRECTORY)
    (tmp_path / BASE_DIRECTORY / "a_Twice.yaml").write_text(
        "name: Twice\nkind: base_type\nsize: 16\nencoding: unsigned\n", encoding="utf-8", newline="\n"
    )

    assert [(element.name, element.size) for element in load_layer(tmp_path, "base_types")] == [
        ("Twice", 16),
        ("Twice", 8),
    ]


def test_placement_orders_before_identity(tmp_path: Path) -> None:
    """``swc/a/Z.yaml`` before ``swc/b/A.yaml`` — and inside one group, identity, not file name."""
    _write_element(tmp_path / "swc" / "a" / "Z.yaml", {"name": "Z", "type": "application"})
    _write_element(tmp_path / "swc" / "b" / "A.yaml", {"name": "A", "type": "application"})
    _write_element(tmp_path / "swc" / "b" / "0.yaml", {"name": "C", "type": "application"})

    expected = [("Z", "a"), ("A", "b"), ("C", "b")]
    assert [(swc.name, placement["group"]) for swc, placement in read_swc_model_with_placement(str(tmp_path))] == (
        expected
    )
    assert [swc.name for swc in load_layer(tmp_path, "swc")] == [name for name, _group in expected]
    assert [swc.name for swc in load_model_state(tmp_path)["swc"]] == [name for name, _group in expected]


def test_the_key_is_the_composed_path_so_a_prefix_continued_below_the_dot_keeps_the_old_order(
    tmp_path: Path,
) -> None:
    """The key is ``compose_storage_path(record, identity, placement)``, composed from content.

    ``REQ-A-1`` sorts before ``REQ-A`` because ``-`` (0x2D) is below the ``.`` (0x2E) of ``.yaml``,
    the order files named after their elements have; a bare identity comparison would reorder
    them. Stored under ``aaa.yaml`` and ``zzz.yaml`` the two keep that order.
    """
    root = tmp_path / "model"
    _write_element(
        root / "requirements" / "zzz.yaml",
        {"id": "REQ-A-1", "title": "Child", "description": "A child.", "req_type": "req", **_REQUIREMENT_SHAPE},
    )
    _write_element(
        root / "requirements" / "aaa.yaml",
        {"id": "REQ-A", "title": "Parent", "description": "A parent.", "req_type": "req", **_REQUIREMENT_SHAPE},
    )

    assert [entry.path.name for entry in scan_layer(root, "requirements").files] == ["aaa.yaml", "zzz.yaml"]
    assert [element.id for element in load_layer(root, "requirements")] == ["REQ-A-1", "REQ-A"]
    assert [element.id for element in load_model_state(root)["requirements"]] == ["REQ-A-1", "REQ-A"]


def test_a_suffixed_file_sorts_by_its_composed_name_beside_its_case_only_twin(tmp_path: Path) -> None:
    """The key's stated limit: the ``~N`` is the file's, so the key never sees it.

    ``Float32~1.yaml`` declaring ``Float32`` sorts as ``data_types/base/Float32.yaml``:
    before ``Float32a`` (``.`` 0x2E < ``a`` 0x61) although its file lists after
    ``Float32a.yaml`` (``~`` 0x7E > ``a``), and so beside ``float32``.
    """
    _primitive(tmp_path, "float32.yaml", "float32", directory=BASE_DIRECTORY)
    _primitive(tmp_path, "Float32~1.yaml", "Float32", directory=BASE_DIRECTORY)
    _primitive(tmp_path, "Float32a.yaml", "Float32a", directory=BASE_DIRECTORY)

    assert [entry.path.name for entry in scan_layer(tmp_path, "base_types").files] == [
        "Float32a.yaml",
        "Float32~1.yaml",
        "float32.yaml",
    ]
    assert [element.name for element in load_layer(tmp_path, "base_types")] == ["Float32", "Float32a", "float32"]


#: The two fields every requirement requires; the link lives in the component's ``satisfies``.
_REQUIREMENT_SHAPE = {"version": 1, "source": "architecture"}


def _prefix_pair_model(model_root: Path) -> None:
    """Two names and two groups of which one is a prefix of the other, continued below ``.`` and ``/``.

    Every file is named after its element, so the files' POSIX path order is the order to keep:
    ``swc/body-ctrl/ZzzSWC.yaml`` lists before ``swc/body/AaaSWC.yaml`` (``-`` 0x2D < ``/``
    0x2F), ``requirements/REQ-A-1.yaml`` before ``requirements/REQ-A.yaml`` (``-`` < ``.``).
    """
    for group, name, requirement in (("body", "AaaSWC", "REQ-A"), ("body-ctrl", "ZzzSWC", "REQ-A-1")):
        _write_element(
            model_root / "swc" / group / f"{name}.yaml",
            {
                "name": name,
                "type": "application",
                "handle_termination_and_restart": "no_support",
                "satisfies": [f"{requirement}@1"],
            },
        )
    _root_composition(model_root, ["AaaSWC", "ZzzSWC"])
    for identity in ("REQ-A", "REQ-A-1"):
        _write_element(
            model_root / "requirements" / f"{identity}.yaml",
            {"id": identity, "title": identity, "description": identity, "req_type": "req", **_REQUIREMENT_SHAPE},
        )
    shutil.copytree(EXAMPLE_MODEL / "hardware", model_root / "hardware")
    for identity, period in (("Task-A", 0.02), ("Task-A-1", 0.05)):
        _write_element(
            model_root / "tasks" / f"{identity}.yaml",
            {
                "name": identity,
                "period": period,
                "priority": 4,
                "partition": "PARTITION0",
                "wcet": "100us",
                "runnables": [],
            },
        )


def _positions(text: str, *needles: str) -> list[int]:
    positions = [text.find(needle) for needle in needles]
    assert -1 not in positions, (needles, positions)
    return positions


def test_a_group_prefix_keeps_its_component_order_in_the_emitted_arxml(tmp_path: Path) -> None:
    """``body`` beside ``body-ctrl``: ``ZzzSWC`` (in ``body-ctrl``) is emitted first, at both versions.

    A bare ``(group, identity)`` key would move ``AaaSWC`` first with no file renamed.
    """
    model_root = tmp_path / "model"
    _prefix_pair_model(model_root)
    out = tmp_path / "out"
    artifacts = _generate_all(model_root, out)

    for version in ("4.2.2", "4.3.0"):
        arxml = artifacts[f"model-{version}.arxml"].decode("utf-8")
        zzz, aaa = _positions(arxml, "<SHORT-NAME>ZzzSWC</SHORT-NAME>", "<SHORT-NAME>AaaSWC</SHORT-NAME>")
        assert zzz < aaa, version


def test_an_id_prefix_pair_is_written_in_key_order_in_the_needs_json(tmp_path: Path) -> None:
    """``REQ-A`` beside ``REQ-A-1`` in ``needs.json``: the shorter id first, whatever the files' order.

    ``needs.json`` sorts its keys, so the reader's order does not reach it.
    """
    model_root = tmp_path / "model"
    _prefix_pair_model(model_root)
    artifacts = _generate_all(model_root, tmp_path / "out")

    text = artifacts["needs.json"].decode("utf-8")
    parent, child = _positions(text, '"REQ-A": {', '"REQ-A-1": {')
    assert parent < child


#: The order for :func:`_flat_beside_grouped_model`, the files' own POSIX path order:
#: ``swc/AaaFlat.yaml`` < ``swc/Alpha/BbbSWC.yaml`` (``a`` 0x61 < ``l`` 0x6C) <
#: ``swc/ZzzFlat.yaml`` (``A`` < ``Z``) < ``swc/zeta/YyySWC.yaml`` (``Z`` 0x5A < ``z`` 0x7A). A
#: flat file sorts **among** the group directories by its own name, so a key that put every flat
#: file on one side of every group would fail whichever side it chose.
_FLAT_AND_GROUPED_ORDER = ["AaaFlat", "BbbSWC", "ZzzFlat", "YyySWC"]


def _flat_beside_grouped_model(model_root: Path, *, placed: bool) -> None:
    """Two components directly under ``swc/`` and two in groups, every file named after its element.

    The order key is the path the storage record would write, built as a ``Path``, so a
    component with no group contributes no directory segment (a ``"/"``-joined
    ``swc//{name}.yaml`` would sort before every group). *placed* writes the four components into
    the root composition; the diagrams draw members in the composition's own order, so they
    witness nothing about the element order.
    """
    for name, group in (("AaaFlat", None), ("ZzzFlat", None), ("BbbSWC", "Alpha"), ("YyySWC", "zeta")):
        directory = model_root / "swc" if group is None else model_root / "swc" / group
        _write_element(
            directory / f"{name}.yaml",
            {
                "name": name,
                "type": "application",
                "handle_termination_and_restart": "no_support",
                "satisfies": [f"REQ-{name}@1"],
            },
        )
    _root_composition(model_root, _FLAT_AND_GROUPED_ORDER if placed else [])
    for name in _FLAT_AND_GROUPED_ORDER:
        _write_element(
            model_root / "requirements" / f"REQ-{name}.yaml",
            {"id": f"REQ-{name}", "title": name, "description": name, "req_type": "req", **_REQUIREMENT_SHAPE},
        )
    shutil.copytree(EXAMPLE_MODEL / "hardware", model_root / "hardware")


def test_a_flat_component_file_keeps_its_place_among_the_group_directories(tmp_path: Path) -> None:
    """A flat component sorts among the group directories by its file name, read and emitted.

    A composed-string key would read ``AaaFlat, ZzzFlat, BbbSWC, YyySWC`` and move the
    ``APPLICATION-SW-COMPONENT-TYPE`` blocks on a model whose files are named after their elements.
    """
    model_root = tmp_path / "model"
    _flat_beside_grouped_model(model_root, placed=False)

    assert [entry.path.relative_to(model_root).as_posix() for entry in scan_layer(model_root, "swc").files] == [
        "swc/AaaFlat.yaml",
        "swc/Alpha/BbbSWC.yaml",
        "swc/ZzzFlat.yaml",
        "swc/zeta/YyySWC.yaml",
    ]
    assert [swc.name for swc in load_layer(model_root, "swc")] == _FLAT_AND_GROUPED_ORDER
    assert [swc.name for swc in load_model_state(model_root)["swc"]] == _FLAT_AND_GROUPED_ORDER

    artifacts = _generate_all(model_root, tmp_path / "out")
    for version in ("4.2.2", "4.3.0"):
        arxml = artifacts[f"model-{version}.arxml"].decode("utf-8")
        positions = _positions(arxml, *(f"<SHORT-NAME>{name}</SHORT-NAME>" for name in _FLAT_AND_GROUPED_ORDER))
        assert positions == sorted(positions), version


def test_a_flat_component_placed_in_the_root_composition_keeps_the_same_order(tmp_path: Path) -> None:
    """The same shape as an ordinary project writes it — every component placed.

    The ARXML component **types** are emitted in the element order whether or not a
    composition places them; the prototypes below them follow the composition's own
    member order, which is why this case asserts the types alone.
    """
    model_root = tmp_path / "model"
    _flat_beside_grouped_model(model_root, placed=True)

    assert [swc.name for swc in load_layer(model_root, "swc")] == _FLAT_AND_GROUPED_ORDER
    artifacts = _generate_all(model_root, tmp_path / "out")
    for version in ("4.2.2", "4.3.0"):
        arxml = artifacts[f"model-{version}.arxml"].decode("utf-8")
        positions = _positions(arxml, *(f"<SHORT-NAME>{name}</SHORT-NAME>" for name in _FLAT_AND_GROUPED_ORDER))
        assert positions == sorted(positions), version


def test_the_hardware_fixture_reads_in_identity_order() -> None:
    """The repository's one authored filename ≠ identity: ``Core0.yaml`` … declare ``CORE0`` ….

    Identity order moves the processor from first to fifth (``'O'`` 0x4F < ``'P'`` 0x50).
    """
    file_order = [entry.path.name for entry in scan_layer(HARDWARE_FIXTURE_MODEL, "hardware").files]
    partitions = ["PARTITION0", "PARTITION1", "PARTITION2", "PARTITION3"]
    assert file_order == [
        "CPU.yaml",
        "Core0.yaml",
        "Core1.yaml",
        "Core2.yaml",
        "Core3.yaml",
        *(f"{name}.yaml" for name in partitions),
        "RAM.yaml",
        "ROM.yaml",
    ]

    expected = ["CORE0", "CORE1", "CORE2", "CORE3", "CPU", *partitions, "RAM", "ROM"]
    assert [element.name for element in load_layer(HARDWARE_FIXTURE_MODEL, "hardware")] == expected
    assert [element.name for element in load_model_state(HARDWARE_FIXTURE_MODEL)["hardware"]] == expected


# ---------------------------------------------------------------------------
# The rename gate
# ---------------------------------------------------------------------------


def _generate_all(model_root: Path, out: Path, *, diagrams: bool = False) -> dict[str, bytes]:
    """ARXML at both versions and the Sphinx-Needs ``needs.json``.

    With *diagrams*, every diagram file too, under ``views/``.
    """
    out.mkdir(parents=True)
    generate_arxml(str(model_root), str(out / "model-4.2.2.arxml"), version="4.2.2")
    generate_arxml(str(model_root), str(out / "model-4.3.0.arxml"), version="4.3.0")
    generated = {path.name: path.read_bytes() for path in sorted(out.iterdir(), key=lambda path: path.as_posix())}
    needs = out / "needs.json"
    generate_needs(str(model_root), str(needs), "model")
    generated["needs.json"] = needs.read_bytes()
    if diagrams:
        views = out / "views"
        generate_diagrams(str(model_root), str(views), "model", node=requires_node())
        generated.update({f"views/{path.name}": path.read_bytes() for path in views.iterdir()})
    return generated


def _rename_into_reverse_order(model_root: Path) -> dict[str, str]:
    """Rename every element file so its file order is the reverse of its identity order.

    Returns each file's old storage name -> its new one, model-root-relative and POSIX. Each file
    keeps its directory, so a placement value never moves.
    """
    registry = production_registry()
    renamed: dict[str, str] = {}
    for layer in registry.layer_names:
        by_directory: dict[str, list[Path]] = {}
        for entry in scan_layer(model_root, layer).files:
            by_directory.setdefault(entry.path.parent.as_posix(), []).append(entry.path)
        for paths in by_directory.values():
            count = len(paths)
            for index, path in enumerate(paths):
                target = path.with_name(f"{count - index:03d}_renamed{path.suffix}")
                path.rename(target)
                renamed[path.relative_to(model_root).as_posix()] = target.relative_to(model_root).as_posix()
    return renamed


def _needs_without_their_paths(text: bytes) -> tuple[dict[str, Any], dict[str, str]]:
    """A ``needs.json``'s document with every need's ``swcraft_path`` taken out, and those paths by need id."""
    document = json.loads(text.decode("utf-8"))
    needs = document["versions"][document["current_version"]]["needs"]
    return document, {need_id: need.pop("swcraft_path") for need_id, need in needs.items()}


_MODEL_BLOCK = re.compile(r'<script type="application/json" id="(swcraft-model|swcraft-elements)">(.*?)</script>')


def _pages_without_their_model_blocks(generated: dict[str, bytes]) -> dict[str, Any]:
    """Every diagram page's model block (and the index's elements block) taken out of *generated*, by page.

    The pages name the storage name of the file each drawn element opens, by design, as
    ``needs.json``'s ``swcraft_path`` does; the rest of the page is compared byte for byte.
    """
    blocks: dict[str, Any] = {}
    for name in [name for name in generated if name.startswith("views/") and name.endswith(".html")]:
        text = generated[name].decode("utf-8")
        found = _MODEL_BLOCK.search(text)
        assert found is not None, name
        blocks[name] = json.loads(found.group(2))
        generated[name] = (text[: found.start(2)] + text[found.end(2) :]).encode("utf-8")
    return blocks


def _renamed_blocks(blocks: dict[str, Any], renames: dict[str, str]) -> dict[str, Any]:
    """*blocks* with every storage name they hold replaced by the file's new name."""
    renamed: dict[str, Any] = {}
    for name, block in blocks.items():
        if name == "views/index.html":
            renamed[name] = {renames[file]: row for file, row in block.items()}
        else:
            renamed[name] = {
                ordinal: {**entry, "file": renames[entry["file"]]} if "file" in entry else entry
                for ordinal, entry in block.items()
            }
    return renamed


def test_renaming_every_file_changes_no_generated_byte(tmp_path: Path) -> None:
    """The file name takes no part in export order.

    Every element file of a copy of ``examples/basic`` is renamed into the reverse of its identity
    order. ARXML at both versions and every diagram file are compared byte for byte, and so is
    ``needs.json`` but for each need's ``swcraft_path``, which names the file declaring the need
    by design: it must be exactly the new name of the file it named before. The diagram pages'
    model blocks and the index's elements block name storage names too and are held the same way.
    """
    original = tmp_path / "original"
    renamed = tmp_path / "renamed"
    shutil.copytree(EXAMPLE_MODEL, original)
    shutil.copytree(EXAMPLE_MODEL, renamed)

    renames = _rename_into_reverse_order(renamed)
    assert len(renames) == len(list(_scan_layer_files(original)))
    before = _generate_all(original, tmp_path / "out-original", diagrams=True)
    after = _generate_all(renamed, tmp_path / "out-renamed", diagrams=True)
    assert any(name.startswith("views/") for name in before)
    document_before, paths_before = _needs_without_their_paths(before.pop("needs.json"))
    document_after, paths_after = _needs_without_their_paths(after.pop("needs.json"))
    blocks_before = _pages_without_their_model_blocks(before)
    blocks_after = _pages_without_their_model_blocks(after)
    assert before == after
    assert document_before == document_after
    assert paths_after == {need_id: renames[path] for need_id, path in paths_before.items()}
    assert paths_after != paths_before
    assert all((renamed / path).is_file() for path in paths_after.values()), paths_after
    assert blocks_after == _renamed_blocks(blocks_before, renames)
    assert blocks_after != blocks_before


# ---------------------------------------------------------------------------
# One spelling of a storage name: needs.json and the diagram pages share it
# ---------------------------------------------------------------------------


def _layer_directory(layer: str) -> str:
    return production_registry().storage(layer).directory


def _stored_names(root: Path, layer: str) -> dict[str, list[str]]:
    return ms.stored_storage_names(root, layer, registry=production_registry())


def test_stored_storage_names_takes_the_registry_by_keyword_and_requires_it() -> None:
    parameter = inspect.signature(ms.stored_storage_names).parameters["registry"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_a_suffixed_file_is_named_as_it_is_stored(tmp_path: Path) -> None:
    directory = _layer_directory("tasks")
    _write_element(tmp_path / directory / "Boot~1.yaml", {"name": "Boot", "period": 0.01})
    _write_element(tmp_path / directory / "boot.yaml", {"name": "boot", "period": 0.01})
    assert _stored_names(tmp_path, "tasks") == {
        "Boot": [f"{directory}/Boot~1.yaml"],
        "boot": [f"{directory}/boot.yaml"],
    }


def test_a_group_directory_is_part_of_the_name_and_the_name_is_posix(tmp_path: Path) -> None:
    directory = _layer_directory("swc")
    _write_element(tmp_path / directory / "chassis" / "Sensor.yaml", {"name": "Sensor", "type": "application"})
    names = _stored_names(tmp_path, "swc")
    assert names == {"Sensor": [f"{directory}/chassis/Sensor.yaml"]}
    assert "\\" not in names["Sensor"][0] and not names["Sensor"][0].startswith("/")


def test_an_identity_two_files_declare_has_both_names_in_listing_order(tmp_path: Path) -> None:
    directory = _layer_directory("tasks")
    _write_element(tmp_path / directory / "First.yaml", {"name": "Twice", "period": 0.01})
    _write_element(tmp_path / directory / "Second.yaml", {"name": "Twice", "period": 0.02})
    expected = [entry.path.relative_to(tmp_path).as_posix() for entry in scan_layer(tmp_path, "tasks").files]
    assert _stored_names(tmp_path, "tasks") == {"Twice": expected}
    assert sorted(expected) == [f"{directory}/First.yaml", f"{directory}/Second.yaml"]


def test_an_empty_or_absent_layer_names_nothing(tmp_path: Path) -> None:
    (tmp_path / _layer_directory("hardware")).mkdir(parents=True)
    assert _stored_names(tmp_path, "hardware") == {}
    assert _stored_names(tmp_path, "sequences") == {}


def test_the_names_are_the_kernel_listing_relative_to_the_root_it_was_given(tmp_path: Path) -> None:
    """The one spelling: ``stored_element_files``' entries, each made relative to the root and POSIX."""
    model_root = tmp_path / "model"
    shutil.copytree(EXAMPLE_MODEL, model_root)
    registry = production_registry()
    for layer in registry.layer_names:
        files = ms.stored_element_files(model_root, layer, registry=registry)
        assert ms.stored_storage_names(model_root, layer, registry=registry) == {
            identity: [entry.path.relative_to(model_root).as_posix() for entry in entries]
            for identity, entries in files.items()
        }, layer


def test_the_module_publishes_the_new_names() -> None:
    assert {
        "FreePath",
        "PlannedStorage",
        "StorageNameRefusal",
        "declared_identity",
        "free_storage_path",
        "stored_element_files",
        "stored_storage_names",
    } <= set(ms.__all__)
