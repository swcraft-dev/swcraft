"""The ARXML goldens -- the byte oracle the writer is held to.

Three committed goldens under ``fixtures/arxml_golden/``:

* ``basic-4.2.2.arxml`` — ``examples/basic/model``, the ``dev.py build`` reference model;
* ``composite-4.2.2.arxml`` — ``fixtures/component_exchange/model`` in place, a strict tag superset of
  basic: six data type kinds (``base_type``, ``value_type``, ``application_type``, ``enum``, ``record``,
  ``array``), both interface kinds, an init event;
* ``empty-4.2.2.arxml`` — an empty model tree, for the degenerate shapes neither populated golden reaches
  (an empty AR-PACKAGE emits **no** ``ELEMENTS`` wrapper).

``TestSemanticRoundTrip`` is the second oracle: byte equality proves the *file* is unchanged, the round
trip that the **model** survives it.

The goldens are **never hand-edited** (``fixtures/arxml_golden/README.md`` holds the regeneration
procedure). A failed golden is a defect in the change, never an approved one, unless the change's
``Qualification Impact`` section authorizes an ARXML semantic change in writing; until it does, the
goldens are not regenerated.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest
import yaml
from conftest import (
    ACCESS_POINTS_MODEL_ROOT,
    COMPOSITIONS_MODEL_ROOT,
    GOLDEN_DIR,
    GOLDEN_MODEL_ROOTS,
    INIT_VALUES_MODEL_ROOT,
    REPO_ROOT,
    _root_composition,
    golden_slice,
    make_empty_model_root,
    read_golden,
)
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_import.connectors import map_compositions
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.arxml_validation import (
    MODE_STRUCTURAL,
    check_arxml,
)
from swcraft_tools.component_exchange.divergence import (
    projected_data_type,
    projected_interface,
    projected_swc,
)
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_data_types,
    read_interfaces,
    read_layer,
    read_swc_model,
)
from swcraft_tools.writer import generate_arxml
from xsd_support import forced_structural_validation

GOLDEN_NAMES = ["basic-4.2.2", "composite-4.2.2", "empty-4.2.2"]

AR_NS = "http://autosar.org/schema/r4.0"


def _tags(name: str) -> set[str]:
    """Every distinct element tag a golden carries, namespace stripped."""
    return {element.tag.split("}")[-1] for element in ET.fromstring(read_golden(name)).iter()}


# The goldens must actually be committed


class TestGoldensAreNotGitIgnored:
    """``.gitignore`` ignores ``*.arxml``; without a negation for the goldens ``git add`` skips them silently."""

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_golden_path_is_not_ignored(self, name: str) -> None:
        path = (GOLDEN_DIR / f"{name}.arxml").relative_to(REPO_ROOT)
        # --no-index: answer from the ignore rules alone. Without it a *tracked*
        # golden reports "not ignored" simply for being tracked, and this test
        # would pass even after the negation was deleted.
        #
        # Deliberately no --verbose: with it, `check-ignore` exits 0 whenever any
        # pattern matched -- including the *negation* -- so the exit code stops
        # being the verdict. Plain form: exit 0 = ignored, exit 1 = not ignored.
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode == 0:
            explained = subprocess.run(
                ["git", "check-ignore", "--no-index", "--verbose", str(path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
            pytest.fail(
                f"{path} is git-ignored ({explained}) — add a negation to .gitignore, "
                "or the golden is silently never committed."
            )

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_golden_is_tracked_by_git(self, name: str) -> None:
        """Not ignored is not the same as committed — check the index too."""
        path = (GOLDEN_DIR / f"{name}.arxml").relative_to(REPO_ROOT)
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, f"{path} is not tracked by git: {completed.stderr.strip()}"


# Byte equality: the goldens are what the writer produces today


class TestGoldensMatchGeneratedOutput:
    @pytest.mark.parametrize("name", sorted(GOLDEN_MODEL_ROOTS))
    def test_populated_golden_is_byte_identical(self, name: str, tmp_path) -> None:
        output = tmp_path / f"{name}.arxml"
        generate_arxml(str(GOLDEN_MODEL_ROOTS[name]), str(output))
        assert output.read_text(encoding="utf-8") == read_golden(name), (
            f"{name}.arxml no longer matches generate_arxml() over {GOLDEN_MODEL_ROOTS[name]}. "
            "If the difference is intentional, regenerate the goldens and record the "
            "difference in the phase plan before closing the task."
        )

    def test_empty_model_golden_is_byte_identical(self, tmp_path) -> None:
        model_root = make_empty_model_root(tmp_path)
        output = tmp_path / "empty.arxml"
        generate_arxml(str(model_root), str(output))
        assert output.read_text(encoding="utf-8") == read_golden("empty-4.2.2")

    @pytest.mark.parametrize("name", sorted(GOLDEN_MODEL_ROOTS))
    def test_generation_is_deterministic(self, name: str, tmp_path) -> None:
        """Two generations of the same model are byte-identical."""
        first = tmp_path / "first.arxml"
        second = tmp_path / "second.arxml"
        generate_arxml(str(GOLDEN_MODEL_ROOTS[name]), str(first))
        generate_arxml(str(GOLDEN_MODEL_ROOTS[name]), str(second))
        assert first.read_bytes() == second.read_bytes()


# Properties of the golden output the writer must keep


class TestGoldenShape:
    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_declares_the_4_2_2_schema_and_the_r4_0_namespace(self, name: str) -> None:
        lines = read_golden(name).splitlines()
        assert lines[0] == '<?xml version="1.0" encoding="utf-8"?>'
        assert lines[1] == (
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
        )

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_no_element_is_self_closed(self, name: str) -> None:
        """No element is written ``<X/>``, which ``lxml`` does by default."""
        assert "/>" not in read_golden(name)

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_package_order_is_datatypes_portinterfaces_componenttypes(self, name: str) -> None:
        """Load-bearing: references point backwards through this order."""
        root = ET.fromstring(read_golden(name))
        packages = root.findall(f"{{{AR_NS}}}AR-PACKAGES/{{{AR_NS}}}AR-PACKAGE")
        names = [p.findtext(f"{{{AR_NS}}}SHORT-NAME") for p in packages]
        assert names == ["DataTypes", "PortInterfaces", "ComponentTypes"]

    def test_composite_golden_is_a_strict_tag_superset_of_basic(self) -> None:
        """Basic's tags are a strict subset of composite's; the counts move whenever a golden gains or loses a tag."""

        basic, composite = _tags("basic-4.2.2"), _tags("composite-4.2.2")
        assert len(basic) == 74
        assert len(composite) == 111
        assert basic < composite

    def test_the_twelve_tags_the_physics_added_are_named(self) -> None:
        """The tags only an application type with ``unit``, ``conversion`` or ``range`` brings.

        ``COMPU-METHOD``, ``COMPU-SCALE`` and their wrappers are not among them: the ``TEXTTABLE`` enum brings those.
        """
        added = {
            "COMPU-DENOMINATOR",
            "COMPU-NUMERATOR",
            "COMPU-RATIONAL-COEFFS",
            "DATA-CONSTR",
            "DATA-CONSTR-REF",
            "DATA-CONSTR-RULE",
            "DATA-CONSTR-RULES",
            "DISPLAY-NAME",
            "PHYS-CONSTRS",
            "UNIT",
            "UNIT-REF",
            "V",
        }
        assert len(added) == 12
        assert added <= _tags("composite-4.2.2")
        assert added.isdisjoint(_tags("basic-4.2.2"))

    def test_empty_package_emits_no_elements_wrapper(self) -> None:
        """An empty AR-PACKAGE has no ELEMENTS."""
        assert "<ELEMENTS>" not in read_golden("empty-4.2.2")
        root = ET.fromstring(read_golden("empty-4.2.2"))
        for package in root.iter(f"{{{AR_NS}}}AR-PACKAGE"):
            assert list(package) == [package.find(f"{{{AR_NS}}}SHORT-NAME")]

    def test_a_model_with_swcs_but_no_connectors_still_emits_its_declared_composition(self, tmp_path) -> None:
        """Components and no connector: the composition comes without ``CONNECTORS``; no populated golden reaches it."""
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["composite-4.2.2"], model_root)
        for connector in (model_root / "connectors").glob("*.yaml"):
            connector.unlink()

        output = tmp_path / "no-connectors.arxml"
        generate_arxml(str(model_root), str(output))
        text = output.read_text(encoding="utf-8")
        assert "COMPOSITION-SW-COMPONENT-TYPE" in text
        assert "APPLICATION-SW-COMPONENT-TYPE" in text
        assert "<SW-COMPONENT-PROTOTYPE>" in text
        assert "<CONNECTORS>" not in text

    def test_ports_are_emitted_provide_first_in_declaration_order(self) -> None:
        """``component_exchange/divergence.py`` projects on this order."""
        swc = golden_slice(read_golden("composite-4.2.2"), "SensorSwc", tag="APPLICATION-SW-COMPONENT-TYPE")
        ports = [line.strip() for line in swc.splitlines() if line.strip().startswith("<SHORT-NAME>p")]
        ports += [line.strip() for line in swc.splitlines() if line.strip().startswith("<SHORT-NAME>r")]
        # Declared order in the fixture is pSpeed, pPose, rCalib — so this is
        # provide-first with declaration order preserved inside each group, NOT
        # an alphabetical sort (which would put pPose first).
        assert ports == [
            "<SHORT-NAME>pSpeed</SHORT-NAME>",
            "<SHORT-NAME>pPose</SHORT-NAME>",
            "<SHORT-NAME>rCalib</SHORT-NAME>",
        ]

    def test_no_golden_holds_a_port_api_option(self) -> None:
        """The model states no port API option, so the writer writes none."""
        for golden in ("basic-4.2.2", "composite-4.2.2", "empty-4.2.2"):
            assert "PORT-API-OPTION" not in read_golden(golden)


class TestGoldenValidation:
    """The **structural** half of validation, forced, so it means one thing on every machine.

    XSD conformance is ``test_arxml_xsd_conformance.py``'s, run by ``python scripts/dev.py test-xsd``.
    """

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_golden_passes_structural_validation(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        forced_structural_validation(monkeypatch)
        outcome = check_arxml(str(GOLDEN_DIR / f"{name}.arxml"))
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.valid is True


class TestSemanticRoundTrip:
    """Generate -> ``read_arxml()`` -> map -> **Pydantic equality** with the source model.

    Compared on the ARXML-representable projection (``component_exchange/divergence.py``'s ``projected_*`` /
    ``_projected_*``): the file cannot carry a value type's ``unit``, authored port order or a component's
    ``memory`` budget.
    """

    @staticmethod
    def _round_trip(arxml_path: str):
        """Read *arxml_path* back through ``import_component()``'s calls, plus the whole-model composition mapping."""
        model = read_arxml(arxml_path)
        forms: list = []
        data_types, registry, dt_skips = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        imported_swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={}
        )
        mapped, composition_skips = map_compositions(
            model, imported_swcs, interfaces, iface_skips, registry, swc_skip_log=swc_skips
        )
        skips = [
            entry for log in (model.skip_log, dt_skips, iface_skips, swc_skips, composition_skips) for entry in log
        ]
        # The external stubs are components too, and a round trip of
        # SWCraft's own output has none: every type it emits is in the file.
        return (
            [s.swc for s in (*imported_swcs, *mapped.stubs)],
            interfaces,
            data_types,
            mapped.connectors,
            skips,
            mapped,
        )

    @pytest.fixture(params=sorted(GOLDEN_MODEL_ROOTS))
    def round_tripped(self, request, tmp_path):
        model_root = str(GOLDEN_MODEL_ROOTS[request.param])
        output = tmp_path / "model.arxml"
        generate_arxml(model_root, str(output))
        return model_root, self._round_trip(str(output))

    def test_nothing_is_skipped_on_the_way_back_in(self, round_tripped) -> None:
        """Necessary but not sufficient — the layer comparisons below are the real bar."""
        _, (_, _, _, _, skips, _) = round_tripped
        assert [f"{e.tag} at {e.path}: {e.reason}" for e in skips] == []

    def test_data_types_come_back_pydantic_equal(self, round_tripped) -> None:
        model_root, (_, _, data_types, _, _, _) = round_tripped
        # The importer answers the three data description layers in one list; the
        # model keeps base types and application types in layers of their own, so
        # the source is the three.
        source = {
            dt.name: projected_data_type(dt)
            for dt in (
                *read_layer(model_root, "base_types"),
                *read_data_types(model_root),
                *read_layer(model_root, "application_types"),
            )
        }
        assert {dt.name: projected_data_type(dt) for dt in data_types} == source

    def test_interfaces_come_back_pydantic_equal(self, round_tripped) -> None:
        model_root, (_, interfaces, _, _, _, _) = round_tripped
        source = {i.name: projected_interface(i) for i in read_interfaces(model_root)}
        assert {i.name: projected_interface(i) for i in interfaces} == source

    def test_swcs_come_back_pydantic_equal(self, round_tripped) -> None:
        model_root, (swcs, _, _, _, _, _) = round_tripped
        source = {s.name: projected_swc(s) for s in read_swc_model(model_root)}
        assert {s.name: projected_swc(s) for s in swcs} == source

    def test_connectors_come_back_with_the_same_names_and_endpoints(self, round_tripped) -> None:
        """Connectors come back with the same names **and** endpoints.

        The importer reads the ``ASSEMBLY-SW-CONNECTOR``'s own ``SHORT-NAME``, so the round trip pins the authored name
        too.
        """
        model_root, (_, _, _, connectors, _, _) = round_tripped
        observed = {(c.name, c.from_.swc, c.from_.port, c.to.swc, c.to.port) for c in connectors}
        assert observed == {
            (c.name, c.from_.swc, c.from_.port, c.to.swc, c.to.port) for c in read_connectors(model_root)
        }

    def test_the_composition_hierarchy_comes_back_element_for_element(self, round_tripped) -> None:
        """The declared hierarchy comes back: compositions, boundary ports, members with their type field, delegations.

        ``title`` is not compared: it is not an ARXML concept and the importer sets it to the ``SHORT-NAME``.
        """
        model_root, (_, _, _, _, _, mapped) = round_tripped
        source = {
            c.name: (
                [(p.name, p.direction, p.interface) for p in c.ports],
                [(m.name, m.type, m.composition) for m in c.members],
                [(d.name, d.port, d.member, d.member_port) for d in c.delegations],
                c.root,
            )
            for c in read_compositions(model_root)
        }
        observed = {
            c.name: (
                [(p.name, p.direction, p.interface) for p in c.ports],
                [(m.name, m.type, m.composition) for m in c.members],
                [(d.name, d.port, d.member, d.member_port) for d in c.delegations],
                c.root,
            )
            for c in mapped.compositions
        }
        assert observed == source

    def test_every_connector_comes_back_inside_the_composition_it_was_written_in(self, round_tripped) -> None:
        """A connector's ``None`` composition means the root, so it is compared against the root's own name."""
        model_root, (_, _, _, connectors, _, mapped) = round_tripped
        root = next(c.name for c in read_compositions(model_root) if c.root)
        source = {c.name: (c.composition or root) for c in read_connectors(model_root)}
        assert {c.name: c.composition for c in connectors} == source


