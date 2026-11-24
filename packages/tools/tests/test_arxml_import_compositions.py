"""Tests for ``arxml_import.connectors.map_compositions``, the composition import.

It maps the composition hierarchy, its members, its delegations, its assembly connectors and the external stubs. Fixture
cases run the real pipeline over ``composition_nested.arxml``; error cases build small ``ImportModel``s inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arxml_import_shapes_support import raw_element
from swcraft_tools.arxml_import import (
    EXTERNAL_STUB_GROUP,
    ImportedSwc,
    PathRegistry,
    derive_root,
    map_compositions,
    map_data_types,
    map_interfaces,
    map_swcs,
    reconcile_reserved_names,
)
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_reader import (
    AssemblyConnectorInfo,
    ComponentPrototypeInfo,
    CompositionInfo,
    ConnectorPortRef,
    DelegationConnectorInfo,
    ImplDataTypeInfo,
    ImportModel,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwBaseTypeInfo,
    SwcInfo,
    SwcPortInfo,
    read_arxml,
)
from swcraft_tools.schema import (
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    SWComponent,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
COMPOSITION_NESTED = FIXTURES_DIR / "composition_nested.arxml"

APPLICATION_DEST = "APPLICATION-SW-COMPONENT-TYPE"
COMPOSITION_DEST = "COMPOSITION-SW-COMPONENT-TYPE"


def _map_full(model: ImportModel, *, verdict: bool = False):
    """The real pipeline over one ``ImportModel``, in `run_import`'s own order.

    ``verdict=True`` threads the merge verdict through all four mappers, as ``swcraft import model`` does;
    the default passes none, as ``import component`` does. The two differ only for a name declared twice.
    """
    merges = decide_merges(model) if verdict else None
    forms: list = []
    data_types, registry, _type_skips = map_data_types(model, merges=merges, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=merges, elements=forms)
    swcs, swc_skips = map_swcs(
        model, interfaces, iface_skip_log=iface_skips, merges=merges, port_inits={}, variable_types={}
    )
    mapped, skip_log = map_compositions(
        model, swcs, interfaces, iface_skips, registry, merges=merges, swc_skip_log=swc_skips
    )
    return mapped, skip_log, registry, swcs


def _skip_reason(skip_log, path: str) -> str | None:
    for entry in skip_log:
        if entry.path == path:
            return entry.reason
    return None


def _composition(mapped, name: str):
    matches = [c for c in mapped.compositions if c.name == name]
    assert len(matches) == 1, f"expected exactly one composition named {name!r}"
    return matches[0]


# Fixture-driven: composition_nested.arxml


@pytest.fixture()
def composition_nested_model() -> ImportModel:
    return read_arxml(str(COMPOSITION_NESTED))


class TestTheHierarchyIsMappedRatherThanFlattened:
    def test_both_compositions_are_mapped_with_their_short_name_as_title(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        assert [c.name for c in mapped.compositions] == ["InnerComposition", "OuterComposition"]
        # The SHORT-NAME, because `title` is required and the export declares
        # none -- inventing prose would be a claim the export never made.
        assert [c.title for c in mapped.compositions] == ["InnerComposition", "OuterComposition"]

    def test_the_boundary_ports_are_mapped_with_their_direction_and_interface(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        inner = _composition(mapped, "InnerComposition")
        assert [(p.name, p.direction, p.interface) for p in inner.ports] == [
            ("pFrontValue", "provide", "ISensorValue"),
            ("pRearValue", "provide", "ISensorValue"),
        ]

    def test_a_member_names_the_type_it_instantiates_by_the_right_field(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        outer = _composition(mapped, "OuterComposition")
        # Which field carries the name is the model's spelling of the
        # `TYPE-TREF`'s `DEST`, and a composition-typed member says
        # `composition` rather than `type`.
        assert [(m.name, m.type, m.composition) for m in outer.members] == [
            ("InnerComposition_1", None, "InnerComposition"),
            ("ActuatorSwc_1", "ActuatorSwc", None),
        ]

    def test_the_delegation_survives_as_a_delegation(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        inner = _composition(mapped, "InnerComposition")
        assert [(d.name, d.port, d.member, d.member_port) for d in inner.delegations] == [
            ("DEL_Front", "pFrontValue", "SensorSwc_Front", "pValue")
        ]

    def test_the_assembly_connector_keeps_its_own_name_and_its_composition(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        assert len(mapped.connectors) == 1
        connector = mapped.connectors[0]
        # The export's own SHORT-NAME, not a name minted from four endpoint
        # names: nothing is flattened, so there is nothing to spell out.
        assert connector.name == "Assembly_Inner_to_Actuator"
        assert connector.composition == "OuterComposition"
        assert (connector.from_.composition, connector.from_.swc, connector.from_.port) == (
            "InnerComposition",
            None,
            "pFrontValue",
        )
        assert (connector.to.composition, connector.to.swc, connector.to.port) == (None, "ActuatorSwc", "rValue")

    def test_a_type_placed_twice_in_one_composition_is_skipped_with_its_rule_named(
        self, composition_nested_model
    ) -> None:
        """A type placed twice in one composition: the second prototype is skipped and reported.

        A second member of one type would make the writer's endpoint resolution ambiguous
        (``rule:composition.member-type-repeated``).
        """
        mapped, skips, _registry, _swcs = _map_full(composition_nested_model)

        inner = _composition(mapped, "InnerComposition")
        assert [m.name for m in inner.members] == ["SensorSwc_Front"]
        reason = _skip_reason(skips, "/Acme/Chassis/Compositions/InnerComposition/SensorSwc_Rear")
        assert reason is not None
        assert "already places type 'SensorSwc'" in reason
        assert "at most once per composition" in reason

    def test_a_delegation_onto_a_skipped_member_is_skipped_with_a_reason(self, composition_nested_model) -> None:
        mapped, skips, _registry, _swcs = _map_full(composition_nested_model)

        inner = _composition(mapped, "InnerComposition")
        assert "DEL_Rear" not in {d.name for d in inner.delegations}
        reason = _skip_reason(skips, "/Acme/Chassis/Compositions/InnerComposition/DEL_Rear")
        assert reason is not None
        assert "SensorSwc_Rear" in reason

    def test_the_reader_captures_each_prototypes_type_ref_and_its_dest(self, composition_nested_model) -> None:
        """The reader captures each prototype's ``DEST``, which the reference cannot give.

        A dropped ``DEST`` would emit an XSD-valid stub that dangles in the target toolchain; the other tests
        here build ``ComponentPrototypeInfo`` themselves and would not catch it."""
        prototypes = {
            proto.name: (proto.type_ref, proto.dest)
            for composition in composition_nested_model.compositions
            for proto in composition.prototypes
        }
        assert prototypes == {
            "SensorSwc_Front": ("/Acme/Chassis/Components/SensorSwc", APPLICATION_DEST),
            "SensorSwc_Rear": ("/Acme/Chassis/Components/SensorSwc", APPLICATION_DEST),
            "InnerComposition_1": ("/Acme/Chassis/Compositions/InnerComposition", COMPOSITION_DEST),
            "ActuatorSwc_1": ("/Acme/Chassis/Components/ActuatorSwc", APPLICATION_DEST),
        }

    def test_the_root_is_the_composition_no_prototype_instantiates(self, composition_nested_model) -> None:
        """The root is recorded on the compositions alone.

        ``MappedCompositions`` carries no ``root_name`` copy that pruning or a rename would leave stale.
        """
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        assert [c.name for c in mapped.compositions if c.root] == ["OuterComposition"]


# Inline models: root derivation, stubs, orientation, the skip classes


def _base_inline_model() -> tuple[ImportModel, dict[str, str]]:
    """Two leaf SWCs and one shared SR interface, with no composition content yet."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Sig_impl",
            name="Sig_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/ISig",
            name="ISig",
            description="",
            data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/Sig_impl")],
        )
    )
    paths = {
        "swc_a": "/Pkg/Components/SwcA",
        "swc_b": "/Pkg/Components/SwcB",
        "port_a": "/Pkg/Components/SwcA/pA",
        "port_b": "/Pkg/Components/SwcB/pB",
        "comp": "/Pkg/Compositions/Comp1",
        "proto_a": "/Pkg/Compositions/Comp1/SwcA_1",
        "proto_b": "/Pkg/Compositions/Comp1/SwcB_1",
        "ac": "/Pkg/Compositions/Comp1/Assembly1",
    }
    return model, paths


