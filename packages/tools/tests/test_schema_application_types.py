"""The application type and the mapping set, at the loader.

Three questions, and only three, because everything else about these kinds is
another suite's:

* **what loads** — the optional fields absent, a conversion that names one of
  its two fields, a range whose bounds are coherent, a set with no pairs;
* **what does not** — a blank name, an incoherent range, a non-finite bound, an
  application type mapped twice in one set, a mapping set named twice by one
  component, and an undeclared key under any of the three closed kinds;
* **what the contract says about them** — the declaration each class is bound
  to, field by field, so that the loader and the declaration cannot drift apart
  in the direction no generated artifact would notice.

What this module does **not** reach: nothing here resolves a name across files. Whether `maps[].application`
names a declared application type, whether a component's
`data_type_mappings` entry names a declared set, and whether every application
type a port speaks in is paired at all are `check-model`'s questions and are
tested with the rules that answer them. Nor does anything here emit ARXML: the
three generated forms an application type's name lengthens are
``identifier.py``'s and the writer's.
"""

from __future__ import annotations

from typing import Any

import pytest
from model_contract_fixtures import bound_classes, load_binding, load_contract
from pydantic import ValidationError
from swcraft_tools import reader
from swcraft_tools.schema import (
    ApplicationDataType,
    Conversion,
    DataTypeMap,
    MappingSet,
    SWComponent,
    ValueRange,
)

# ---------------------------------------------------------------------------
# The application type at the loader
# ---------------------------------------------------------------------------


class TestTheApplicationTypeLoads:
    def test_every_optional_field_may_be_absent(self) -> None:
        """The physics is optional field by field.

        A source export that carried none of it imports as a bare application primitive."""
        element = ApplicationDataType(name="Kph_T", kind="application_type")

        assert (element.unit, element.conversion, element.range, element.description) == ("", None, None, "")

    def test_a_conversion_naming_one_field_leaves_the_other_at_its_default(self) -> None:
        element = ApplicationDataType.model_validate(
            {"name": "Kph_T", "kind": "application_type", "conversion": {"factor": 0.1}}
        )

        assert element.conversion == Conversion(factor=0.1, offset=0.0)

    def test_an_empty_conversion_is_the_identity_conversion(self) -> None:
        element = ApplicationDataType.model_validate({"name": "Kph_T", "kind": "application_type", "conversion": {}})

        assert element.conversion == Conversion(factor=1.0, offset=0.0)

    def test_a_range_may_declare_one_bound(self) -> None:
        """``value_type``'s rule exactly: either bound may be absent.

        A single bound is a half-open interval rather than an error."""
        element = ApplicationDataType.model_validate({"name": "Kph_T", "kind": "application_type", "range": {"min": 0}})

        assert element.range == ValueRange(min=0.0, max=None)

    def test_an_absent_conversion_and_range_are_not_dumped_as_objects(self) -> None:
        dumped = ApplicationDataType(name="Kph_T", kind="application_type").model_dump()

        assert dumped["conversion"] is None and dumped["range"] is None