#: The two deliberately lossy rows, as the values they come back as. ``flag``: the writer spells the ``bool``
#: ``1`` and the reader does not resolve the base type. ``mode``: the writer spells the literal's value and
#: the mapper hands back its name, so the row is lossy in the file and lossless in the model.
LOSSY_INIT_ROWS = {"flag": 1}


def _init_leaves(value, path: tuple = ()):
    """Every scalar leaf of an ``init`` value with the path that reaches it, walked by the value's own shape."""
    if isinstance(value, list):
        for index, member in enumerate(value):
            yield from _init_leaves(member, (*path, index))
    elif isinstance(value, dict):
        for key, member in value.items():
            yield from _init_leaves(member, (*path, key))
    else:
        yield path, value


def _typed_leaves(value) -> dict:
    """``{path: (value, type name)}`` — the comparison this class is for."""
    return {path: (leaf, type(leaf).__name__) for path, leaf in _init_leaves(value)}


@pytest.fixture(scope="module")
def round_tripped(tmp_path_factory):
    """``fixtures/init_values/model`` generated once and read back through :meth:`TestSemanticRoundTrip._round_trip`."""
    output = tmp_path_factory.mktemp("init-values-round-trip") / "model.arxml"
    generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output))
    return TestSemanticRoundTrip._round_trip(str(output))