def _add_swc(
    model: ImportModel,
    path: str,
    name: str,
    port_name: str,
    port_kind: str,
    interface_ref: str = "/Pkg/Interfaces/ISig",
) -> None:
    model.swcs.append(
        SwcInfo(
            path=path,
            name=name,
            description="",
            ports=[
                SwcPortInfo(
                    path=f"{path}/{port_name}",
                    name=port_name,
                    kind=port_kind,
                    interface_ref=interface_ref,
                    description="",
                )
            ],
            runnables=[],
            events=[],
        )
    )


def _prototype(path: str, name: str, type_ref: str, dest: str = APPLICATION_DEST) -> ComponentPrototypeInfo:
    return ComponentPrototypeInfo(path=path, name=name, type_ref=type_ref, dest=dest)


def _composition_info(
    path: str,
    name: str,
    *,
    ports: list[SwcPortInfo] | None = None,
    prototypes: list[ComponentPrototypeInfo] | None = None,
    assembly: list[AssemblyConnectorInfo] | None = None,
    delegations: list[DelegationConnectorInfo] | None = None,
) -> CompositionInfo:
    return CompositionInfo(
        path=path,
        name=name,
        description="",
        ports=ports or [],
        prototypes=prototypes or [],
        assembly_connectors=assembly or [],
        delegation_connectors=delegations or [],
    )


def _a_composition_named_like_a_component() -> ImportModel:
    """One component ``SwcA`` and one composition ``SwcA``: the cross-kind name collision."""
    model, paths = _base_inline_model()
    _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
    model.compositions.append(_composition_info("/Pkg/Compositions/SwcA", "SwcA"))
    model.compositions.append(
        _composition_info(
            paths["comp"],
            "Comp1",
            prototypes=[
                _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                # The colliding composition is instantiated, so the root is
                # still unambiguous and this case is about the name alone.
                _prototype("/Pkg/Compositions/Comp1/SwcA_c", "SwcA_c", "/Pkg/Compositions/SwcA", COMPOSITION_DEST),
            ],
        )
    )
    return model