class TestTheApplicationTypeRefuses:
    def test_a_blank_name(self) -> None:
        with pytest.raises(ValidationError, match="name must not be blank"):
            ApplicationDataType(name="   ", kind="application_type")

    def test_a_name_carrying_a_path_separator(self) -> None:
        with pytest.raises(ValidationError, match="must not contain path separators"):
            ApplicationDataType(name="a/b", kind="application_type")

    def test_a_range_whose_min_exceeds_its_max(self) -> None:
        with pytest.raises(ValidationError, match="range 'min' must be <= 'max'"):
            ApplicationDataType.model_validate(
                {"name": "Kph_T", "kind": "application_type", "range": {"min": 400, "max": 0}}
            )

    @pytest.mark.parametrize("path", [("conversion", "factor"), ("conversion", "offset"), ("range", "min")])
    def test_a_non_finite_number_anywhere_under_it(self, path: tuple[str, str]) -> None:
        """``ModelElement``'s ``allow_inf_nan=False`` reaches the nested records.

        The guard is on the base every element inherits, so neither member kind needs one of its own."""
        owner, field = path
        with pytest.raises(ValidationError):
            ApplicationDataType.model_validate(
                {"name": "Kph_T", "kind": "application_type", owner: {field: float("nan")}}
            )

    def test_an_undeclared_key(self) -> None:
        """A typo is a load failure and not a silently dropped key."""
        with pytest.raises(ValidationError):
            ApplicationDataType.model_validate({"name": "Kph_T", "kind": "application_type", "base": "uint16"})

    @pytest.mark.parametrize(
        ("owner", "payload"),
        [("conversion", {"gain": 2}), ("range", {"lower": 2})],
    )
    def test_an_undeclared_key_inside_a_nested_record(self, owner: str, payload: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ApplicationDataType.model_validate({"name": "Kph_T", "kind": "application_type", owner: payload})


# ---------------------------------------------------------------------------
# The mapping set at the loader
# ---------------------------------------------------------------------------


class TestTheMappingSetLoads:
    def test_a_set_with_no_pairs(self) -> None:
        """A container with nothing in it is not a broken model.

        What matters — that every application type a port speaks in is paired — is reported by `check-model` over the
        whole project and not by this file."""
        assert MappingSet(name="Physical").maps == []

    def test_the_pairs_keep_the_order_they_are_declared_in(self) -> None:
        element = MappingSet.model_validate(
            {
                "name": "Physical",
                "maps": [
                    {"application": "Kph_T", "implementation": "Speed_T"},
                    {"application": "Deg_T", "implementation": "Angle_T"},
                ],
            }
        )

        assert [entry.application for entry in element.maps] == ["Kph_T", "Deg_T"]

    def test_one_implementation_type_may_store_two_application_types(self) -> None:
        """The uniqueness is on the application side alone.

        Two physical views over one stored representation is the ordinary case."""
        element = MappingSet.model_validate(
            {
                "name": "Physical",
                "maps": [
                    {"application": "Kph_T", "implementation": "uint16_T"},
                    {"application": "Deg_T", "implementation": "uint16_T"},
                ],
            }
        )

        assert len(element.maps) == 2


class TestTheMappingSetRefuses:
    def test_a_blank_name(self) -> None:
        with pytest.raises(ValidationError, match="name must not be blank"):
            MappingSet(name=" ")

    def test_one_application_type_mapped_twice(self) -> None:
        with pytest.raises(ValidationError, match="application type mapped twice in one mapping set: Kph_T"):
            MappingSet.model_validate(
                {
                    "name": "Physical",
                    "maps": [
                        {"application": "Kph_T", "implementation": "Speed_T"},
                        {"application": "Kph_T", "implementation": "Other_T"},
                    ],
                }
            )

    @pytest.mark.parametrize("field", ["application", "implementation"])
    def test_a_blank_side_of_a_pair(self, field: str) -> None:
        payload = {"application": "Kph_T", "implementation": "Speed_T"} | {field: "  "}
        with pytest.raises(ValidationError, match="data type map fields must not be blank"):
            DataTypeMap.model_validate(payload)


# ---------------------------------------------------------------------------
# The component's reference to its sets
# ---------------------------------------------------------------------------


class TestDataTypeMappings:
    def test_it_defaults_to_empty_and_is_omitted_on_disk_at_that_default(self) -> None:
        """`omitAtDefault` keeps a component that names no set exactly the component it was.

        The generic writer leaves the key out entirely."""
        from swcraft_tools.writer.elements import _is_omitted_at_default

        assert SWComponent(name="SpeedSwc").data_type_mappings == []
        declared = next(
            field
            for layer in load_contract()["layers"]
            for kind in layer["kinds"]
            for field in kind["fields"]
            if field["id"] == "field:swc.swc.data_type_mappings"
        )
        assert declared["omitAtDefault"] is True
        info = SWComponent.model_fields["data_type_mappings"]
        assert _is_omitted_at_default(SWComponent, "data_type_mappings", info)

    def test_the_order_declared_is_the_order_kept(self) -> None:
        element = SWComponent(name="SpeedSwc", data_type_mappings=["Physical", "Diagnostic"])

        assert element.data_type_mappings == ["Physical", "Diagnostic"]

    def test_one_set_named_twice_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="mapping set named twice in data_type_mappings: Physical"):
            SWComponent(name="SpeedSwc", data_type_mappings=["Physical", "Physical"])

    def test_a_blank_entry_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="data_type_mappings entries must not be blank"):
            SWComponent(name="SpeedSwc", data_type_mappings=[""])


# ---------------------------------------------------------------------------
# The reader over the new layer
# ---------------------------------------------------------------------------