@pytest.fixture(scope="module")
def source_inits() -> dict:
    """``{data element: init}`` as the YAML declares it — read, never projected."""
    interface = next(i for i in read_interfaces(str(INIT_VALUES_MODEL_ROOT)) if i.name == "ISignals")
    return {element.name: element.init for element in interface.data_elements}


@pytest.fixture(scope="module")
def returned_inits(round_tripped) -> dict:
    """The same mapping, as the round trip hands it back — also unprojected."""
    _, interfaces, _, _, _, _ = round_tripped
    interface = next(i for i in interfaces if i.name == "ISignals")
    return {element.name: element.init for element in interface.data_elements}


class TestTheUnprojectedInitSurvivesTheRoundTrip:
    """The ``init`` round trip over ``fixtures/init_values/model``: **typed** and **unprojected**.

    ``TestSemanticRoundTrip`` compares on ``projected_interface``, which blanks ``init`` on both sides, so it
    cannot see whether a value survived. Leaves are compared by ``==`` *and* by ``type``: ``100.0`` emitted as
    ``100`` reads back as an equal ``int``. The two lossy rows are asserted as their outcomes, never exempted.
    """

    def test_nothing_is_skipped_on_the_way_back_in(self, round_tripped) -> None:
        """The skip list is **empty**, which depends on the fixture's shape as much as on the writer.

        The fixture's depth-3 element is an array of records of records; no record there has an array-typed
        field. Record-in-array-in-record is held in ``test_arxml_init_writer.py`` and
        ``test_arxml_xsd_conformance.py``.
        """
        _, _, _, _, skips, _ = round_tripped
        assert [f"{e.tag} at {e.path}: {e.reason}" for e in skips] == []

    def test_the_interface_comes_back_whole(self, round_tripped, source_inits, returned_inits) -> None:
        """The interface comes back whole, or every ``init`` assertion below is vacuous."""
        _, interfaces, _, _, _, _ = round_tripped

        assert [i.name for i in interfaces] == ["ISignals"]
        assert list(returned_inits) == list(source_inits)

    def test_every_unprojected_init_comes_back_equal(self, source_inits, returned_inits) -> None:
        expected = {name: LOSSY_INIT_ROWS.get(name, value) for name, value in source_inits.items()}

        assert returned_inits == expected

    def test_every_leaf_of_every_init_comes_back_with_the_same_type(self, source_inits, returned_inits) -> None:
        """Every leaf comes back with its type at every depth; ``bool``, the lossy row, is excluded by name."""
        expected = {name: _typed_leaves(LOSSY_INIT_ROWS.get(name, value)) for name, value in source_inits.items()}

        assert {name: _typed_leaves(value) for name, value in returned_inits.items()} == expected

    @pytest.mark.parametrize(
        ("element", "leaves"),
        [
            ("huge", {(): (1e20, "float")}),
            ("negative", {(): (-1e20, "float")}),
            ("tiny", {(): (1e-7, "float")}),
            ("whole", {(): (100.0, "float")}),
            (
                "floats",
                {(0,): (1e20, "float"), (1,): (-1e20, "float"), (2,): (1e-7, "float"), (3,): (100.0, "float")},
            ),
            (
                "limits",
                {
                    ("low",): (-1e20, "float"),
                    ("high",): (1e20, "float"),
                    ("tiny",): (1e-7, "float"),
                    ("whole",): (100.0, "float"),
                },
            ),
        ],
    )
    def test_the_four_float_leaves_are_really_in_all_three_positions(
        self, element: str, leaves: dict, source_inits, returned_inits
    ) -> None:
        """Non-vacuity: the four hard floats are in all three positions.

        ``1e20`` and ``100.0`` are the two ``format_init_number`` appends ``.0`` for.
        """
        assert _typed_leaves(source_inits[element]) == leaves
        assert _typed_leaves(returned_inits[element]) == leaves

    def test_the_depth_three_leaves_come_back_typed(self, source_inits, returned_inits) -> None:
        """The deepest nesting, an array of records of records, comes back typed."""
        expected = {
            (0, "row", "index"): (1, "int"),
            (0, "row", "mode"): ("OFF", "str"),
            (1, "row", "index"): (2, "int"),
            (1, "row", "mode"): ("ON", "str"),
        }

        assert _typed_leaves(source_inits["deep"]) == expected
        assert _typed_leaves(returned_inits["deep"]) == expected

    def test_the_bool_comes_back_as_the_int_one(self, source_inits, returned_inits) -> None:
        """The lossy ``bool`` row comes back as the ``int`` ``1``; ``True == 1``, so only the type sees it."""
        assert source_inits["flag"] is True
        assert returned_inits["flag"] == 1
        assert type(returned_inits["flag"]) is int

    def test_the_enum_comes_back_as_the_literal_name(self, source_inits, returned_inits) -> None:
        """The lossy enum row comes back as the literal's **name**, which is what the model declares.

        A mapper handing back the number would still satisfy ``==`` against a model whose ``init`` was the number.
        """
        assert source_inits["mode"] == "ON"
        assert returned_inits["mode"] == "ON"
        assert type(returned_inits["mode"]) is str

    def test_the_element_without_an_init_comes_back_without_one(self, source_inits, returned_inits) -> None:
        """Non-vacuity: a writer inventing a value for every element would satisfy every comparison above."""
        assert source_inits["plain"] is None
        assert returned_inits["plain"] is None

    def test_the_projection_this_class_exists_for_really_blanks_init(self, round_tripped) -> None:
        """``projected_interface`` blanks ``init`` on both sides, which is why this class exists."""
        _, interfaces, _, _, _, _ = round_tripped
        interface = next(i for i in interfaces if i.name == "ISignals")

        projected = projected_interface(interface)

        assert [element.init for element in projected.data_elements] == [None] * len(projected.data_elements)
        assert any(element.init is not None for element in interface.data_elements)


