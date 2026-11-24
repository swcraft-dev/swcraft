"""The implementation record, the implementation array and the pointer through both write paths.

``writer/yaml_documents.py``'s hand-written ``data_type_to_dict`` is what the ARXML
importer and component exchange write a data type through; ``writer/elements.py``'s
generic ``element_to_dict`` is what the editing protocol writes through. Over a copy of
``examples/basic`` this module holds that the two agree, that the legacy
``mutations.create_data_type`` refuses the three kinds, and that the protocol creates,
updates, removes and reads them, every refusal code they reach leaving the model
byte-identical.

What this module does **not** reach:

* the rules over every shape: each is witnessed once, as the refusal the protocol maps
  it to; the rule matrix is ``test_impl_composites_validator.py``'s;
* the ARXML these models generate, component exchange's closure over them and the
  importer's reading of them: only that the serializer those paths write through keeps
  every field;
* the authorization boundary beyond the ordinary review: the safety classifier
  dispatches on ``hardware``, ``swc`` and ``requirements`` only, so a data type edit is
  never a safety act — which the classifier's own suite holds, not this one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_data_types
from swcraft_tools.schema import ImplementationArrayDataType, ImplementationRecordDataType, PointerDataType
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import mutations
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import data_type_to_dict

#: The three kinds as the tests author them: a record over two value types, a
#: fixed-size array of the record, and a pointer to the record.
RECORD: dict[str, Any] = {
    "name": "WheelSpeeds_T",
    "kind": "implementation_record",
    "elements": [{"name": "front", "type": "uint16"}, {"name": "rear", "type": "uint16"}],
}
ARRAY: dict[str, Any] = {
    "name": "WheelSpeedHistory_T",
    "kind": "implementation_array",
    "element_type": "WheelSpeeds_T",
    "length": 8,
    "element_name": "Sample",
}
POINTER: dict[str, Any] = {"name": "WheelSpeeds_Ptr", "kind": "pointer", "target": "WheelSpeeds_T"}

KINDS: dict[str, tuple[type, dict[str, Any]]] = {
    "implementation_record": (ImplementationRecordDataType, RECORD),
    "implementation_array": (ImplementationArrayDataType, ARRAY),
    "pointer": (PointerDataType, POINTER),
}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _file(project: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load(
        (project / "model" / "data_types" / "implementation" / f"{name}.yaml").read_text(encoding="utf-8")
    )


def _findings(project: Path) -> list[Any]:
    return list(check_cross_layer_consistency(str(project / "model")).records)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """``examples/basic`` as a project: its manifest beside a copy of its model."""
    root = tmp_path / "impl-composites-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    shutil.copytree(fixtures.BASE_MODEL_ROOT, root / "model")
    return root


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=False
    )


def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "implementation composites", "operations": operations},
        project=str(project),
        review=_approve,
    )


def _address(element: str, *path: dict[str, Any], layer: str = "implementation_types") -> dict[str, Any]:
    address: dict[str, Any] = {"layer": layer, "element": element}
    if path:
        address["path"] = list(path)
    return address


#: The application kinds, which lie in the ``application_types`` layer.
_APPLICATION_KINDS = frozenset({"application_type", "enum", "record", "array"})


def _create(document: dict[str, Any], layer: str | None = None) -> dict[str, Any]:
    if layer is None:
        layer = "application_types" if document.get("kind") in _APPLICATION_KINDS else "implementation_types"
    return {"operation": "create", "address": _address(document["name"], layer=layer), "value": document}


def _update(element: str, *path: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "update", "address": _address(element, *path), "value": value}


def _remove(element: str, *path: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": _address(element, *path)}


def _element(member: str, *tail: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return ({"field": "elements"}, {"member": member}, *tail)


def _committed_all_three(project: Path) -> None:
    response = _change_set(project, [_create(RECORD), _create(ARRAY), _create(POINTER)])
    assert response["status"] == "committed", response


def _refused(project: Path, operations: list[dict[str, Any]], error: str) -> dict[str, Any]:
    """Send *operations*, assert the refusal is *error* and the model did not move."""
    before = _tree(project / "model")
    response = _change_set(project, operations)
    assert response.get("error") == error, response
    assert _tree(project / "model") == before
    return response


class TestTheTwoSerializersAgree:
    @pytest.mark.parametrize("kind", sorted(KINDS))
    @pytest.mark.parametrize("description", ["", "kept"])
    def test_the_hand_written_arm_is_the_generic_document(self, kind: str, description: str) -> None:
        model_class, document = KINDS[kind]
        element = model_class.model_validate({**document, "description": description})
        hand_written, generic = data_type_to_dict(element), element_to_dict(element)
        assert hand_written == generic
        assert list(hand_written) == list(generic)
        assert ("description" in hand_written) is bool(description)

    @pytest.mark.parametrize("kind", sorted(KINDS))
    def test_a_protocol_create_writes_every_field(self, project: Path, kind: str) -> None:
        _committed_all_three(project)
        model_class, document = KINDS[kind]
        response = _change_set(project, [_update(document["name"], {"field": "description"}, value="kept")])
        assert response["status"] == "committed", response

        written = _file(project, document["name"])
        assert written == {**document, "description": "kept"}
        (loaded,) = (dt for dt in read_data_types(str(project / "model")) if dt.name == document["name"])
        assert isinstance(loaded, model_class)
        assert data_type_to_dict(loaded) == written
        assert list(data_type_to_dict(loaded)) == list(written)


class TestTheLegacyEntryPointRefusesTheKinds:
    """``create_data_type`` takes no ``external_type`` either; the protocol is the authoring surface."""

    @pytest.mark.parametrize("kind", sorted(KINDS))
    def test_it_refuses_the_kind_and_writes_nothing(self, project: Path, kind: str) -> None:
        before = _tree(project / "model")
        with pytest.raises(ValueError, match=f"Unknown data type kind '{kind}'"):
            mutations.create_data_type(str(project / "model"), KINDS[kind][1]["name"], kind)
        assert _tree(project / "model") == before


class TestTheProtocolCreates:
    def test_an_implementation_record_alone(self, project: Path) -> None:
        response = _change_set(project, [_create(RECORD)])
        assert response["status"] == "committed", response
        assert _file(project, RECORD["name"]) == RECORD
        assert _findings(project) == []

    def test_an_implementation_array_of_a_value_type(self, project: Path) -> None:
        array = {**ARRAY, "element_type": "uint8"}
        response = _change_set(project, [_create(array)])
        assert response["status"] == "committed", response
        assert _file(project, ARRAY["name"]) == array
        assert _findings(project) == []

    def test_a_pointer_to_a_value_type(self, project: Path) -> None:
        pointer = {**POINTER, "target": "uint8"}
        response = _change_set(project, [_create(pointer)])
        assert response["status"] == "committed", response
        assert _file(project, POINTER["name"]) == pointer
        assert _findings(project) == []

    def test_all_three_in_one_change_set_whatever_the_order(self, project: Path) -> None:
        response = _change_set(project, [_create(POINTER), _create(ARRAY), _create(RECORD)])
        assert response["status"] == "committed", response
        assert [_file(project, document["name"]) for document in (RECORD, ARRAY, POINTER)] == [RECORD, ARRAY, POINTER]
        assert _findings(project) == []

    def test_with_an_application_record_and_the_composite_map(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                _create({"name": "WheelSpeed_Kph", "kind": "application_type", "unit": "kph"}),
                _create(
                    {
                        "name": "WheelSpeeds",
                        "kind": "record",
                        "fields": [
                            {"name": "left", "type": "WheelSpeed_Kph"},
                            {"name": "right", "type": "WheelSpeed_Kph"},
                        ],
                    }
                ),
                _create(RECORD),
                _create(
                    {
                        "name": "WheelSpeedMapping",
                        "maps": [
                            {"application": "WheelSpeed_Kph", "implementation": "uint16"},
                            {"application": "WheelSpeeds", "implementation": RECORD["name"]},
                        ],
                    },
                    layer="mapping_sets",
                ),
            ],
        )
        # The member names differ (`left`/`right` against `front`/`rear`): the positional
        # check judges no member name, so the pair is accepted.
        assert response["status"] == "committed", response
        assert _findings(project) == []

    def test_a_reference_type_aliasing_each_kind(self, project: Path) -> None:
        aliases = [
            _create({"name": f"{document['name']}_Alias", "kind": "reference_type", "base": document["name"]})
            for document in (RECORD, ARRAY, POINTER)
        ]
        response = _change_set(project, [_create(RECORD), _create(ARRAY), _create(POINTER), *aliases])
        assert response["status"] == "committed", response
        assert _findings(project) == []


class TestTheProtocolUpdates:
    def test_a_member_created_inside_elements(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(
            project,
            [
                {
                    "operation": "create",
                    "address": _address(RECORD["name"], *_element("spare")),
                    "value": {"name": "spare", "type": "uint8"},
                }
            ],
        )
        assert response["status"] == "committed", response
        assert _file(project, RECORD["name"])["elements"] == [*RECORD["elements"], {"name": "spare", "type": "uint8"}]

    def test_a_member_type(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(project, [_update(RECORD["name"], *_element("rear", {"field": "type"}), value="uint32")])
        assert response["status"] == "committed", response
        assert _file(project, RECORD["name"])["elements"] == [
            {"name": "front", "type": "uint16"},
            {"name": "rear", "type": "uint32"},
        ]

    def test_a_reorder_of_elements_is_a_collection_update(self, project: Path) -> None:
        _committed_all_three(project)
        reordered = list(reversed(RECORD["elements"]))
        response = _change_set(project, [_update(RECORD["name"], {"field": "elements"}, value=reordered)])
        assert response["status"] == "committed", response
        assert _file(project, RECORD["name"])["elements"] == reordered

    def test_the_array_length_and_element_name(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(
            project,
            [
                _update(ARRAY["name"], {"field": "length"}, value=16),
                _update(ARRAY["name"], {"field": "element_name"}, value="Entry"),
            ],
        )
        assert response["status"] == "committed", response
        assert _file(project, ARRAY["name"]) == {**ARRAY, "length": 16, "element_name": "Entry"}

    def test_the_pointer_target(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(project, [_update(POINTER["name"], {"field": "target"}, value=ARRAY["name"])])
        assert response["status"] == "committed", response
        assert _file(project, POINTER["name"])["target"] == ARRAY["name"]
        assert _findings(project) == []

    def test_a_member_rename_is_immutable(self, project: Path) -> None:
        _committed_all_three(project)
        _refused(
            project, [_update(RECORD["name"], *_element("front", {"field": "name"}), value="left")], "immutable-field"
        )

    def test_a_kind_change_is_immutable(self, project: Path) -> None:
        _committed_all_three(project)
        _refused(project, [_update(POINTER["name"], {"field": "kind"}, value="reference_type")], "immutable-field")


class TestTheProtocolRemoves:
    def test_a_member(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(project, [_remove(RECORD["name"], *_element("rear"))])
        assert response["status"] == "committed", response
        assert _file(project, RECORD["name"])["elements"] == [{"name": "front", "type": "uint16"}]

    def test_an_element_nothing_names(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(project, [_remove(POINTER["name"])])
        assert response["status"] == "committed", response
        assert not (project / "model" / "data_types" / "implementation" / f"{POINTER['name']}.yaml").exists()

    def test_a_referenced_element_with_its_referrers_re_pointed(self, project: Path) -> None:
        _committed_all_three(project)
        response = _change_set(
            project,
            [
                _update(ARRAY["name"], {"field": "element_type"}, value="uint8"),
                _update(POINTER["name"], {"field": "target"}, value="uint8"),
                _remove(RECORD["name"]),
            ],
        )
        assert response["status"] == "committed", response
        assert _findings(project) == []

    def test_a_referenced_element_alone_is_a_broken_reference_at_the_referrers(self, project: Path) -> None:
        _committed_all_three(project)
        response = _refused(project, [_remove(RECORD["name"])], "broken-reference")
        referrers = [response["addresses"], *(entry["addresses"] for entry in response.get("additional", []))]
        flat = [address for addresses in referrers for address in addresses]
        assert _address(ARRAY["name"], {"field": "element_type"}) in flat
        assert _address(POINTER["name"], {"field": "target"}) in flat


# Refusals


class TestSchemaViolations:
    def test_an_empty_elements_list(self, project: Path) -> None:
        _refused(project, [_create({**RECORD, "elements": []})], "schema-violation")

    def test_a_zero_length(self, project: Path) -> None:
        _refused(project, [_create({**ARRAY, "element_type": "uint8", "length": 0})], "schema-violation")

    def test_a_pointer_naming_itself(self, project: Path) -> None:
        _refused(project, [_create({**POINTER, "target": POINTER["name"]})], "schema-violation")


class TestUnknownFields:
    def test_a_semantics_key_on_the_array(self, project: Path) -> None:
        response = _refused(
            project, [_create({**ARRAY, "element_type": "uint8", "semantics": "FIXED-SIZE"})], "unknown-field"
        )
        assert response["addresses"] == [_address(ARRAY["name"], {"field": "semantics"})]

    def test_a_third_key_on_an_element(self, project: Path) -> None:
        record = {**RECORD, "elements": [{"name": "front", "type": "uint16", "offset": 0}]}
        _refused(project, [_create(record)], "unknown-field")


class TestIdentityConflicts:
    def test_a_member_created_twice(self, project: Path) -> None:
        _committed_all_three(project)
        _refused(
            project,
            [
                {
                    "operation": "create",
                    "address": _address(RECORD["name"], *_element("front")),
                    "value": {"name": "front", "type": "uint8"},
                }
            ],
            "identity-conflict",
        )

    def test_an_element_created_over_a_held_name(self, project: Path) -> None:
        _refused(project, [_create({**POINTER, "name": "uint16", "target": "uint8"})], "identity-conflict")


class TestInvalidIdentifiers:
    def test_an_element_name(self, project: Path) -> None:
        record = {**RECORD, "elements": [{"name": "1front", "type": "uint16"}]}
        response = _refused(project, [_create(record)], "invalid-identifier")
        assert response["addresses"] == [_address(RECORD["name"], *_element("1front", {"field": "name"}))]
        assert "element name '1front'" in response["message"]

    def test_the_array_element_name(self, project: Path) -> None:
        response = _refused(
            project, [_create({**ARRAY, "element_type": "uint8", "element_name": "my sample"})], "invalid-identifier"
        )
        assert response["addresses"] == [_address(ARRAY["name"], {"field": "element_name"})]
        assert f"the implementation_array '{ARRAY['name']}' element name 'my sample'" in response["message"]


class TestBrokenReferences:
    def test_an_undeclared_element_type(self, project: Path) -> None:
        record = {**RECORD, "elements": [{"name": "front", "type": "uint12"}]}
        response = _refused(project, [_create(record)], "broken-reference")
        assert response["addresses"] == [_address(RECORD["name"], *_element("front", {"field": "type"}))]

    def test_an_undeclared_array_element_type(self, project: Path) -> None:
        response = _refused(project, [_create(ARRAY)], "broken-reference")
        assert response["addresses"] == [_address(ARRAY["name"], {"field": "element_type"})]

    def test_an_undeclared_pointer_target(self, project: Path) -> None:
        response = _refused(project, [_create(POINTER)], "broken-reference")
        assert response["addresses"] == [_address(POINTER["name"], {"field": "target"})]


class TestConsistencyViolations:
    def test_an_element_naming_an_application_type_is_an_undeclared_name(self, project: Path) -> None:
        """An element's ``type`` resolves in ``implementation_types`` alone (contract ``8.0.0``).

        An application type of that name is an undeclared name, refused as a broken reference.
        """
        record = {**RECORD, "elements": [{"name": "front", "type": "WheelSpeed_Kph"}]}
        response = _refused(
            project,
            [_create({"name": "WheelSpeed_Kph", "kind": "application_type"}), _create(record)],
            "broken-reference",
        )
        assert response["addresses"] == [_address(RECORD["name"], *_element("front", {"field": "type"}))]
        assert "implementation-element-not-implementation-type" not in response["message"]

    def test_a_duplicate_element_name(self, project: Path) -> None:
        record = {**RECORD, "elements": [{"name": "front", "type": "uint16"}, {"name": "front", "type": "uint8"}]}
        response = _refused(project, [_create(record)], "consistency-violation")
        assert "duplicate element name" in response["message"]

    def test_a_record_containing_itself_through_an_array(self, project: Path) -> None:
        record = {**RECORD, "elements": [{"name": "history", "type": ARRAY["name"]}]}
        response = _refused(project, [_create(record), _create(ARRAY)], "consistency-violation")
        assert "Cycle detected" in response["message"]

    def test_a_map_onto_a_pointer(self, project: Path) -> None:
        response = _refused(
            project,
            [
                _create({"name": "WheelSpeed_Kph", "kind": "application_type"}),
                _create({**POINTER, "target": "uint16"}),
                _create(
                    {
                        "name": "WheelSpeedMapping",
                        "maps": [{"application": "WheelSpeed_Kph", "implementation": POINTER["name"]}],
                    },
                    layer="mapping_sets",
                ),
            ],
            "consistency-violation",
        )
        assert "[constr_1066]" in response["message"]

    def test_a_composite_map_of_another_member_count(self, project: Path) -> None:
        response = _refused(
            project,
            [
                _create({"name": "WheelSpeed_Kph", "kind": "application_type"}),
                _create(
                    {"name": "WheelSpeeds", "kind": "record", "fields": [{"name": "front", "type": "WheelSpeed_Kph"}]}
                ),
                _create(RECORD),
                _create(
                    {
                        "name": "WheelSpeedMapping",
                        "maps": [
                            {"application": "WheelSpeed_Kph", "implementation": "uint16"},
                            {"application": "WheelSpeeds", "implementation": RECORD["name"]},
                        ],
                    },
                    layer="mapping_sets",
                ),
            ],
            "consistency-violation",
        )
        assert "positional check of a composite map" in response["message"]

    def test_a_record_field_naming_an_implementation_record_is_an_undeclared_name(self, project: Path) -> None:
        """A record field's ``type`` resolves in ``application_types`` alone (contract ``8.0.0``).

        An implementation record of that name is an undeclared name, refused as a broken reference.
        """
        response = _refused(
            project,
            [
                _create(RECORD),
                _create({"name": "WheelSpeeds", "kind": "record", "fields": [{"name": "all", "type": RECORD["name"]}]}),
            ],
            "broken-reference",
        )
        assert response["addresses"][0]["layer"] == "application_types"
        assert response["addresses"][0]["element"] == "WheelSpeeds"
        assert "composite-member-implementation-type" not in response["message"]

    def test_a_pointer_crossing_a_connector_between_application_components(self, project: Path) -> None:
        response = _refused(
            project,
            [
                _create({**POINTER, "target": "float32"}),
                {
                    "operation": "update",
                    "address": _address(
                        "IExample",
                        {"field": "data_elements"},
                        {"member": "value"},
                        {"field": "implementation_type"},
                        layer="interfaces",
                    ),
                    "value": POINTER["name"],
                },
            ],
            "consistency-violation",
        )
        assert "[constr_1295]" in response["message"]


class TestTheProtocolReads:
    def test_an_element(self, project: Path) -> None:
        _committed_all_three(project)
        response = handle_request(
            {"operation": "read", "address": _address(ARRAY["name"]), "form": "full"}, project=str(project)
        )
        # `package` since contract 7.10.0: the full form publishes it at its `null` default.
        assert response["value"] == {**ARRAY, "description": "", "package": None}

    def test_the_elements_collection(self, project: Path) -> None:
        _committed_all_three(project)
        response = handle_request(
            {"operation": "read", "address": _address(RECORD["name"], {"field": "elements"}), "form": "full"},
            project=str(project),
        )
        # The full form publishes a field at its default: since contract 7.10.0
        # every element carries its `description`, `""` here.
        assert response["value"] == [{**element, "description": ""} for element in RECORD["elements"]]