class TestTheLayerIsRead:
    def test_an_absent_directory_reads_as_an_empty_layer(self, tmp_path) -> None:
        """Every layer's reader answers `[]` for a directory that is not there.

        The layer being mandatory is `check-model`'s vocabulary, not the reader's — and for this layer nothing reports
        its absence at all, because a project with no application type needs no set."""
        (tmp_path / "model").mkdir()

        assert reader.read_mapping_sets(str(tmp_path / "model")) == []

    def test_an_empty_directory_reads_as_an_empty_layer(self, tmp_path) -> None:
        (tmp_path / "model" / "data_types" / "mapping_sets").mkdir(parents=True)

        assert reader.read_mapping_sets(str(tmp_path / "model")) == []

    def test_the_elements_come_back_in_composed_storage_path_order(self, tmp_path) -> None:
        directory = tmp_path / "model" / "data_types" / "mapping_sets"
        directory.mkdir(parents=True)
        for name in ("Physical", "Diagnostic"):
            (directory / f"{name}.yaml").write_text(
                f"name: {name}\nmaps:\n- application: Kph_T\n  implementation: Speed_T\n",
                encoding="utf-8",
                newline="\n",
            )

        elements = reader.read_mapping_sets(str(tmp_path / "model"))

        assert [element.name for element in elements] == ["Diagnostic", "Physical"]

    def test_a_file_that_does_not_validate_names_itself(self, tmp_path) -> None:
        directory = tmp_path / "model" / "data_types" / "mapping_sets"
        directory.mkdir(parents=True)
        (directory / "Physical.yaml").write_text("name: ''\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"mapping_sets[\\/]Physical\.yaml"):
            reader.read_mapping_sets(str(tmp_path / "model"))


# ---------------------------------------------------------------------------
# The declaration the classes are bound to
# ---------------------------------------------------------------------------


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


#: Per new kind: the bound class name, whether it is a member kind, and its
#: fields in declaration order. The contract's order and the class's order are
#: asserted to be the same list, which is the drift no generated artifact
#: reports.
NEW_KINDS_7_0_0: dict[str, tuple[str, bool, list[str]]] = {
    "kind:application_types.application_type": (
        "ApplicationDataType",
        False,
        [
            "name",
            "kind",
            "unit",
            "conversion",
            "range",
            "unit_ref",
            "compu_method_ref",
            "data_constr_ref",
            "description",
            "package",
        ],
    ),
    "kind:application_types.conversion": ("Conversion", True, ["factor", "offset", "limits"]),
    "kind:application_types.range": ("ValueRange", True, ["min", "max"]),
    "kind:mapping_sets.mapping_set": ("MappingSet", False, ["name", "maps", "description", "package"]),
    "kind:mapping_sets.map": ("DataTypeMap", True, ["application", "implementation"]),
}


class TestTheDeclarationAndTheClassesAgree:
    @pytest.mark.parametrize("kind_id", sorted(NEW_KINDS_7_0_0))
    def test_each_new_kind_is_bound_to_its_class_with_its_fields_in_order(self, kind_id: str) -> None:
        class_name, member_only, names = NEW_KINDS_7_0_0[kind_id]
        kind = _kinds()[kind_id]
        classes = bound_classes()

        assert kind["memberOnly"] is member_only
        assert [field["name"] for field in kind["fields"]] == names
        assert load_binding()["kinds"][kind_id]["class"] == class_name
        assert classes[kind_id].__name__ == class_name
        assert list(classes[kind_id].model_fields) == names

    def test_the_two_nested_kinds_belong_to_the_application_type_alone(self) -> None:
        kinds = _kinds()

        assert kinds["kind:application_types.conversion"]["memberOf"] == [
            {"kind": "application_type", "field": "conversion"}
        ]
        assert kinds["kind:application_types.range"]["memberOf"] == [{"kind": "application_type", "field": "range"}]

    def test_the_owning_fields_name_their_element_kind(self) -> None:
        """A nested object is a member kind and the owning field says which.

        The connector endpoint's shape, and what lets the editing protocol address `conversion.factor` rather than the
        whole record."""
        fields = {field["id"]: field for field in _kinds()["kind:application_types.application_type"]["fields"]}

        assert fields["field:application_types.application_type.conversion"]["elementKind"] == "conversion"
        assert fields["field:application_types.application_type.range"]["elementKind"] == "range"
        assert fields["field:application_types.application_type.conversion"]["type"] == "conversion"
        assert fields["field:application_types.application_type.range"]["type"] == "range"

    def test_the_pairs_are_a_collection_identified_by_the_application_side(self) -> None:
        maps = next(field for field in _kinds()["kind:mapping_sets.mapping_set"]["fields"] if field["name"] == "maps")

        assert maps["type"] == "list[map]"
        assert maps["collection"] == {
            "kind": "list",
            "memberIdentity": "field",
            "memberKinds": ["map"],
            "identityField": "application",
        }

    def test_both_sides_of_a_pair_are_ordinary_scalar_references(self) -> None:
        """Neither side declares a relation, and neither carries a binding row.

        A relation names exactly one target kind; each of these has to reach two
        — an application type or an enumeration on one side, a value type or a
        reference type on the other — so which kinds a pair may name is the
        validator's question and never the declaration's.
        """
        fields = {field["id"]: field for field in _kinds()["kind:mapping_sets.map"]["fields"]}
        relations = load_binding()["relations"]

        for field_id, layer in (
            ("field:mapping_sets.map.application", "application_types"),
            ("field:mapping_sets.map.implementation", "implementation_types"),
        ):
            assert fields[field_id]["reference"] == {"layer": layer, "identityField": "name"}
            assert "relation" not in fields[field_id]
            assert field_id not in relations

    def test_the_components_field_is_the_one_new_relation(self) -> None:
        declared = next(
            field
            for layer in load_contract()["layers"]
            for kind in layer["kinds"]
            for field in kind["fields"]
            if field["id"] == "field:swc.swc.data_type_mappings"
        )

        assert declared["type"] == "list[string]"
        assert declared["relation"]["layer"] == "mapping_sets"
        assert declared["relation"]["kind"] == "mapping_set"
        assert declared["relation"]["field"] == "name"
        assert declared["relation"]["resolution"] == "list-values"
        assert load_binding()["relations"]["field:swc.swc.data_type_mappings"] == {
            "module": "swcraft_tools.relations",
            "callable": "resolve_list_values",
        }


# ---------------------------------------------------------------------------
# The two read-only surfaces that name every declared layer
# ---------------------------------------------------------------------------


class TestTheLayerIsNamedByTheReadOnlySurfaces:
    """`list` and `query` are total over the declared layers.

    So the new one arrives in both with no edit to either module.

    What this does **not** reach: the walk surfaces. `query --dependents` reads
    the declared edges, and an edge with no detail row still draws, with no
    detail, so a missing row is invisible here.
    """

    @staticmethod
    def _seed(root) -> None:
        directory = root / "data_types" / "mapping_sets"
        directory.mkdir(parents=True)
        (directory / "Physical.yaml").write_text(
            "name: Physical\nmaps:\n- application: Kph_T\n  implementation: Speed_T\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_list_names_the_layer_and_its_elements(self, tmp_path) -> None:
        from swcraft_tools.listing import list_model

        model_root = tmp_path / "model"
        self._seed(model_root)

        listed = list_model(str(model_root))

        assert "mapping_sets" in listed
        assert tuple(listed["mapping_sets"].identities) == ("Physical",)

    def test_list_narrowed_to_the_layer_answers_it_alone(self, tmp_path) -> None:
        from swcraft_tools.listing import list_model

        model_root = tmp_path / "model"
        self._seed(model_root)

        assert list(list_model(str(model_root), "mapping_sets")) == ["mapping_sets"]

    def test_an_empty_layer_is_listed_with_no_element(self, tmp_path) -> None:
        """The state a project that declares no set is in, and it is not an error."""
        from swcraft_tools.listing import list_model

        model_root = tmp_path / "model"
        (model_root / "data_types" / "mapping_sets").mkdir(parents=True)

        assert tuple(list_model(str(model_root), "mapping_sets")["mapping_sets"].identities) == ()

    def test_query_slices_the_layer_and_its_one_collection(self, tmp_path) -> None:
        from swcraft_tools.model_state import load_model_state
        from swcraft_tools.query import slice_layer

        model_root = tmp_path / "model"
        self._seed(model_root)
        state = load_model_state(str(model_root))

        assert slice_layer(state, "mapping_sets").identities == ("Physical",)
        members = slice_layer(state, "mapping_sets", members="maps")
        assert members.members == "maps"
        assert members.identities == ({"element": "Physical", "member": "Kph_T"},)