# The unprojected access lists over ``fixtures/access_points/model``


def _access_entries(runnable) -> set[tuple[str, str, str, str | None]]:
    """``{(list, port, member, by)}`` for one runnable.

    A set, as neither side promises an order; ``by`` is ``None`` for a by-argument read, a write and a call.
    """
    return {
        *(("reads", entry.port, entry.element, entry.by) for entry in runnable.reads),
        *(("writes", entry.port, entry.element, None) for entry in runnable.writes),
        *(("calls", entry.port, entry.operation, None) for entry in runnable.calls),
    }


def _access_sets(swcs) -> dict[tuple[str, str], set[tuple[str, str, str, str | None]]]:
    """``{(swc, runnable): entry set}`` over a whole model — source or round-tripped."""
    return {(swc.name, runnable.name): _access_entries(runnable) for swc in swcs for runnable in swc.runnables}


@pytest.fixture(scope="module")
def access_round_tripped(tmp_path_factory):
    """``fixtures/access_points/model`` generated once, read back through :meth:`TestSemanticRoundTrip._round_trip`."""
    output = tmp_path_factory.mktemp("access-points-round-trip") / "model.arxml"
    generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output))
    return TestSemanticRoundTrip._round_trip(str(output))


@pytest.fixture(scope="module")
def source_access_sets() -> dict[tuple[str, str], set[tuple[str, str, str, str | None]]]:
    """The three lists as the YAML declares them — read, never projected."""
    return _access_sets(read_swc_model(str(ACCESS_POINTS_MODEL_ROOT)))


