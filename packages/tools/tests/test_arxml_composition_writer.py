"""The composition and connector emitters, each tested directly against a named slice of a golden.

The rules held here:

* the composition always carries a ``PORTS`` element, empty when it declares no boundary port, and its
  declared ``SHORT-NAME``;
* ``PROVIDER-IREF``/``REQUESTER-IREF`` carry **no** ``DEST``; only their children do;
* ``CONTEXT-COMPONENT-REF`` points at the **member** inside the connector's composition and
  ``TARGET-*-PORT-REF`` at the port of the *component type*;
* what is emitted is what the ``compositions/`` layer declares; only the consistency **check** is gated on
  the connector list.

Delegations, external stubs, a member named apart from its type and composition-typed endpoints are in
``test_arxml_compositions_writer.py``: no committed golden reaches them. The goldens cannot observe
prototype or connector order, so ``composition_shapes`` builds a model whose read order and alphabetical
order disagree.

A ``*_matches_the_generator_path`` test compares the emitter against the dispatcher's use of it, which is
not an independent oracle; the three byte goldens and ``TestSemanticRoundTrip`` are.
"""

from __future__ import annotations

import shutil

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    _root_composition,
    golden_slice,
    make_empty_model_root,
    read_golden,
    write_model_element,
)
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_data_types,
    read_interfaces,
    read_swc_model,
)
from swcraft_tools.schema import Composition, CompositionMember, Connector, ConnectorEndpoint, SWComponent
from swcraft_tools.writer.arxml_generation import (
    _check_connectors,
    _emit_assembly_connector,
    _emit_composition,
    _emit_compositions,
    _emit_swcs,
    build_workspace,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, ArxmlDocument, ShortNameScope, serialize_fragment
from swcraft_tools.writer.composition_structure import compositions_by_name

#: The type every ad-hoc (non-golden) interface in this module references: a value type over a
#: ``{name}_base`` base type, which every model root here declares beside it.
_UINT8_BASE = {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
_UINT8 = {"name": "uint8", "kind": "value_type", "base": "uint8_base"}

#: One sender-receiver interface is enough: nothing these emitters write varies with the
#: interface kind.
_ILINK: dict = {
    "name": "ILink",
    "kind": "sender_receiver",
    "data_elements": [{"name": "value", "implementation_type": "uint8"}],
}

#: ``(subdirectory, swc)`` pairs. The subdirectory orders the read: ``Zulu``, ``Alpha``, ``Mike``. Three,
#: because with two no order can differ from both the read order and the alphabetical one.
_SHAPE_SWCS: list[tuple[str, dict]] = [
    (
        "a",
        {
            "name": "Zulu",
            "type": "application",
            "ports": [
                {"name": "pOut", "direction": "provide", "interface": "ILink"},
                {"name": "rBack", "direction": "require", "interface": "ILink"},
            ],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
        },
    ),
    (
        "b",
        {
            "name": "Alpha",
            "type": "application",
            "ports": [
                {"name": "rIn", "direction": "require", "interface": "ILink"},
                {"name": "pBack", "direction": "provide", "interface": "ILink"},
            ],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
        },
    ),
    (
        "c",
        {
            "name": "Mike",
            "type": "application",
            "ports": [],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
        },
    ),
]

#: The root's declared members, in declaration order, agreeing with neither the read order nor the
#: alphabetical order.
_SHAPE_MEMBERS = ["Mike", "Zulu", "Alpha"]

#: ``(filename, connector)`` pairs, named out of step with their ``name`` field so that file order and
#: name order disagree; a hand-authored model reaches the same state.
_SHAPE_CONNECTORS: list[tuple[str, dict]] = [
    (
        "01-forward",
        {
            "name": "zForward",
            "from": {"swc": "Zulu", "port": "pOut"},
            "to": {"swc": "Alpha", "port": "rIn"},
        },
    ),
    (
        "02-back",
        {
            "name": "aBack",
            "from": {"swc": "Alpha", "port": "pBack"},
            "to": {"swc": "Zulu", "port": "rBack"},
        },
    ),
]

#: The ``SHORT-NAME`` of the root composition every model root in this module
#: declares — the two goldens' and the shape fixture's. It is a fact about those
#: model files, not a constant the writer owns.
ROOT_NAME = "Root"


def _write_model(model_root, subdir: str, data: dict, stem: str | None = None) -> None:
    """Write one ``<subdir>/<stem>.yaml`` element file into a materialized model root."""
    path = model_root / subdir / f"{stem or data['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _model_interfaces(model_root: str) -> dict:
    """A model root's interfaces, keyed by name (the registry shape the emitter takes)."""
    return {iface.name: iface for iface in read_interfaces(model_root)}


def _model_types(model_root: str) -> dict:
    """A model root's data types, keyed by name; callers pass the real map.

    With an empty map the delegation gate accepts two interfaces differing only in a member's resolved type.
    """
    return {dt.name: dt for dt in read_data_types(model_root)}


def _emit_whole_package(model_root: str, composition: str = ROOT_NAME) -> tuple[ArxmlDocument, str]:
    """Emit a model root's components, then its compositions, into a fresh document.

    Returns the document and the named composition's serialized fragment (the root by default).
    """
    document = ArxmlDocument()
    swcs = read_swc_model(model_root)
    connectors = read_connectors(model_root)
    compositions = read_compositions(model_root)
    _emit_swcs(document.component_types, swcs, _model_interfaces(model_root))
    emitted = _emit_compositions(
        document.component_types,
        compositions,
        swcs,
        connectors,
        _model_interfaces(model_root),
        model_root,
        _model_types(model_root),
    )
    by_name = {node.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text: node for node in emitted}
    return document, "" if composition not in by_name else serialize_fragment(by_name[composition])


def _package_short_names(package) -> list[str]:
    """The ``SHORT-NAME``s of a package's elements, in emission order."""
    return [element.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text for element in package.elements]


def _connector_names(model_root: str) -> list[str]:
    """Every connector name in a model, in read order."""
    return [connector.name for connector in read_connectors(model_root)]


def _root_of(model_root: str) -> Composition:
    """The root composition of a model root, as the emitters resolve it."""
    return next(composition for composition in read_compositions(model_root) if composition.root)


#: Every connector in each golden's model, as ``(golden, name)`` pairs.
GOLDEN_CONNECTORS = [
    (golden, name) for golden, root in GOLDEN_MODEL_ROOTS.items() for name in _connector_names(str(root))
]


@pytest.fixture(scope="module")
def composition_shapes(tmp_path_factory) -> tuple[str, str]:
    """``_SHAPE_SWCS`` + ``_SHAPE_CONNECTORS`` through ``generate_arxml()``: the ARXML text and its model root."""
    tmp_path = tmp_path_factory.mktemp("composition_shapes")
    model_root = make_empty_model_root(tmp_path)
    write_model_element(model_root, _UINT8_BASE)
    _write_model(model_root, "data_types/implementation", _UINT8)
    _write_model(model_root, "interfaces", _ILINK)
    for subdir, swc in _SHAPE_SWCS:
        _write_model(model_root, f"swc/{subdir}", swc)
    for stem, connector in _SHAPE_CONNECTORS:
        _write_model(model_root, "connectors", connector, stem=stem)
    _root_composition(model_root, _SHAPE_MEMBERS, name=ROOT_NAME)
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8"), str(model_root)


class TestCompositionEmitter:
    """``COMPOSITION-SW-COMPONENT-TYPE`` as a whole."""

    @pytest.mark.parametrize("golden", sorted(GOLDEN_MODEL_ROOTS))
    def test_composition_matches_the_golden_slice(self, golden):
        _document, emitted = _emit_whole_package(str(GOLDEN_MODEL_ROOTS[golden]))
        assert emitted == golden_slice(read_golden(golden), ROOT_NAME)

    def test_composition_matches_the_generator_path_for_the_shape_fixture(self, composition_shapes):
        text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert emitted == golden_slice(text, ROOT_NAME)

    @pytest.mark.parametrize("golden", sorted(GOLDEN_MODEL_ROOTS))
    def test_one_component_prototype_per_declared_member(self, golden):
        model_root = str(GOLDEN_MODEL_ROOTS[golden])
        _document, emitted = _emit_whole_package(model_root)
        assert emitted.count("<SW-COMPONENT-PROTOTYPE>") == len(_root_of(model_root).members)

    def test_prototypes_follow_the_declared_member_order(self, composition_shapes):
        """The prototype order is the root's ``members`` list, which disagrees with read and alphabetical order."""
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert [swc.name for swc in read_swc_model(model_root)] == ["Zulu", "Alpha", "Mike"]
        positions = [emitted.index(f"<SHORT-NAME>{name}</SHORT-NAME>") for name in _SHAPE_MEMBERS]
        assert positions == sorted(positions)

    def test_a_prototype_type_tref_points_at_the_component_type(self, composition_shapes):
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert '<TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/ComponentTypes/Zulu</TYPE-TREF>' in emitted

    def test_the_composition_carries_an_empty_two_line_ports_element(self, composition_shapes):
        # Emitted even with no boundary port, in its two-line empty form, like `_emit_swc`'s PORTS.
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert "  <PORTS>\n  </PORTS>\n" in emitted

    def test_the_composition_is_the_packages_last_element(self, composition_shapes):
        # A two-pass emitter that collected compositions before the components
        # would still be valid ARXML and would still pass every slice test.
        _text, model_root = composition_shapes
        document, _emitted = _emit_whole_package(model_root)
        assert _package_short_names(document.component_types) == [
            "Zulu",
            "Zulu_Implementation",
            "Alpha",
            "Alpha_Implementation",
            "Mike",
            "Mike_Implementation",
            ROOT_NAME,
        ]

    def test_the_composition_name_is_the_declared_one(self, composition_shapes):
        """The composition's name comes from its file."""
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert f"<SHORT-NAME>{ROOT_NAME}</SHORT-NAME>" in emitted
        assert "SwCraftComposition" not in emitted

    def test_a_member_less_composition_omits_the_components_wrapper(self):
        # Reachable: a freshly `swcraft init`-ed project declares a root with no members at all.
        node = _emit_composition(
            ArxmlDocument().component_types,
            Composition(name="Empty", title="Empty"),
            [],
            {},
            {},
            {},
            {},
        )
        assert "<COMPONENTS>" not in serialize_fragment(node)
        assert "<CONNECTORS>" not in serialize_fragment(node)
        assert "  <PORTS>\n  </PORTS>\n" in serialize_fragment(node)


class TestAssemblyConnector:
    """``ASSEMBLY-SW-CONNECTOR`` and its two instance references."""

    @pytest.mark.parametrize(("golden", "name"), GOLDEN_CONNECTORS)
    def test_connector_matches_the_golden_slice(self, golden, name):
        _document, emitted = _emit_whole_package(str(GOLDEN_MODEL_ROOTS[golden]))
        assert golden_slice(emitted, name) == golden_slice(read_golden(golden), name)

    @pytest.mark.parametrize("name", [connector["name"] for _stem, connector in _SHAPE_CONNECTORS])
    def test_connector_matches_the_generator_path_for_every_shape(self, composition_shapes, name):
        text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert golden_slice(emitted, name) == golden_slice(text, name)

    def test_connectors_follow_identity_order_not_file_order(self, composition_shapes):
        # `01-forward.yaml` holds `zForward` and `02-back.yaml` holds `aBack`, so file order and
        # identity order disagree; the loader orders a layer's elements by their declared
        # identity, so renaming a file changes no emitted byte. Both goldens have a single
        # connector and cannot tell the two apart.
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert _connector_names(model_root) == ["aBack", "zForward"]
        assert emitted.index("<SHORT-NAME>aBack</SHORT-NAME>") < emitted.index("<SHORT-NAME>zForward</SHORT-NAME>")

    def test_the_iref_wrappers_carry_no_dest(self, composition_shapes):
        # The one place in the writer where a reference-shaped element is *not* built through
        # `reference_element()`.
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        assert "<PROVIDER-IREF>" in emitted
        assert "<REQUESTER-IREF>" in emitted
        assert "<PROVIDER-IREF " not in emitted
        assert "<REQUESTER-IREF " not in emitted

    def test_the_context_and_target_refs_use_different_base_paths(self, composition_shapes):
        # The context is the prototype *inside* the composition, the target is the port of the
        # component type.
        _text, model_root = composition_shapes
        _document, emitted = _emit_whole_package(model_root)
        connector = golden_slice(emitted, "zForward")
        assert (
            '<CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">'
            f"/ComponentTypes/{ROOT_NAME}/Zulu</CONTEXT-COMPONENT-REF>" in connector
        )
        assert '<TARGET-P-PORT-REF DEST="P-PORT-PROTOTYPE">/ComponentTypes/Zulu/pOut</TARGET-P-PORT-REF>' in connector
        assert (
            '<CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">'
            f"/ComponentTypes/{ROOT_NAME}/Alpha</CONTEXT-COMPONENT-REF>" in connector
        )
        assert '<TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Alpha/rIn</TARGET-R-PORT-REF>' in connector

    @staticmethod
    def _owner() -> Composition:
        """A one-off composition placing ``Src`` and ``Dst`` under their own names."""
        return Composition(
            name="Owner",
            title="Owner",
            members=[CompositionMember(name="Src", type="Src"), CompositionMember(name="Dst", type="Dst")],
        )

    def _emit_one(self, connector: Connector) -> str:
        host = ArxmlDocument().component_types.create_element("CONNECTORS-HOST", "irrelevant")
        owner = self._owner()
        swc_map = {name: SWComponent(name=name) for name in ("Src", "Dst")}
        # A real `ShortNameScope`, because the emitter takes one and nothing else: the scope is
        # the sibling uniqueness backstop.
        node = _emit_assembly_connector(
            ShortNameScope(host, "CONNECTORS-HOST"),
            connector,
            owner,
            swc_map,
            compositions_by_name([owner]),
        )
        return serialize_fragment(node)

    def test_the_provider_side_is_the_from_endpoint(self):
        # `check_connector_compatibility()` enforces from=provide / to=require,
        # so the emitter maps `from_` onto PROVIDER-IREF without re-deriving it.
        emitted = self._emit_one(
            Connector(
                name="link",
                **{"from": ConnectorEndpoint(swc="Src", port="pOut")},
                to=ConnectorEndpoint(swc="Dst", port="rIn"),
            )
        )
        assert emitted.index("/ComponentTypes/Src/pOut") < emitted.index("/ComponentTypes/Dst/rIn")
        assert emitted.index("<PROVIDER-IREF>") < emitted.index("<REQUESTER-IREF>")

    def test_a_description_is_written_as_its_desc(self):
        # Written exactly, after the SHORT-NAME.
        emitted = self._emit_one(
            Connector(
                name="link",
                **{"from": ConnectorEndpoint(swc="Src", port="pOut")},
                to=ConnectorEndpoint(swc="Dst", port="rIn"),
                description="a connector description",
            )
        )
        assert '<DESC>\n    <L-2 L="FOR-ALL">a connector description</L-2>\n  </DESC>' in emitted

    def test_an_endpoint_whose_type_is_not_a_member_is_refused(self):
        """A member for the endpoint's type has no answer, and the emitter says so rather than inventing one."""
        with pytest.raises(ValueError, match="not a member of composition 'Owner'"):
            self._emit_one(
                Connector(
                    name="link",
                    **{"from": ConnectorEndpoint(swc="Absent", port="pOut")},
                    to=ConnectorEndpoint(swc="Dst", port="rIn"),
                )
            )


class TestConnectorConsistencyGate:
    """The check runs **before** any composition is built, and still raises."""

    @staticmethod
    def _inconsistent_model(tmp_path) -> str:
        """A copy of the composite golden's model whose one connector is reversed, so ``from`` names a require port."""
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["composite-4.2.2"], model_root)
        path = next((model_root / "connectors").glob("*.yaml"))
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["from"], data["to"] = data["to"], data["from"]
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
        return str(model_root)

    @staticmethod
    def _emit(document: ArxmlDocument, model_root: str):
        return _emit_compositions(
            document.component_types,
            read_compositions(model_root),
            read_swc_model(model_root),
            read_connectors(model_root),
            {iface.name: iface for iface in read_interfaces(model_root)},
            model_root,
            _model_types(model_root),
        )

    def test_an_inconsistent_model_raises(self, tmp_path):
        model_root = self._inconsistent_model(tmp_path)
        with pytest.raises(ValueError, match="Connector consistency check failed"):
            self._emit(ArxmlDocument(), model_root)

    def test_the_message_is_the_one_build_workspace_raises(self, tmp_path):
        # The guard is shared rather than restated, so the two cannot drift —
        # this asserts they really are one copy for one model.
        model_root = self._inconsistent_model(tmp_path)
        with pytest.raises(ValueError) as emitted:
            _check_connectors(model_root)
        with pytest.raises(ValueError) as via_workspace:
            build_workspace(model_root)
        assert str(emitted.value) == str(via_workspace.value)

    def test_nothing_is_emitted_when_the_check_fails(self, tmp_path):
        # The check runs before the first element is created, so a caller that
        # catches the ValueError cannot serialize a half-built composition.
        model_root = self._inconsistent_model(tmp_path)
        document = ArxmlDocument()
        with pytest.raises(ValueError):
            self._emit(document, model_root)
        assert not document.component_types.has_elements

    def test_the_check_runs_before_the_composition_is_built(self, tmp_path, monkeypatch):
        # Ordering, not just presence: a writer that emitted first and validated
        # afterwards would pass every other test in this class.
        model_root = str(GOLDEN_MODEL_ROOTS["composite-4.2.2"])
        document = ArxmlDocument()
        import swcraft_tools.validator as validator

        real = validator.check_connector_consistency
        reached: list[str] = []

        def spy(root: str):
            assert not document.component_types.has_elements
            reached.append(root)
            return real(root)

        monkeypatch.setattr(validator, "check_connector_consistency", spy)
        assert self._emit(document, model_root)
        assert document.component_types.has_elements
        # The spy was reached: a by-name import in the writer would bind the original and skip it.
        assert reached == [model_root]


class TestTheGateIsTheLayerNotTheConnectorList:
    """The composition exists because the model declares one; only the consistency check is gated on connectors."""

    @staticmethod
    def _model_without_connectors(tmp_path) -> str:
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["composite-4.2.2"], model_root)
        for connector in (model_root / "connectors").glob("*.yaml"):
            connector.unlink()
        return str(model_root)

    def test_a_declared_composition_is_emitted_with_no_connector_at_all(self, tmp_path):
        model_root = self._model_without_connectors(tmp_path)
        document, emitted = _emit_whole_package(model_root)
        assert f"<SHORT-NAME>{ROOT_NAME}</SHORT-NAME>" in emitted
        assert "<COMPONENTS>" in emitted
        assert "<CONNECTORS>" not in emitted
        assert "COMPOSITION-SW-COMPONENT-TYPE" in document.serialize()

    def test_the_consistency_check_does_not_run(self, tmp_path, monkeypatch):
        # Invisible in the output: with no connectors the check would always pass anyway, so only
        # a spy can tell whether it ran.
        model_root = self._model_without_connectors(tmp_path)
        import swcraft_tools.validator as validator

        def refuse(root: str):
            raise AssertionError(f"check_connector_consistency must not run for a connector-less model: {root}")

        monkeypatch.setattr(validator, "check_connector_consistency", refuse)
        assert _emit_whole_package(model_root)[1]

    def test_the_consistency_check_runs_once_there_is_a_connector(self, monkeypatch):
        # The refusal above's positive twin, through the same patch: with connectors the
        # writer asks the patched name, so the refusal cannot pass because the patch is never seen.
        model_root = str(GOLDEN_MODEL_ROOTS["composite-4.2.2"])
        import swcraft_tools.validator as validator

        real = validator.check_connector_consistency
        reached: list[str] = []

        def record(root: str):
            reached.append(root)
            return real(root)

        monkeypatch.setattr(validator, "check_connector_consistency", record)
        assert _emit_whole_package(model_root)[1]
        assert reached == [model_root]

    def test_an_empty_model_emits_no_composition(self, tmp_path):
        model_root = str(make_empty_model_root(tmp_path))
        document = ArxmlDocument()
        # `{}` for both registries, spelled rather than defaulted: this model
        # really declares no interface and no data type.
        assert _emit_compositions(document.component_types, [], [], [], {}, model_root, {}) == []
        assert document.serialize() == read_golden("empty-4.2.2")

    def test_a_connector_with_no_composition_to_hold_it_is_refused(self, tmp_path):
        # Connectors with nowhere to be drawn: silently dropping them would lose a declared
        # element from the output.
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["composite-4.2.2"], model_root)
        for composition in (model_root / "compositions").glob("*.yaml"):
            composition.unlink()
        with pytest.raises(ValueError, match="declares no composition to draw them inside"):
            build_workspace(str(model_root))
