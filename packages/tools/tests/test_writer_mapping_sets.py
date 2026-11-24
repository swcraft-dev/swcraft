"""``create_mapping_set`` — the narrow writer for the mapping-set layer.

It is shaped on :func:`create_composition`: the layer-local
half a single call can see is refused here — a blank name, a path-escaping one, a
name no AUTOSAR ``Identifier`` admits and a name the layer already carries — and
everything cross-file (``mapping-set.unknown-application``,
``mapping-set.unknown-implementation``, ``data-type.application-type-unmapped``)
stays ``check-model``'s, because this writer sees one file.

What it does **not** reach: no generated form lengthens a mapping set's
``SHORT-NAME``, so there is no reserved-name guard here at all —
``check_reserved_data_type_name``'s twin would have nothing to reserve. And a
pair naming a type of the wrong kind is written: which kinds a side may name is a question about other files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import model_element_path
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import find_element_file, read_mapping_sets
from swcraft_tools.schema import DataTypeMap, MappingSet
from swcraft_tools.writer import create_mapping_set

PAIR = {"application": "VehicleSpeed_Kph", "implementation": "Speed_T"}


def _audit(tmp_path: Path) -> AuditLog:
    return AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_create_mapping_set_creates_yaml(tmp_path):
    path = create_mapping_set(str(tmp_path), name="PhysicalMapping", maps=[PAIR])

    expected = model_element_path(tmp_path, "PhysicalMapping", layer="mapping_sets")
    assert path == str(expected)
    assert expected.exists()

    sets = read_mapping_sets(str(tmp_path))
    assert sets == [
        MappingSet(
            name="PhysicalMapping",
            maps=[DataTypeMap(application="VehicleSpeed_Kph", implementation="Speed_T")],
        )
    ]


def test_create_mapping_set_writes_every_declared_field_in_declaration_order(tmp_path):
    """The serializer is a field list written by hand, so the shape is asserted.

    ``maps`` declares no ``omitAtDefault``, so an empty list is written rather
    than omitted; ``description`` does, so it is omitted at its default.
    """
    path = create_mapping_set(
        str(tmp_path),
        name="PhysicalMapping",
        maps=[PAIR],
        description="The physical view of the speed signals.",
    )

    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert list(document) == ["name", "maps", "description"]
    assert document["maps"] == [{"application": "VehicleSpeed_Kph", "implementation": "Speed_T"}]
    assert document["description"] == "The physical view of the speed signals."


def test_an_empty_set_is_written_with_an_empty_maps_list(tmp_path):
    path = create_mapping_set(str(tmp_path), name="Empty")

    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert document == {"name": "Empty", "maps": []}
    assert read_mapping_sets(str(tmp_path)) == [MappingSet(name="Empty")]


def test_create_mapping_set_with_auditor(tmp_path):
    auditor = _audit(tmp_path)
    path = create_mapping_set(str(tmp_path), name="PhysicalMapping", maps=[PAIR], auditor=auditor)

    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_mapping_set"
    assert entries[0]["args"] == {"name": "PhysicalMapping", "maps_count": 1}
    assert entries[0]["result"] == Path(path).relative_to(tmp_path).as_posix()


def test_find_element_file_after_creation(tmp_path):
    create_mapping_set(str(tmp_path), name="PhysicalMapping")
    assert find_element_file(str(tmp_path), "mapping_sets", "PhysicalMapping") is not None


# ---------------------------------------------------------------------------
# Refusals the call can decide on its own
# ---------------------------------------------------------------------------


def test_create_mapping_set_empty_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name must not be empty"):
        create_mapping_set(str(tmp_path), name="")


def test_create_mapping_set_whitespace_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name must not be empty"):
        create_mapping_set(str(tmp_path), name="   ")


@pytest.mark.parametrize("name", ["A/B", "A\\B", "A..B"])
def test_create_mapping_set_path_traversal_raises(tmp_path, name):
    with pytest.raises(ValueError, match="must not contain path separators"):
        create_mapping_set(str(tmp_path), name=name)


def test_create_mapping_set_non_identifier_name_raises(tmp_path):
    """A set's name is emitted as a ``DATA-TYPE-MAPPING-SET`` ``SHORT-NAME``."""
    with pytest.raises(ValueError, match="Mapping set name '9Sets' is not a legal AUTOSAR Identifier"):
        create_mapping_set(str(tmp_path), name="9Sets")


def test_create_mapping_set_duplicate_raises(tmp_path):
    create_mapping_set(str(tmp_path), name="PhysicalMapping")
    with pytest.raises(ValueError, match="Mapping set 'PhysicalMapping' already exists"):
        create_mapping_set(str(tmp_path), name="PhysicalMapping")


def test_a_pair_with_a_blank_side_is_refused_by_the_schema(tmp_path):
    with pytest.raises(ValueError, match="must not be blank"):
        create_mapping_set(str(tmp_path), name="PhysicalMapping", maps=[{"application": " ", "implementation": "X"}])


def test_one_application_type_mapped_twice_in_one_set_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mapped twice"):
        create_mapping_set(
            str(tmp_path),
            name="PhysicalMapping",
            maps=[PAIR, {"application": "VehicleSpeed_Kph", "implementation": "Other_T"}],
        )


def test_a_refused_set_leaves_no_file(tmp_path):
    with pytest.raises(ValueError):
        create_mapping_set(str(tmp_path), name="9Sets")
    assert not (tmp_path / "data_types" / "mapping_sets").exists()


# ---------------------------------------------------------------------------
# The store: the file name is derived from the identity
# ---------------------------------------------------------------------------


def test_a_set_whose_file_name_another_set_holds_takes_a_collision_suffix(tmp_path):
    """``free_storage_path`` at the write, compared by ``str.casefold()``.

    The identity is what refuses (the content check); the file name only takes
    the nearest free ``~N``, and the file that held the plain name is untouched.
    """
    directory = tmp_path / "data_types" / "mapping_sets"
    directory.mkdir(parents=True)
    occupant = "name: Occupant\nmaps: []\n"
    (directory / "PhysicalMapping.yaml").write_text(occupant, encoding="utf-8", newline="\n")

    result = create_mapping_set(str(tmp_path), name="PhysicalMapping")

    assert Path(result).relative_to(tmp_path).as_posix() == "data_types/mapping_sets/PhysicalMapping~1.yaml"
    assert yaml.safe_load(Path(result).read_text(encoding="utf-8"))["name"] == "PhysicalMapping"
    assert (directory / "PhysicalMapping.yaml").read_text(encoding="utf-8") == occupant


def test_a_case_only_collision_takes_the_suffix_on_every_filesystem(tmp_path):
    directory = tmp_path / "data_types" / "mapping_sets"
    directory.mkdir(parents=True)
    (directory / "physicalmapping.yaml").write_text("name: Occupant\nmaps: []\n", encoding="utf-8", newline="\n")

    result = create_mapping_set(str(tmp_path), name="PhysicalMapping")

    assert Path(result).relative_to(tmp_path).as_posix() == "data_types/mapping_sets/PhysicalMapping~1.yaml"


def test_the_identity_is_refused_under_any_file_name(tmp_path):
    directory = tmp_path / "data_types" / "mapping_sets"
    directory.mkdir(parents=True)
    (directory / "Elsewhere.yaml").write_text("name: PhysicalMapping\nmaps: []\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="Mapping set 'PhysicalMapping' already exists"):
        create_mapping_set(str(tmp_path), name="PhysicalMapping")