@pytest.fixture(scope="module")
def returned_access_sets(access_round_tripped) -> dict[tuple[str, str], set[tuple[str, str, str, str | None]]]:
    """The same mapping, as the round trip hands it back — also unprojected."""
    swcs, _, _, _, _, _ = access_round_tripped
    return _access_sets(swcs)


class TestTheUnprojectedAccessListsSurviveTheRoundTrip:
    """The access-list round trip over ``fixtures/access_points/model``: **unprojected**.

    ``TestSemanticRoundTrip`` compares on ``projected_swc``, which blanks ``reads``/``writes``/``calls`` on
    both sides; this class reads them with no ``model_copy`` in between, so it does not depend on the
    exchange code.
    """

    def test_nothing_is_skipped_on_the_way_back_in(self, access_round_tripped) -> None:
        """The skip list is **empty**, a measurement of the *reader* as much as of the writer.

        A reader whose ``ADMIN-DATA`` skip did not recognize SWCraft's own access-marker ``SDG`` would report
        each one and abort ``import component`` of a marked SWCraft export.
        """
        _, _, _, _, skips, _ = access_round_tripped
        assert [f"{e.tag} at {e.path}: {e.reason}" for e in skips] == []

    def test_the_components_come_back_whole(self, access_round_tripped, source_access_sets) -> None:
        """The components and runnables come back whole, or every set comparison below is vacuous."""
        swcs, _, _, _, _, _ = access_round_tripped

        assert sorted(s.name for s in swcs) == ["AccessSwc", "ServerSwc"]
        assert sorted(_access_sets(swcs)) == sorted(source_access_sets)

    def test_every_runnables_entry_set_comes_back_equal(self, source_access_sets, returned_access_sets) -> None:
        """Per runnable, ``{(list, port, member, by)}`` read back equals the source's **as a set**."""
        assert returned_access_sets == source_access_sets

    def test_the_by_value_read_is_really_in_the_source_set(self, source_access_sets) -> None:
        """Non-vacuity: a by-value read is in the source.

        ``by`` changes the RTE API, and a writer flattening it to by-argument would otherwise pass.
        """
        assert source_access_sets[("AccessSwc", "AccessMain")] == {
            ("reads", "rSignals", "speed", None),
            ("reads", "rSignals", "level", "value"),
            ("writes", "pSignals", "speed", None),
            ("calls", "rControl", "Apply", None),
        }

    def test_the_runnable_that_declares_nothing_comes_back_declaring_nothing(self, returned_access_sets) -> None:
        """Non-vacuity: a runnable declaring nothing, in a declaring and in a silent component, comes back empty."""
        assert returned_access_sets[("AccessSwc", "QuietRun")] == set()
        assert returned_access_sets[("ServerSwc", "ServerMain")] == set()

    def test_the_untouched_port_produces_no_entry(self, access_round_tripped) -> None:
        """The silence rule: ``rSpare``, a port no runnable touches, comes back as a port and no access point."""
        swcs, _, _, _, _, _ = access_round_tripped
        access_swc = next(s for s in swcs if s.name == "AccessSwc")

        assert "rSpare" in [port.name for port in access_swc.ports]
        assert not any("rSpare" in entry for runnable in access_swc.runnables for entry in _access_entries(runnable))

    def test_the_projection_this_class_exists_for_really_blanks_the_lists(self, access_round_tripped) -> None:
        """``projected_swc`` blanks the three lists on both sides, which is why this class exists."""
        swcs, _, _, _, _, _ = access_round_tripped
        access_swc = next(s for s in swcs if s.name == "AccessSwc")

        projected = projected_swc(access_swc)

        assert all(not (r.reads or r.writes or r.calls) for r in projected.runnables)
        assert any(r.reads or r.writes or r.calls for r in access_swc.runnables)