class TestTheRootIsDerived:
    """The importer derives the root and refuses an export without one answer.

    The rules are ``rule:composition.no-root`` and ``rule:composition.multiple-roots``.
    """

    def test_an_export_with_no_composition_derives_no_root(self) -> None:
        model, _paths = _base_inline_model()

        assert derive_root(model) is None

    def test_the_unreferenced_composition_is_the_root(self) -> None:
        model, paths = _base_inline_model()
        model.compositions.append(_composition_info("/Pkg/Compositions/Inner", "Inner"))
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Outer",
                "Outer",
                prototypes=[
                    _prototype(
                        "/Pkg/Compositions/Outer/Inner_1", "Inner_1", "/Pkg/Compositions/Inner", COMPOSITION_DEST
                    )
                ],
            )
        )
        assert paths["comp"]  # the shared paths are unused by this case

        root = derive_root(model)
        assert root is not None and root.name == "Outer"

    def test_two_unreferenced_compositions_are_an_import_error_naming_both(self) -> None:
        model, _paths = _base_inline_model()
        model.compositions.append(_composition_info("/Pkg/Compositions/AlphaTop", "AlphaTop"))
        model.compositions.append(_composition_info("/Pkg/Compositions/BetaTop", "BetaTop"))

        with pytest.raises(ValueError) as excinfo:
            derive_root(model)

        message = str(excinfo.value)
        assert "/Pkg/Compositions/AlphaTop" in message
        assert "/Pkg/Compositions/BetaTop" in message
        assert "exactly one" in message

    def test_a_hierarchy_in_which_everything_is_instantiated_is_an_import_error(self) -> None:
        model, _paths = _base_inline_model()
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/A",
                "A",
                prototypes=[_prototype("/Pkg/Compositions/A/B_1", "B_1", "/Pkg/Compositions/B", COMPOSITION_DEST)],
            )
        )
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/B",
                "B",
                prototypes=[_prototype("/Pkg/Compositions/B/A_1", "A_1", "/Pkg/Compositions/A", COMPOSITION_DEST)],
            )
        )

        with pytest.raises(ValueError) as excinfo:
            derive_root(model)

        assert "no composition is the root" in str(excinfo.value)

    def test_the_error_reaches_the_mapper_before_anything_is_mapped(self) -> None:
        model, _paths = _base_inline_model()
        model.compositions.append(_composition_info("/Pkg/Compositions/AlphaTop", "AlphaTop"))
        model.compositions.append(_composition_info("/Pkg/Compositions/BetaTop", "BetaTop"))

        with pytest.raises(ValueError):
            _map_full(model)