class TestShapesNoGoldenReaches:
    """Emitted shapes between "populated" and "empty", tested directly rather than by byte equality.

    ``SERVER-COM-SPEC``/``QUEUE-LENGTH`` are in neither golden, as no fixture provides a client-server
    interface; this class and ``test_arxml_components_writer.py`` hold them.
    """

    @staticmethod
    def _model_with_one_swc(tmp_path, swc: dict) -> str:
        """examples/basic's data types and interfaces with one SWC, no connectors, and the root re-pointed at it."""
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
        for existing in (model_root / "connectors").glob("*.yaml"):
            existing.unlink()
        for existing in (model_root / "swc").rglob("*.yaml"):
            existing.unlink()
        target = model_root / "swc" / "group" / f"{swc['name']}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(swc, default_flow_style=False, sort_keys=False), encoding="utf-8")
        _root_composition(model_root, [swc["name"]])
        return str(model_root)

    def _generate(self, tmp_path, swc: dict) -> str:
        model_root = self._model_with_one_swc(tmp_path, swc)
        output = tmp_path / "out.arxml"
        generate_arxml(model_root, str(output))
        return output.read_text(encoding="utf-8")

    def test_a_provided_client_server_port_emits_one_server_com_spec_per_operation(self, tmp_path) -> None:
        text = self._generate(
            tmp_path,
            {
                "name": "DiagServer",
                "type": "application",
                "ports": [{"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"}],
                "runnables": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        port = golden_slice(text, "pDiag", tag="P-PORT-PROTOTYPE")
        assert port == (
            "<P-PORT-PROTOTYPE>\n"
            "  <SHORT-NAME>pDiag</SHORT-NAME>\n"
            "  <PROVIDED-COM-SPECS>\n"
            "    <SERVER-COM-SPEC>\n"
            '      <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
            "/PortInterfaces/IDiagnosticService/ReadDtc</OPERATION-REF>\n"
            "      <QUEUE-LENGTH>1</QUEUE-LENGTH>\n"
            "    </SERVER-COM-SPEC>\n"
            "    <SERVER-COM-SPEC>\n"
            '      <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
            "/PortInterfaces/IDiagnosticService/ClearDtc</OPERATION-REF>\n"
            "      <QUEUE-LENGTH>1</QUEUE-LENGTH>\n"
            "    </SERVER-COM-SPEC>\n"
            "  </PROVIDED-COM-SPECS>\n"
            '  <PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">'
            "/PortInterfaces/IDiagnosticService</PROVIDED-INTERFACE-TREF>\n"
            "</P-PORT-PROTOTYPE>"
        )

    def test_a_required_client_server_port_emits_one_client_com_spec_per_operation(self, tmp_path) -> None:
        """The composite golden pins CLIENT-COM-SPEC's shape but not its cardinality."""
        text = self._generate(
            tmp_path,
            {
                "name": "DiagClient",
                "type": "application",
                "ports": [{"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}],
                "runnables": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        port = golden_slice(text, "rDiag", tag="R-PORT-PROTOTYPE")
        operations = [line.strip() for line in port.splitlines() if "<OPERATION-REF " in line]
        assert port.count("<CLIENT-COM-SPEC>") == 2
        assert operations == [
            '<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/PortInterfaces/IDiagnosticService/ReadDtc</OPERATION-REF>',
            '<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/PortInterfaces/IDiagnosticService/ClearDtc</OPERATION-REF>',
        ]

    def test_a_bare_swc_keeps_an_empty_ports_element_and_omits_the_behavior_lists(self, tmp_path) -> None:
        """PORTS is emitted empty, EVENTS/RUNNABLES are omitted (and no PORT-API-OPTIONS is written at all).

        ``Bare`` declares ``no_support`` (a component without it is refused), so its behaviour
        carries that one line after the short name."""
        text = self._generate(
            tmp_path,
            {
                "name": "Bare",
                "type": "application",
                "ports": [],
                "runnables": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        swc = golden_slice(text, "Bare", tag="APPLICATION-SW-COMPONENT-TYPE")
        assert swc == (
            "<APPLICATION-SW-COMPONENT-TYPE>\n"
            "  <SHORT-NAME>Bare</SHORT-NAME>\n"
            "  <PORTS>\n"
            "  </PORTS>\n"
            "  <INTERNAL-BEHAVIORS>\n"
            "    <SWC-INTERNAL-BEHAVIOR>\n"
            "      <SHORT-NAME>Bare_InternalBehavior</SHORT-NAME>\n"
            "      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\n"
            "      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>\n"
            "    </SWC-INTERNAL-BEHAVIOR>\n"
            "  </INTERNAL-BEHAVIORS>\n"
            "</APPLICATION-SW-COMPONENT-TYPE>"
        )
        # SWC-IMPLEMENTATION is still emitted, interleaved right after the SWC.
        assert golden_slice(text, "Bare_Implementation") == (
            "<SWC-IMPLEMENTATION>\n"
            "  <SHORT-NAME>Bare_Implementation</SHORT-NAME>\n"
            '  <BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR">/ComponentTypes/Bare/Bare_InternalBehavior</BEHAVIOR-REF>\n'
            "</SWC-IMPLEMENTATION>"
        )


# The fragment-comparison mechanism the emitter tests depend on


class TestGoldenSlice:
    def test_slices_a_known_section_and_dedents_it(self) -> None:
        fragment = golden_slice(read_golden("composite-4.2.2"), "float32_base", tag="SW-BASE-TYPE")
        assert fragment == (
            "<SW-BASE-TYPE>\n"
            "  <SHORT-NAME>float32_base</SHORT-NAME>\n"
            "  <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
            "  <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>\n"
            "  <BASE-TYPE-ENCODING>IEEE754</BASE-TYPE-ENCODING>\n"
            "  <NATIVE-DECLARATION>float32</NATIVE-DECLARATION>\n"
            "</SW-BASE-TYPE>"
        )

    def test_tag_is_optional_when_the_short_name_is_unique(self) -> None:
        fragment = golden_slice(read_golden("composite-4.2.2"), "Gear_T_CompuMethod")
        assert fragment.startswith("<COMPU-METHOD>")
        assert fragment.endswith("</COMPU-METHOD>")

    def test_slices_a_deeply_nested_section(self) -> None:
        fragment = golden_slice(read_golden("composite-4.2.2"), "TMT_ActuatorMain")
        assert fragment.splitlines()[0] == "<TIMING-EVENT>"
        assert "  <PERIOD>0.02</PERIOD>" in fragment.splitlines()

    def test_a_sliced_section_is_well_formed_xml(self) -> None:
        fragment = golden_slice(read_golden("basic-4.2.2"), "IExample", tag="SENDER-RECEIVER-INTERFACE")
        assert ET.fromstring(fragment).tag == "SENDER-RECEIVER-INTERFACE"

    def test_unknown_short_name_raises(self) -> None:
        with pytest.raises(ValueError, match="No element with SHORT-NAME 'NoSuchThing'"):
            golden_slice(read_golden("basic-4.2.2"), "NoSuchThing")

    def test_known_short_name_with_the_wrong_tag_raises(self) -> None:
        with pytest.raises(ValueError, match="No element with SHORT-NAME 'IExample' and tag 'COMPU-METHOD'"):
            golden_slice(read_golden("basic-4.2.2"), "IExample", tag="COMPU-METHOD")

    def test_ambiguous_short_name_raises_and_names_the_candidates(self) -> None:
        # "SensorSwc" is both the APPLICATION-SW-COMPONENT-TYPE and its
        # SW-COMPONENT-PROTOTYPE inside the composition.
        with pytest.raises(ValueError, match="carried by 2 elements"):
            golden_slice(read_golden("composite-4.2.2"), "SensorSwc")

    def test_tag_disambiguates_an_ambiguous_short_name(self) -> None:
        fragment = golden_slice(read_golden("composite-4.2.2"), "SensorSwc", tag="SW-COMPONENT-PROTOTYPE")
        assert fragment == (
            "<SW-COMPONENT-PROTOTYPE>\n"
            "  <SHORT-NAME>SensorSwc</SHORT-NAME>\n"
            '  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/ComponentTypes/SensorSwc</TYPE-TREF>\n'
            "</SW-COMPONENT-PROTOTYPE>"
        )

    def test_unterminated_element_raises_rather_than_returning_a_truncated_slice(self) -> None:
        truncated = "\n".join(read_golden("basic-4.2.2").splitlines()[:10])
        with pytest.raises(ValueError, match="Unterminated"):
            golden_slice(truncated, "DataTypes")


# The semantic round trip over a root that HAS a hierarchy


class TestTheSemanticRoundTripOverARootWithRealCompositionStructure:
    """The semantic round trip over a root with boundary ports, delegations and nested levels.

    Both golden roots declare none of these, so ``TestSemanticRoundTrip`` compares empty lists there, and the
    golden trees are not changed to exercise something new. ``fixtures/compositions/model`` carries three
    compositions over two levels, a member named apart from its type, an external stub and both
    ``INNER-PORT-IREF`` variants; no golden is generated from it.

    Does not reach: a misunderstanding of AUTOSAR shared by SWCraft's writer and importer (the licensed-XSD
    gate and the closed round-trip suite are for that); the bytes; a shape this one root does not declare.
    """

    @staticmethod
    @pytest.fixture
    def round_tripped(tmp_path):
        output = tmp_path / "model.arxml"
        generate_arxml(str(COMPOSITIONS_MODEL_ROOT), str(output))
        return TestSemanticRoundTrip._round_trip(str(output))

    def test_the_fixture_really_carries_the_structure_this_class_exists_for(self) -> None:
        """The fixture carries the structure, so the comparisons below are not of two empty lists."""
        declared = {c.name: c for c in read_compositions(str(COMPOSITIONS_MODEL_ROOT))}
        assert any(c.ports for c in declared.values()), "no boundary port to round-trip"
        assert any(c.delegations for c in declared.values()), "no delegation to round-trip"
        assert any(m.composition for c in declared.values() for m in c.members), "the hierarchy is one level"
        directions = {
            port.direction
            for composition in declared.values()
            for delegation in composition.delegations
            for port in composition.ports
            if port.name == delegation.port
        }
        assert directions == {"provide", "require"}, f"only one INNER-PORT-IREF variant is exercised: {directions}"

    def test_nothing_is_skipped_on_the_way_back_in(self, round_tripped) -> None:
        _, _, _, _, skips, _ = round_tripped
        assert [f"{entry.tag} at {entry.path}: {entry.reason}" for entry in skips] == []

    def test_the_composition_hierarchy_comes_back_element_for_element(self, round_tripped) -> None:
        """The witness against a lost level and a mis-wired delegation; ``title`` is not compared, as above."""
        _, _, _, _, _, mapped = round_tripped
        # Every imported stub is `<leaf>_ext<X>`: the hand-written stub comes back under the
        # importer's name, X = 0 here.
        stub_names = {
            s.name: f"{s.external_ref.rsplit('/', 1)[-1]}_ext0"
            for s in read_swc_model(str(COMPOSITIONS_MODEL_ROOT))
            if s.external
        }

        def projected(compositions, renamed=None):
            renamed = renamed or {}
            return {
                c.name: (
                    [(p.name, p.direction, p.interface) for p in c.ports],
                    [(m.name, renamed.get(m.type, m.type), m.composition) for m in c.members],
                    [(d.name, d.port, d.member, d.member_port) for d in c.delegations],
                    c.root,
                )
                for c in compositions
            }

        assert projected(mapped.compositions) == projected(read_compositions(str(COMPOSITIONS_MODEL_ROOT)), stub_names)

    def test_the_external_stub_comes_back_with_its_own_reference_pair(self, round_tripped) -> None:
        """The stub's ``TYPE-TREF`` is copied, never derived."""
        swcs, _, _, _, _, _ = round_tripped
        # Read back under the importer's `<leaf>_ext<X>` name, X = 0 for the one stub.
        source = {
            f"{s.external_ref.rsplit('/', 1)[-1]}_ext0": (s.external_ref, s.external_dest)
            for s in read_swc_model(str(COMPOSITIONS_MODEL_ROOT))
            if s.external
        }
        assert source, "the fixture declares no external stub"
        assert {s.name: (s.external_ref, s.external_dest) for s in swcs if s.external} == source