class TestTheExternalStub:
    """An external stub stands in for a prototype's type the imported model cannot resolve.

    The type is absent from the export, or skipped by an earlier pass.
    """

    def _model_with_absent_type(self, dest: str = APPLICATION_DEST) -> tuple[ImportModel, dict[str, str]]:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Rte_1", "Rte_1", "/Elsewhere/Rte/Rte", dest),
                ],
            )
        )
        return model, paths

    def test_a_prototype_of_an_absent_type_becomes_a_stub_with_the_reference_verbatim(self) -> None:
        model, _paths = self._model_with_absent_type()

        mapped, _skips, _registry, _swcs = _map_full(model)

        assert [s.swc.name for s in mapped.stubs] == ["Rte_ext0"]
        stub = mapped.stubs[0].swc
        assert stub.external is True
        assert stub.external_ref == "/Elsewhere/Rte/Rte"
        assert stub.external_dest == APPLICATION_DEST
        assert stub.ports == []
        assert stub.runnables == []
        assert mapped.stubs[0].group == EXTERNAL_STUB_GROUP

    def test_a_composition_typed_dest_is_carried_verbatim_too(self) -> None:
        """A composition-typed ``DEST`` is carried verbatim, never derived from the reference path."""
        model, _paths = self._model_with_absent_type(dest=COMPOSITION_DEST)

        mapped, _skips, _registry, _swcs = _map_full(model)

        assert mapped.stubs[0].swc.external_dest == COMPOSITION_DEST

    def test_the_member_still_names_the_stub_so_the_hierarchy_stays_complete(self) -> None:
        model, _paths = self._model_with_absent_type()

        mapped, _skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert [(m.name, m.type) for m in comp.members] == [("SwcA_1", "SwcA"), ("Rte_1", "Rte_ext0")]

    def test_a_present_but_skipped_type_becomes_a_stub_too(self) -> None:
        """A type the export declares but ``map_swcs`` skips becomes a stub too."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, "/Pkg/Other/SwcA", "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Twin", "Twin", "/Pkg/Other/SwcA"),
                ],
            )
        )

        mapped, _skips, _registry, swcs = _map_full(model)

        assert [s.path for s in swcs] == [paths["swc_a"]], "the second declaration was skipped by map_swcs"
        assert [s.swc.external_ref for s in mapped.stubs] == ["/Pkg/Other/SwcA"]
        comp = _composition(mapped, "Comp1")
        assert [m.type for m in comp.members] == ["SwcA", "SwcA_ext0"]

    def test_a_stub_sharing_an_imported_components_leaf_is_ext0_and_reported(self) -> None:
        """A stub sharing an imported component's leaf is ``<leaf>_ext0``; the rename line names the namesake rank."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Other", "Other", "/Elsewhere/Pkg/SwcA"),
                ],
            )
        )

        mapped, _skips, registry, _swcs = _map_full(model)

        assert [s.swc.name for s in mapped.stubs] == ["SwcA_ext0"]
        assert mapped.stubs[0].swc.external_ref == "/Elsewhere/Pkg/SwcA"
        assert registry.renames == [
            "SwcA -> SwcA_ext0 (imported stub, namesake 0 of 1 -- the stub of /Elsewhere/Pkg/SwcA)"
        ]

    def test_one_stub_per_absent_type_however_many_prototypes_reference_it(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/One",
                "One",
                prototypes=[_prototype("/Pkg/Compositions/One/Rte_1", "Rte_1", "/Elsewhere/Rte/Rte")],
            )
        )
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Top",
                "Top",
                prototypes=[
                    _prototype("/Pkg/Compositions/Top/One_1", "One_1", "/Pkg/Compositions/One", COMPOSITION_DEST),
                    _prototype("/Pkg/Compositions/Top/Rte_2", "Rte_2", "/Elsewhere/Rte/Rte"),
                ],
            )
        )

        mapped, _skips, _registry, _swcs = _map_full(model)

        assert [s.swc.name for s in mapped.stubs] == ["Rte_ext0"]
        assert len(mapped.stub_lines) == 1

    def test_a_connector_onto_a_stubs_consistent_port_is_kept_and_mints_the_port(self) -> None:
        """A connector onto a stub's port is kept, and the stub's port is minted from the end's ``DEST``."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Rte_1", "Rte_1", "/Elsewhere/Rte/Rte"),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        requester=ConnectorPortRef(
                            component_ref="/Pkg/Compositions/Comp1/Rte_1",
                            port_ref="/Elsewhere/Rte/Rte/rIn",
                            dest="R-PORT-PROTOTYPE",
                            stated_direction="require",
                        ),
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert [(c.name, c.to.swc, c.to.port) for c in mapped.connectors] == [("Assembly1", "Rte_ext0", "rIn")]
        assert _skip_reason(skips, paths["ac"]) is None
        assert mapped.unowned_port_paths == {}
        (stub,) = mapped.stubs
        assert [(p.name, p.direction, p.interface) for p in stub.swc.ports] == [("rIn", "require", None)]

    def test_a_delegation_onto_a_stubs_port_is_kept_and_one_without_a_dest_is_not(self) -> None:
        """A delegation onto a stub's port is kept once its ``DEST`` is read; an end without one is skipped."""
        model, paths = _base_inline_model()
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                ports=[
                    SwcPortInfo(
                        path=f"{paths['comp']}/pOut",
                        name="pOut",
                        kind="P-PORT-PROTOTYPE",
                        interface_ref="/Pkg/Interfaces/ISig",
                        description="",
                    )
                ],
                prototypes=[_prototype("/Pkg/Compositions/Comp1/Rte_1", "Rte_1", "/Elsewhere/Rte/Rte")],
                delegations=[
                    DelegationConnectorInfo(
                        path="/Pkg/Compositions/Comp1/DEL",
                        name="DEL",
                        inner=ConnectorPortRef(
                            component_ref="/Pkg/Compositions/Comp1/Rte_1",
                            port_ref="/Elsewhere/Rte/Rte/pOut",
                        ),
                        outer_port_ref=f"{paths['comp']}/pOut",
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert comp.delegations == []
        reason = _skip_reason(skips, "/Pkg/Compositions/Comp1/DEL")
        assert reason is not None
        assert "no DEST attribute" in reason
        assert "external stub 'Rte_ext0'" in reason
        assert mapped.stubs[0].swc.ports == []

        inner = model.compositions[-1].delegation_connectors[0].inner
        inner.dest, inner.stated_direction = "P-PORT-PROTOTYPE", "provide"
        mapped, skips, _registry, _swcs = _map_full(model)

        assert [(d.name, d.member, d.member_port) for d in _composition(mapped, "Comp1").delegations] == [
            ("DEL", "Rte_1", "pOut")
        ]
        assert _skip_reason(skips, "/Pkg/Compositions/Comp1/DEL") is None
        assert [(p.name, p.direction) for p in mapped.stubs[0].swc.ports] == [("pOut", "provide")]


class TestTheSkipClasses:
    def test_orientation_is_normalised_from_the_resolved_ports_not_the_iref_tag(self) -> None:
        """A dialect putting the R-port under PROVIDER-IREF still yields provide as ``from``, require as ``to``."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "R-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref=paths["port_b"]),
                    )
                ],
            )
        )

        mapped, _skips, _registry, _swcs = _map_full(model)

        assert len(mapped.connectors) == 1
        connector = mapped.connectors[0]
        assert (connector.from_.swc, connector.from_.port) == ("SwcB", "pB")
        assert (connector.to.swc, connector.to.port) == ("SwcA", "pA")

    def test_two_ends_of_one_direction_are_skipped_with_a_reason(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref=paths["port_b"]),
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert mapped.connectors == []
        reason = _skip_reason(skips, paths["ac"])
        assert reason is not None and "both 'provide'" in reason

    def test_an_endpoint_naming_a_prototype_of_another_composition_is_skipped(self) -> None:
        """An endpoint naming a prototype of another composition is skipped.

        A connector belongs to exactly one composition.
        """
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Other",
                "Other",
                prototypes=[_prototype("/Pkg/Compositions/Other/SwcB_1", "SwcB_1", paths["swc_b"])],
            )
        )
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(
                        "/Pkg/Compositions/Comp1/Other_1", "Other_1", "/Pkg/Compositions/Other", COMPOSITION_DEST
                    ),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        requester=ConnectorPortRef(
                            component_ref="/Pkg/Compositions/Other/SwcB_1", port_ref=paths["port_b"]
                        ),
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert mapped.connectors == []
        reason = _skip_reason(skips, paths["ac"])
        assert reason is not None and "not a member of 'Comp1'" in reason

    def test_a_port_reference_naming_another_type_is_skipped_rather_than_matched_by_name(self) -> None:
        """A port reference naming another type is skipped, never matched by name.

        Resolving it by last segment would import an XSD-valid, wrong wiring (malfunction class (1)).
        """
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pA", "R-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref="/Pkg/Components/SwcB/pA"),
                        requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref="/Pkg/Components/SwcB/pA"),
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert mapped.connectors == []
        reason = _skip_reason(skips, paths["ac"])
        assert reason is not None and "not a port of member 'SwcA_1's type 'SwcA'" in reason

    def test_a_connector_onto_a_port_map_swcs_skipped_chains_the_reason(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE", interface_ref="/Pkg/Interfaces/Missing")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path=paths["ac"],
                        name="Assembly1",
                        provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref=paths["port_b"]),
                    )
                ],
            )
        )

        # The file declares the port it skipped: a chained skip, not the case of
        # a file that declares nothing there.
        model.path_index[paths["port_b"]] = raw_element(paths["port_b"])

        mapped, skips, _registry, _swcs = _map_full(model)

        assert mapped.connectors == []
        reason = _skip_reason(skips, paths["ac"])
        assert reason == f"requester end: unresolved reference {paths['port_b']} (port was skipped)"

    def test_a_pr_boundary_port_is_skipped_with_the_shared_reason(self) -> None:
        """A boundary PR port is skipped with the leaf ports' reason; an undelegated boundary port is no skip."""
        model, paths = _base_inline_model()
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                ports=[
                    SwcPortInfo(
                        path=f"{paths['comp']}/prBoth",
                        name="prBoth",
                        kind="PR-PORT-PROTOTYPE",
                        interface_ref="/Pkg/Interfaces/ISig",
                        description="",
                    ),
                    SwcPortInfo(
                        path=f"{paths['comp']}/pLonely",
                        name="pLonely",
                        kind="P-PORT-PROTOTYPE",
                        interface_ref="/Pkg/Interfaces/ISig",
                        description="",
                    ),
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert [p.name for p in comp.ports] == ["pLonely"]
        reason = _skip_reason(skips, f"{paths['comp']}/prBoth")
        assert reason is not None and "PR (combined provide+require)" in reason
        assert _skip_reason(skips, f"{paths['comp']}/pLonely") is None

    def test_a_delegation_whose_two_ports_disagree_in_direction_is_skipped(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                ports=[
                    SwcPortInfo(
                        path=f"{paths['comp']}/rOut",
                        name="rOut",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref="/Pkg/Interfaces/ISig",
                        description="",
                    )
                ],
                prototypes=[_prototype(paths["proto_a"], "SwcA_1", paths["swc_a"])],
                delegations=[
                    DelegationConnectorInfo(
                        path="/Pkg/Compositions/Comp1/DEL",
                        name="DEL",
                        inner=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        outer_port_ref=f"{paths['comp']}/rOut",
                    )
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert comp.delegations == []
        reason = _skip_reason(skips, "/Pkg/Compositions/Comp1/DEL")
        assert reason is not None and "must declare the same direction" in reason

    def test_a_second_connector_of_one_name_is_skipped_not_silently_overwritten(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE")
        endpoints = dict(
            provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
            requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref=paths["port_b"]),
        )
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Other",
                "Other",
                prototypes=[
                    _prototype("/Pkg/Compositions/Other/SwcA_1", "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Other/SwcB_1", "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(
                        path="/Pkg/Compositions/Other/Assembly1",
                        name="Assembly1",
                        provider=ConnectorPortRef(
                            component_ref="/Pkg/Compositions/Other/SwcA_1", port_ref=paths["port_a"]
                        ),
                        requester=ConnectorPortRef(
                            component_ref="/Pkg/Compositions/Other/SwcB_1", port_ref=paths["port_b"]
                        ),
                    )
                ],
            )
        )
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                    _prototype(
                        "/Pkg/Compositions/Comp1/Other_1", "Other_1", "/Pkg/Compositions/Other", COMPOSITION_DEST
                    ),
                ],
                assembly=[AssemblyConnectorInfo(path=paths["ac"], name="Assembly1", **endpoints)],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert [c.name for c in mapped.connectors] == ["Assembly1"]
        reason = _skip_reason(skips, paths["ac"])
        assert reason is not None and "duplicate connector name 'Assembly1'" in reason

    def test_a_second_connector_of_one_link_is_skipped_naming_the_rule(self) -> None:
        """A second connector of one endpoint pair is skipped naming ``rule:connector.duplicate-link``."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE")
        endpoints = dict(
            provider=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
            requester=ConnectorPortRef(component_ref=paths["proto_b"], port_ref=paths["port_b"]),
        )
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcB_1", paths["swc_b"]),
                ],
                assembly=[
                    AssemblyConnectorInfo(path=paths["ac"], name="Assembly1", **endpoints),
                    AssemblyConnectorInfo(path=f"{paths['ac']}b", name="Assembly2", **endpoints),
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert [c.name for c in mapped.connectors] == ["Assembly1"]
        reason = _skip_reason(skips, f"{paths['ac']}b")
        assert reason is not None and "rule:connector.duplicate-link" in reason

    def test_a_duplicate_member_name_is_skipped_with_its_whole_reason(self) -> None:
        """A duplicate member name is skipped with its whole reason.

        It is owner-scoped (duplicate content inside one element), so the merge verdict never reaches it."""
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype(paths["proto_b"], "SwcA_1", paths["swc_b"]),
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert [m.name for m in comp.members] == ["SwcA_1"]
        assert _skip_reason(skips, paths["proto_b"]) == "duplicate member name 'SwcA_1' in composition 'Comp1'"

    def test_a_second_delegation_of_one_link_is_skipped_naming_the_rule(self) -> None:
        """A second delegation of one ``(boundary port, member, member port)`` triple is skipped naming the rule.

        The whole message is pinned. Does not reach: a second delegation of one *name*, a different skip.
        """
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                ports=[
                    SwcPortInfo(
                        path=f"{paths['comp']}/pOut",
                        name="pOut",
                        kind="P-PORT-PROTOTYPE",
                        interface_ref="/Pkg/Interfaces/ISig",
                        description="",
                    )
                ],
                prototypes=[_prototype(paths["proto_a"], "SwcA_1", paths["swc_a"])],
                delegations=[
                    DelegationConnectorInfo(
                        path="/Pkg/Compositions/Comp1/DelA",
                        name="DelA",
                        inner=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        outer_port_ref=f"{paths['comp']}/pOut",
                    ),
                    DelegationConnectorInfo(
                        path="/Pkg/Compositions/Comp1/DelB",
                        name="DelB",
                        inner=ConnectorPortRef(component_ref=paths["proto_a"], port_ref=paths["port_a"]),
                        outer_port_ref=f"{paths['comp']}/pOut",
                    ),
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        comp = _composition(mapped, "Comp1")
        assert [d.name for d in comp.delegations] == ["DelA"]
        assert _skip_reason(skips, "/Pkg/Compositions/Comp1/DelB") == (
            "duplicate delegation link pOut -> SwcA_1.pA inside composition 'Comp1' "
            "(already declared by /Pkg/Compositions/Comp1/DelA) -- "
            "`rule:composition.duplicate-delegation-link` would refuse the imported project"
        )

    def test_a_composition_named_like_an_imported_component_is_skipped(self) -> None:
        """A composition named like an imported component is skipped.

        Both share ``/ComponentTypes``' ``SHORT-NAME`` scope. The whole message is pinned, its prefix included.
        """
        model = _a_composition_named_like_a_component()

        mapped, skips, _registry, _swcs = _map_full(model)

        assert [c.name for c in mapped.compositions] == ["Comp1"]
        assert _skip_reason(skips, "/Pkg/Compositions/SwcA") == (
            "duplicate component-type name 'SwcA' -- a composition and a component are "
            "siblings in /ComponentTypes and cannot share one SHORT-NAME"
        )

    def test_the_cross_kind_collision_is_skipped_the_same_way_under_the_verdict(self) -> None:
        """The cross-kind collision is skipped the same way with the verdict active over all four mappers.

        Does not reach: two compositions of one name (the merge module's subject), nor the sentence a
        caller passing no verdict gets for it."""
        model = _a_composition_named_like_a_component()

        mapped, skips, _registry, _swcs = _map_full(model, verdict=True)

        assert [c.name for c in mapped.compositions] == ["Comp1"]
        assert _skip_reason(skips, "/Pkg/Compositions/SwcA") == (
            "duplicate component-type name 'SwcA' -- a composition and a component are "
            "siblings in /ComponentTypes and cannot share one SHORT-NAME"
        )


# The component-type namespace: compositions and components are siblings


def _imported(name: str, path: str | None = None):
    return ImportedSwc(swc=SWComponent(name=name, type="application"), group="Components", path=path or f"/Pkg/{name}")


class TestTheReservedNamePassSeesOneComponentTypeNamespace:
    """The reserved-name pass reconciles compositions and components together.

    They share ``/ComponentTypes``' ``SHORT-NAME`` scope: a replacement must not land on a composition's name, and a
    composition named like a generated ``SWC-IMPLEMENTATION`` must move.
    """

    def test_a_replacement_never_lands_on_a_declared_composition_name(self) -> None:
        swcs = [_imported("A"), _imported("A_Implementation")]
        compositions = [
            Composition(
                name="Root",
                title="Root",
                root=True,
                members=[
                    CompositionMember(name="m1", type="A"),
                    CompositionMember(name="m2", type="A_Implementation"),
                ],
            ),
            Composition(name="A_Implementation_app", title="Taken"),
        ]

        reconciled = reconcile_reserved_names([], [], swcs, [], compositions, PathRegistry(), [])

        component_type_names = [s.swc.name for s in reconciled.swcs] + [c.name for c in reconciled.compositions]
        assert len(component_type_names) == len(set(component_type_names)), component_type_names

    def test_a_composition_named_like_a_generated_implementation_is_renamed(self) -> None:
        swcs = [_imported("B")]
        compositions = [
            Composition(
                name="Root",
                title="Root",
                root=True,
                members=[
                    CompositionMember(name="m1", type="B"),
                    CompositionMember(name="m2", composition="B_Implementation"),
                ],
            ),
            Composition(name="B_Implementation", title="Derived"),
        ]
        registry = PathRegistry()

        reconciled = reconcile_reserved_names([], [], swcs, [], compositions, registry, [])

        assert [c.name for c in reconciled.compositions] == ["Root", "B_Implementation_app"]
        assert any("SWC-IMPLEMENTATION" in line for line in registry.renames)

    def test_a_composition_rename_is_followed_into_every_reference(self) -> None:
        swcs = [_imported("B")]
        compositions = [
            Composition(
                name="Root",
                title="Root",
                root=True,
                members=[
                    CompositionMember(name="m1", type="B"),
                    CompositionMember(name="m2", composition="B_Implementation"),
                ],
            ),
            Composition(
                name="B_Implementation",
                title="Derived",
                ports=[CompositionPort(name="pOut", direction="provide", interface="ISig")],
            ),
        ]
        connectors = [
            Connector(
                name="C1",
                **{"from": ConnectorEndpoint(composition="B_Implementation", port="pOut")},
                to=ConnectorEndpoint(swc="B", port="rIn"),
                composition="Root",
            ),
            Connector(
                name="C2",
                **{"from": ConnectorEndpoint(swc="B", port="pOut")},
                to=ConnectorEndpoint(swc="B", port="rIn"),
                composition="B_Implementation",
            ),
        ]

        reconciled = reconcile_reserved_names([], [], swcs, connectors, compositions, PathRegistry(), [])

        root = next(c for c in reconciled.compositions if c.name == "Root")
        assert [(m.name, m.type, m.composition) for m in root.members] == [
            ("m1", "B", None),
            ("m2", None, "B_Implementation_app"),
        ]
        by_name = {c.name: c for c in reconciled.connectors}
        assert by_name["C1"].from_.composition == "B_Implementation_app"
        assert by_name["C2"].composition == "B_Implementation_app"


class TestABlankDestIsRefusedLikeABlankTypeRef:
    """A stub prototype without a ``DEST`` is refused like a blank ``type_ref``.

    The writer emits ``external_dest`` as ``DEST``, and a reference without one aborts ``generate arxml``.
    """

    def _model_with_blank_dest(self):
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Rte_1", "Rte_1", "/Elsewhere/Rte/Rte", dest=""),
                ],
            )
        )
        return model, paths

    def test_no_stub_is_manufactured_for_a_prototype_with_no_dest(self) -> None:
        model, _paths = self._model_with_blank_dest()

        mapped, _skips, _registry, _swcs = _map_full(model)

        assert mapped.stubs == []
        assert mapped.stub_lines == {}

    def test_the_prototype_is_skipped_with_a_reason_naming_the_dest(self) -> None:
        model, _paths = self._model_with_blank_dest()

        mapped, skips, _registry, _swcs = _map_full(model)

        assert [m.name for m in _composition(mapped, "Comp1").members] == ["SwcA_1"]
        reason = _skip_reason(skips, "/Pkg/Compositions/Comp1/Rte_1")
        assert reason is not None, [(e.path, e.reason) for e in skips]
        assert "DEST" in reason


class TestAnUnusableReferencePairIsRefusedLikeABlankOne:
    """The guard is as wide as ``rule:swc.external-incomplete-reference``, which checks both halves for shape.

    Each case is one shape a blankness-only guard lets through: a ``DEST`` that is not a ``DEST``, a
    ``TYPE-TREF`` that is not a path, and a whitespace-only ``TYPE-TREF``.
    """

    def _model_with(self, type_ref: str, dest: str):
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype(paths["proto_a"], "SwcA_1", paths["swc_a"]),
                    _prototype("/Pkg/Compositions/Comp1/Rte_1", "Rte_1", type_ref, dest=dest),
                ],
            )
        )
        return model

    @pytest.mark.parametrize(
        ("type_ref", "dest", "expected"),
        [
            ("/Elsewhere/Rte/Rte", "not a dest <&>", "not an AUTOSAR reference DEST"),
            ("not a path at all", "APPLICATION-SW-COMPONENT-TYPE", "not an AUTOSAR reference path"),
            # A path inside a package the writer declares the project's own
            # elements in: every prototype of the stub would bind to whatever
            # the project declares there.
            (
                "/ComponentTypes/SwcA",
                "APPLICATION-SW-COMPONENT-TYPE",
                "lies inside /ComponentTypes, a package the generated ARXML declares this project's own elements in",
            ),
        ],
    )
    def test_an_unusable_pair_mints_no_stub_and_is_skipped_once(self, type_ref: str, dest: str, expected: str) -> None:
        mapped, skips, _registry, _swcs = _map_full(self._model_with(type_ref, dest))

        assert mapped.stubs == []
        assert [m.name for m in _composition(mapped, "Comp1").members] == ["SwcA_1"]
        lines = [entry.reason for entry in skips if entry.path == "/Pkg/Compositions/Comp1/Rte_1"]
        assert len(lines) == 1, lines
        assert expected in lines[0]

    def test_a_whitespace_only_type_ref_is_one_skip_and_no_stub(self) -> None:
        """A whitespace-only reference is one skip line and no stub, not two skip lines."""
        mapped, skips, _registry, _swcs = _map_full(self._model_with("   ", "APPLICATION-SW-COMPONENT-TYPE"))

        assert mapped.stubs == []
        lines = [entry.reason for entry in skips if entry.path == "/Pkg/Compositions/Comp1/Rte_1"]
        assert lines == ["component prototype has no TYPE-TREF"]


class TestADerivedRootThatDoesNotSurviveIsRefused:
    """A derived root the name pass drops is refused, never written without a root (``rule:composition.no-root``)."""

    def test_a_root_whose_short_name_is_blank_is_an_import_error(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Inner",
                "Inner",
                prototypes=[_prototype("/Pkg/Compositions/Inner/SwcA_1", "SwcA_1", paths["swc_a"])],
            )
        )
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Top",
                "   ",
                prototypes=[
                    _prototype("/Pkg/Compositions/Top/Inner_1", "Inner_1", "/Pkg/Compositions/Inner", COMPOSITION_DEST)
                ],
            )
        )

        with pytest.raises(ValueError) as excinfo:
            _map_full(model)

        assert "/Pkg/Compositions/Top" in str(excinfo.value)

    def test_a_root_colliding_with_a_component_name_is_an_import_error(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Inner",
                "Inner",
                prototypes=[_prototype("/Pkg/Compositions/Inner/SwcA_1", "SwcA_1", paths["swc_a"])],
            )
        )
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/SwcA",
                "SwcA",
                prototypes=[
                    _prototype("/Pkg/Compositions/SwcA/Inner_1", "Inner_1", "/Pkg/Compositions/Inner", COMPOSITION_DEST)
                ],
            )
        )

        with pytest.raises(ValueError) as excinfo:
            _map_full(model)

        assert "/Pkg/Compositions/SwcA" in str(excinfo.value)


class TestTheDuplicateLinkKeyMirrorsTheValidators:
    """The duplicate-link key is ``rule:connector.duplicate-link``'s.

    It is the composition, and each endpoint's field, name and port.
    """

    def test_the_same_endpoint_pair_in_two_compositions_is_not_a_duplicate(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, paths["swc_a"], "SwcA", "pA", "P-PORT-PROTOTYPE")
        _add_swc(model, paths["swc_b"], "SwcB", "pB", "R-PORT-PROTOTYPE")
        for composition in ("Comp1", "Comp2"):
            base = f"/Pkg/Compositions/{composition}"
            model.compositions.append(
                _composition_info(
                    base,
                    composition,
                    prototypes=[
                        _prototype(f"{base}/SwcA_1", "SwcA_1", paths["swc_a"]),
                        _prototype(f"{base}/SwcB_1", "SwcB_1", paths["swc_b"]),
                    ],
                    assembly=[
                        AssemblyConnectorInfo(
                            path=f"{base}/Assembly_{composition}",
                            name=f"Assembly_{composition}",
                            provider=ConnectorPortRef(component_ref=f"{base}/SwcA_1", port_ref=paths["port_a"]),
                            requester=ConnectorPortRef(component_ref=f"{base}/SwcB_1", port_ref=paths["port_b"]),
                        )
                    ],
                )
            )
        # A third, unreferenced composition is the root, so `derive_root` has one
        # answer and neither wired composition is it.
        model.compositions.append(
            _composition_info(
                "/Pkg/Compositions/Top",
                "Top",
                prototypes=[
                    _prototype("/Pkg/Compositions/Top/C1", "C1", "/Pkg/Compositions/Comp1", COMPOSITION_DEST),
                    _prototype("/Pkg/Compositions/Top/C2", "C2", "/Pkg/Compositions/Comp2", COMPOSITION_DEST),
                ],
            )
        )

        mapped, skips, _registry, _swcs = _map_full(model)

        assert sorted(c.name for c in mapped.connectors) == ["Assembly_Comp1", "Assembly_Comp2"]
        assert [e.reason for e in skips if "duplicate connector link" in e.reason] == []


# The source path of every mapped connector and composition -- the
# whole-export importer's write order for the two layers


class TestTheMappedHierarchyNamesTheSourcePathOfEachWrittenElement:
    """``map_compositions`` returns the connector and composition name/path maps it keeps."""

    def test_every_connector_and_composition_maps_to_its_own_path(self, composition_nested_model) -> None:
        mapped, _skips, _registry, _swcs = _map_full(composition_nested_model)

        assert mapped.connector_source_paths == {
            "Assembly_Inner_to_Actuator": "/Acme/Chassis/Compositions/OuterComposition/Assembly_Inner_to_Actuator"
        }
        assert mapped.composition_source_paths == {
            "InnerComposition": "/Acme/Chassis/Compositions/InnerComposition",
            "OuterComposition": "/Acme/Chassis/Compositions/OuterComposition",
        }
        assert set(mapped.connector_source_paths) == {connector.name for connector in mapped.connectors}
        assert set(mapped.composition_source_paths) == {composition.name for composition in mapped.compositions}


class TestAComponentStubPastAClaimedName:
    """A component stub's name is ranked past a name the import already holds.

    Its line still names the namesake index.
    """

    def test_an_imported_component_named_like_the_rank_0_stub_moves_the_stub_to_ext1(self) -> None:
        model, paths = _base_inline_model()
        _add_swc(model, "/Pkg/Swcs/SwcA_ext0", "SwcA_ext0", "pA", "P-PORT-PROTOTYPE")
        model.compositions.append(
            _composition_info(
                paths["comp"],
                "Comp1",
                prototypes=[
                    _prototype("/Pkg/Compositions/Comp1/Taken", "Taken", "/Pkg/Swcs/SwcA_ext0"),
                    _prototype("/Pkg/Compositions/Comp1/Other", "Other", "/Elsewhere/Pkg/SwcA"),
                ],
            )
        )

        mapped, _skips, registry, _swcs = _map_full(model)

        assert [s.swc.name for s in mapped.stubs] == ["SwcA_ext1"]
        comp = _composition(mapped, "Comp1")
        assert [(m.name, m.type) for m in comp.members] == [("Taken", "SwcA_ext0"), ("Other", "SwcA_ext1")]
        assert registry.renames == [
            "SwcA -> SwcA_ext1 (imported stub, namesake 0 of 1 -- the stub of /Elsewhere/Pkg/SwcA)"
        ]
