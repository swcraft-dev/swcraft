"""The self-check, the specimen sidecar and the fixed point.

A specimen is published only when the passport the pass computes over its rendered documents holds
every leaf the preset states; a miss is refused in generation at the leaf (class `missed`, exit 2)
with nothing written. The specimen sidecar is a document of its own kind, and the loop preset ->
specimen -> sidecar passport -> preset -> specimen is a fixed point of the tree.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from swcraft_specimens import __version__
from swcraft_specimens.cli import run
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import (
    Composition,
    CompositionPort,
    ExternalInterface,
    Port,
    Project,
    Unit,
)
from swcraft_specimens.passport import (
    MISSED,
    PassportRefused,
    Refusal,
    compute_passport,
    validate_passport,
)
from swcraft_specimens.sidecar import (
    SPECIMEN_SCHEMA_VERSION,
    project_tree_sha256,
    sidecar_kind,
    validate_sidecar,
    validate_specimen_sidecar,
)
from swcraft_specimens.specimen import build as build_module
from swcraft_specimens.specimen.build import (
    build_specimen_files,
    render_specimen,
    self_check,
)
from swcraft_specimens.specimen.compositions import build_compositions
from swcraft_specimens.specimen.delegations import build_delegations
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.interfaces import InterfacePlan, build_interfaces
from swcraft_specimens.specimen.layout import Layout, check_layout
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SPECIMEN_KIND, SpecimenPreset, preset_text
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.types import TypeLayers, build_types
from tests.oracle.specimen_helpers import LAYERS, neutral_passport, shaped_witness, write_json

#: The fifteen layers in the order of the pinned release's `swcraft list` (`metrics.LayerCounts`).
LIST_ORDER = [
    "requirements",
    "compositions",
    "swc",
    "interfaces",
    "application_types",
    "implementation_types",
    "base_types",
    "units",
    "compu_methods",
    "data_constraints",
    "mapping_sets",
    "connectors",
    "tasks",
    "sequences",
    "hardware",
]


def rich_passport() -> dict[str, Any]:
    """A partial passport the builder completes.

    Twelve components with one hub, three unconnected ports, two links, declared levels, three
    partitions with one rated and the component names' lengths stated; the connector count, the
    fan-out list, the port total and most identifier lengths are left to the construction.
    """
    passport = neutral_passport()
    for layer in ("connectors", "hardware"):
        del passport["layers"][layer]
    passport["layers"].update(swc=12, interfaces=6, implementation_types=3, base_types=2)
    passport["components"] = {
        "hubs": [9],
        "unconnected": {"total": 3, "max_on_one": 2},
        "by_declared_level": {"QM": 2, "A": 0, "B": 1, "C": 0, "D": 1},
        "runnables": 7,
    }
    passport["interfaces"] = {"sender_receiver": 4, "client_server": 2, "fan_out": {"max": 7}}
    passport["connectors"] = {"delegation": 0, "links": 2}
    passport["hardware"] = {"cores": 2, "partitions": 3, "memories": 1, "rated_partitions": 1}
    for layer in (
        "swc",
        "interfaces",
        "connectors",
        "implementation_types",
        "base_types",
        "hardware",
        "compositions",
    ):
        del passport["identifiers"][layer]
    passport["identifiers"]["swc"] = {"max_length": 20, "mean_length": 11.5}
    passport["identifiers"]["max_length"] = 24
    return validate_passport(passport)


PASSPORTS = {"neutral": neutral_passport, "rich": rich_passport}


def tree(directory: Path) -> dict[str, bytes]:
    """Every file under a directory, by its POSIX path relative to it."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"), key=lambda p: p.as_posix())
        if path.is_file()
    }


def project_files(directory: Path) -> dict[str, bytes]:
    """The generated tree without its sidecar."""
    return {path: data for path, data in tree(directory).items() if path != "generation.json"}


def sidecar(directory: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((directory / "generation.json").read_text(encoding="utf-8"))
    return document


def generate(tmp_path: Path, label: str, text: str) -> Path:
    preset = tmp_path / f"{label}.yaml"
    preset.write_text(text, encoding="utf-8", newline="\n")
    out = tmp_path / label
    assert run(["generate", str(preset), "--out", str(out)]) == 0
    return out


# --- the self-check -----------------------------------------------------------------------------


def drop_an_unconnected_port(project: Project) -> Project:
    """The project with one unreached port (and the data accesses through it) removed.

    The model check still passes, so the miss reaches the self-check.
    """
    reached = {(end.swc, end.port) for connector in project.connectors for end in (connector.source, connector.target)}
    components = list(project.components)
    for index, component in enumerate(components):
        for port in component.ports:
            if (component.name, port.name) not in reached:
                ports = tuple(p for p in component.ports if p is not port)
                runnables = tuple(
                    dataclasses.replace(
                        runnable,
                        reads=tuple(a for a in runnable.reads if a[0] != port.name),
                        writes=tuple(a for a in runnable.writes if a[0] != port.name),
                    )
                    for runnable in component.runnables
                )
                components[index] = dataclasses.replace(component, ports=ports, runnables=runnables)
                return dataclasses.replace(project, components=tuple(components))
    raise AssertionError("the project has no unconnected port")


class TestSelfCheck:
    def faulty_builder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = build_module.build_project

        def faulty(preset: SpecimenPreset) -> Project:
            return drop_an_unconnected_port(original(preset))

        monkeypatch.setattr(build_module, "build_project", faulty)

    def test_a_dropped_port_is_refused_as_missed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.faulty_builder(monkeypatch)
        preset = SpecimenPreset(DEFAULT_NAME, rich_passport())
        with pytest.raises(PassportRefused) as caught:
            build_specimen_files(preset)
        refusals = caught.value.refusals
        assert refusals and all(r.refusal_class == MISSED for r in refusals)
        by_leaf = {r.leaf: r.sentence for r in refusals}
        assert by_leaf["components.unconnected.total"] == "configured 3, generated 2"
        assert list(by_leaf) == [path for path in LEAVES if path in by_leaf]

    def test_the_cli_refuses_a_miss_in_generation_with_exit_2_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.faulty_builder(monkeypatch)
        source = write_json(tmp_path / "passport.json", rich_passport())
        out = tmp_path / "out"
        assert run(["specimen", str(source), "--out", str(out)]) == 2
        lines = capsys.readouterr().err.splitlines()
        assert lines and all(line.startswith("refused in generation: ") for line in lines)
        assert ("refused in generation: components.unconnected.total (missed): configured 3, generated 2") in lines
        assert not out.exists()
        assert [path.name for path in tmp_path.iterdir()] == ["passport.json"]

    def test_the_self_check_compares_every_stated_leaf_but_the_versions(self) -> None:
        stated = neutral_passport()
        stated["passport_version"] = "1.7.0"
        stated["contract_version"] = "12.3.1"
        generated = copy.deepcopy(neutral_passport())
        assert self_check(stated, generated) == []
        generated["components"]["ports"]["top"] = [2, 0]
        generated["identifiers"]["hardware"]["mean_length"] = 7.25
        assert self_check(stated, generated) == [
            Refusal("components.ports.top", MISSED, "configured [1, 1], generated [2, 0]"),
            Refusal("identifiers.hardware.mean_length", MISSED, "configured 7.5, generated 7.25"),
        ]

    def test_an_unstated_leaf_is_not_checked(self) -> None:
        stated = neutral_passport()
        del stated["components"]["ports"]
        generated = copy.deepcopy(neutral_passport())
        generated["components"]["ports"] = {"total": 9, "top": [8, 1]}
        assert self_check(stated, generated) == []

    def test_a_neutral_mean_stated_as_a_float_meets_the_int_zero(self) -> None:
        stated = neutral_passport()
        stated["identifiers"]["units"]["mean_length"] = 0.0
        assert self_check(stated, neutral_passport()) == []

    def test_a_miss_line_is_prefixed_refused_in_generation(self) -> None:
        refused = PassportRefused([Refusal("layers.swc", MISSED, "configured 2, generated 1")])
        assert str(refused) == ("refused in generation: layers.swc (missed): configured 2, generated 1")


# --- the self-check over the remaining shapes ---------------------------------------------------


def shapes_passport() -> dict[str, Any]:
    """Six components of two ports with stubs, three compositions of delegations and the type layers.

    U = 2, three sender-receiver interfaces and one external interface stub, fifteen delegations
    of all three sources (4 reach, 8 chain steps, 3 repeats), and the five type layers with units,
    so linear compu methods.
    """
    passport = neutral_passport()
    passport["identifiers"] = {"max_length": 8}
    passport["layers"].update(
        swc=6,
        interfaces=4,
        connectors=3,
        compositions=3,
        implementation_types=1,
        base_types=1,
        units=2,
        compu_methods=3,
        data_constraints=1,
        application_types=4,
        mapping_sets=1,
    )
    passport["components"]["ports"] = {"total": 12, "top": [2, 2, 2, 2, 2, 2]}
    passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
    passport["components"]["runnables"] = 6
    passport["interfaces"] = {
        "sender_receiver": 3,
        "client_server": 0,
        "fan_out": {"max": 4, "top": [4, 4, 3, 1]},
    }
    passport["connectors"] = {"assembly": 3, "delegation": 15, "links": 0}
    return validate_passport(passport)


def one_stub_more(stated: Any, fan_outs: Any, names: Any) -> InterfacePlan:
    """The interfaces with one external interface stub more than the layer states."""
    plan = build_interfaces(stated, fan_outs, names)
    name = f"If{len(plan.interfaces) + 1}"
    stub = ExternalInterface(name, f"/External/Interfaces/{name}", "CLIENT-SERVER-INTERFACE")
    return dataclasses.replace(plan, interfaces=(*plan.interfaces, stub))


def one_composition_more(
    names: Sequence[str], entered: Sequence[str], connector_free: Sequence[str]
) -> tuple[Composition, ...]:
    """The chain one composition longer than the layer states."""
    extra = f"Cmp{len(names)}"
    assert extra not in names
    return build_compositions([*names, extra], entered, connector_free)


def one_delegation_fewer(*args: Any) -> tuple[Composition, ...]:
    """The delegations without the root's last one (a repeat) and its boundary port."""
    root, *rest = build_delegations(*args)
    last = root.delegations[-1]
    root = dataclasses.replace(
        root,
        delegations=root.delegations[:-1],
        ports=tuple(port for port in root.ports if port.name != last.port),
    )
    return (root, *rest)


def one_unit_more(stated: Any, value_types: Any, names: Any) -> TypeLayers:
    """The type layers with one unit more than the layer states, referenced by nothing."""
    layers = build_types(stated, value_types, names)
    return dataclasses.replace(layers, units=(*layers.units, Unit(f"Un{len(layers.units) + 1}")))


#: Each new builder part with its injected fault and the one leaf the fault misses.
FAULTS: dict[str, tuple[str, Callable[..., Any], str]] = {
    "stubs": ("build_interfaces", one_stub_more, "layers.interfaces: configured 4, generated 5"),
    "compositions": (
        "build_compositions",
        one_composition_more,
        "layers.compositions: configured 3, generated 4",
    ),
    "delegations": (
        "build_delegations",
        one_delegation_fewer,
        "connectors.delegation: configured 15, generated 14",
    ),
    "types": ("build_types", one_unit_more, "layers.units: configured 2, generated 3"),
}


class TestSelfCheckOverTheRemainingShapes:
    def test_the_shapes_passport_is_met_without_a_fault(self) -> None:
        files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, shapes_passport()))
        assert "generation.json" in files

    @pytest.mark.parametrize("part", sorted(FAULTS))
    def test_a_fault_in_a_new_part_is_refused_missed_and_nothing_is_written(
        self,
        part: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        attribute, fault, miss = FAULTS[part]
        monkeypatch.setattr(build_module, attribute, fault)
        leaf, _, sentence = miss.partition(": ")
        with pytest.raises(PassportRefused) as caught:
            build_specimen_files(SpecimenPreset(DEFAULT_NAME, shapes_passport()))
        assert list(caught.value.refusals) == [Refusal(leaf, MISSED, sentence)]
        source = write_json(tmp_path / "passport.json", shapes_passport())
        out = tmp_path / "out"
        assert run(["specimen", str(source), "--out", str(out)]) == 2
        assert capsys.readouterr().err.splitlines() == [f"refused in generation: {leaf} (missed): {sentence}"]
        assert not out.exists()
        assert [path.name for path in tmp_path.iterdir()] == ["passport.json"]


# --- the layout's own check ---------------------------------------------------------------------


def one_root_passport() -> dict[str, Any]:
    """`shapes_passport` in one composition with five delegations.

    The root holds every component, two of them connector-free with two delegation-only ports each
    (4 reach, 1 repeat), two others with one unconnected port each.
    """
    passport = shapes_passport()
    passport["layers"]["compositions"] = 1
    passport["connectors"]["delegation"] = 5
    return passport


def one_delegation_moved(
    compositions: Sequence[Composition],
    component_names: Sequence[str],
    ports: Sequence[Sequence[Port]],
    open_ports: Sequence[Sequence[str]],
    layout: Layout,
    *rest: Any,
) -> tuple[Composition, ...]:
    """The delegations with one reach delegation moved to an unconnected port of another root member.

    D, U and u1 are kept, so the pass sees nothing, but the component the delegation left gains
    an unconnected port and the one it reached loses one.
    """
    root, *others = build_delegations(compositions, component_names, ports, open_ports, layout, *rest)
    types = {m.name: m.type for m in root.members if m.type is not None}
    member_of = {component: member for member, component in types.items()}
    delegated = {(types[d.member], d.member_port) for d in root.delegations if d.member in types}
    index = {name: j for j, name in enumerate(component_names)}
    free = [(name, port) for name in member_of for port in open_ports[index[name]] if (name, port) not in delegated]
    target, target_port = free[0]
    moved = next(
        d
        for d in root.delegations
        if d.member in types and types[d.member] != target and d.member_port in open_ports[index[str(types[d.member])]]
    )
    inner = next(p for p in ports[index[target]] if p.name == target_port)
    boundary = tuple(
        CompositionPort(p.name, inner.direction, inner.interface) if p.name == moved.port else p for p in root.ports
    )
    delegations = tuple(
        dataclasses.replace(d, member=member_of[target], member_port=target_port) if d is moved else d
        for d in root.delegations
    )
    return (dataclasses.replace(root, ports=boundary, delegations=delegations), *others)


class TestTheLayoutCheck:
    def test_the_built_project_holds_its_layout(self) -> None:
        preset = SpecimenPreset(DEFAULT_NAME, one_root_passport())
        shape = specimen_shape(stated_leaves(preset.passport))
        assert isinstance(shape, Shape)
        check_layout(render_specimen(preset).project, shape.layout)
        assert "generation.json" in build_specimen_files(preset)

    def test_a_delegation_moved_to_another_component_is_refused_and_nothing_is_written(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(build_module, "build_delegations", one_delegation_moved)
        preset = SpecimenPreset(DEFAULT_NAME, one_root_passport())
        # The pass sees nothing: the fault keeps every stated leaf.
        rendered = render_specimen(preset)
        assert self_check(preset.passport, rendered.passport) == []
        with pytest.raises(GenerationError, match="the built project differs from its layout"):
            build_specimen_files(preset)
        source = write_json(tmp_path / "passport.json", one_root_passport())
        out = tmp_path / "out"
        assert run(["specimen", str(source), "--out", str(out)]) == 1
        assert "the built project differs from its layout" in capsys.readouterr().err
        assert not out.exists()
        assert [path.name for path in tmp_path.iterdir()] == ["passport.json"]


def seed_501_preset() -> SpecimenPreset:
    """Seed 501's shaped witness (counts 12, 5, 2; C = 4).

    A solver-built layout with a deep component, reach 4, two chain steps and no repeat.
    """
    return SpecimenPreset(DEFAULT_NAME, validate_passport(compute_passport(shaped_witness(501, 3))))


class TestTheLayoutCheckNamesTheField:
    """`check_layout` against a layout one field away from the built project's.

    Each field is compared, and the error names it.
    """

    @pytest.mark.parametrize(
        ("changes", "field"),
        [
            ({"unconnected": (7, 2)}, "the components"),
            ({"unconnected": (6, 2, 1)}, "the unconnected ports"),
            ({"connected": (3, 2, 1)}, "the connector-reached ports"),
            ({"delegated": (1, 0, 3)}, "the delegation-only ports"),
            ({"root": (0, 1, 2), "deep": ()}, "the membership"),
            ({"steps": 1, "repeats": 1}, "the sources"),
            ({"connectors": 4}, "the connectors"),
        ],
        ids=[
            "components",
            "unconnected",
            "connected",
            "delegated",
            "membership",
            "sources",
            "count",
        ],
    )
    def test_a_field_the_project_does_not_hold_is_named(self, changes: dict[str, Any], field: str) -> None:
        preset = seed_501_preset()
        shape = specimen_shape(stated_leaves(preset.passport))
        assert isinstance(shape, Shape)
        layout = shape.layout
        assert (layout.deep, layout.reach, layout.steps, layout.repeats) == ((2,), 4, 2, 0)
        project = render_specimen(preset).project
        check_layout(project, layout)
        with pytest.raises(GenerationError, match=rf"differs from its layout \({field}\)"):
            check_layout(project, dataclasses.replace(layout, **changes))


def _delegations_with(
    monkeypatch: pytest.MonkeyPatch,
    changes: Callable[[Layout], dict[str, Any]],
    centres: Callable[[Sequence[Sequence[Port]]], Any] | None = None,
) -> None:
    """`build_delegations` handed seed 501's layout with `changes`.

    When given, other star centres are handed too.
    """

    def changed(
        compositions: Sequence[Composition],
        component_names: Sequence[str],
        ports: Sequence[Sequence[Port]],
        open_ports: Sequence[Sequence[str]],
        layout: Layout,
        given: Any,
        stubs: frozenset[str],
    ) -> tuple[Composition, ...]:
        return build_delegations(
            compositions,
            component_names,
            ports,
            open_ports,
            dataclasses.replace(layout, **changes(layout)),
            given if centres is None else centres(ports),
            stubs,
        )

    monkeypatch.setattr(build_module, "build_delegations", changed)


class TestTheDelegationsExecuteTheLayout:
    """`build_delegations` gives the layout's sources exactly or raises.

    It never decides a layout of its own.
    """

    @pytest.mark.parametrize(
        ("changes", "match"),
        [
            (lambda layout: {"steps": layout.steps + 100}, "do not reproduce the layout's sources"),
            (lambda layout: {"repeats": layout.repeats + 100}, "do not reproduce the layout"),
            (lambda layout: {"delegated": (2, 0, 50)}, "fewer open ports"),
        ],
        ids=["steps", "repeats", "open-ports"],
    )
    def test_a_source_the_chain_cannot_give_is_a_generation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        changes: Callable[[Layout], dict[str, Any]],
        match: str,
    ) -> None:
        _delegations_with(monkeypatch, changes)
        with pytest.raises(GenerationError, match=match):
            render_specimen(seed_501_preset())

    def test_a_repeat_from_a_star_centre_outside_the_root_is_a_generation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Component 2 is deep (a member of the deepest composition).
        _delegations_with(
            monkeypatch,
            lambda layout: {"repeats": 1, "steps": layout.steps - 1},
            lambda ports: ((2, ports[2][0].name),),
        )
        with pytest.raises(GenerationError, match="a star centre lies outside the root"):
            render_specimen(seed_501_preset())


# --- no leaf is left not-built ------------------------------------------------------------------


class TestEveryMetLeafIsBuilt:
    def test_the_interim_class_is_gone(self) -> None:
        assert not hasattr(build_module, "NOT_BUILT")
        assert not hasattr(build_module, "not_built_leaves")

    @pytest.mark.parametrize("label", sorted(PASSPORTS))
    def test_the_refusals_before_generation_are_the_feasibility_check_s(self, label: str) -> None:
        passport = PASSPORTS[label]()
        passport["layers"]["tasks"] = 3
        preset = SpecimenPreset(DEFAULT_NAME, passport)
        with pytest.raises(PassportRefused) as refused:
            build_specimen_files(preset)
        assert list(refused.value.refusals) == check_feasibility(passport)


# --- the specimen sidecar -----------------------------------------------------------------------


class TestSpecimenSidecar:
    @pytest.mark.parametrize("label", sorted(PASSPORTS))
    def test_the_kind_and_the_fields(self, tmp_path: Path, label: str) -> None:
        passport = PASSPORTS[label]()
        text = preset_text(passport)
        out = generate(tmp_path, label, text)
        document = sidecar(out)
        assert list(document) == [
            "kind",
            "schema_version",
            "generator",
            "toolchain",
            "preset",
            "project",
            "passport",
        ]
        assert document["kind"] == SPECIMEN_KIND == "specimen"
        assert document["schema_version"] == SPECIMEN_SCHEMA_VERSION == "1.0"
        assert document["generator"] == {"name": "swcraft-specimens", "version": __version__}
        # No installed release (the autouse fixture).
        assert document["toolchain"] == {"swcraft_version": None}
        canonical = preset_text(passport, DEFAULT_NAME).encode("utf-8")
        assert document["preset"] == {"sha256": sha256(canonical).hexdigest()}
        assert list(document["project"]) == ["layer_counts", "project_tree_sha256"]
        assert list(document["project"]["layer_counts"]) == LIST_ORDER
        assert sorted(LIST_ORDER) == sorted(LAYERS)
        assert document["project"]["layer_counts"] == {
            layer: document["passport"]["layers"][layer] for layer in LIST_ORDER
        }
        assert document["project"]["project_tree_sha256"] == project_tree_sha256(tree(out))
        assert validate_passport(copy.deepcopy(document["passport"])) == document["passport"]
        stated = stated_leaves(passport)
        computed = stated_leaves(document["passport"])
        assert {path: computed[path] for path in stated} == stated
        assert "placement" not in document and "timing" not in document

    def test_the_sidecar_ends_with_one_newline_and_is_deterministic(self, tmp_path: Path) -> None:
        text = preset_text(rich_passport())
        first = generate(tmp_path, "first", text)
        second = generate(tmp_path, "second", text)
        assert tree(first) == tree(second)
        data = (first / "generation.json").read_bytes()
        assert data.endswith(b"}\n") and not data.endswith(b"\n\n") and b"\r" not in data

    def test_the_preset_digest_is_the_canonical_text_s_whatever_the_file(self, tmp_path: Path) -> None:
        passport = neutral_passport()
        plain = generate(tmp_path, "plain", preset_text(passport))
        named = generate(tmp_path, "named", preset_text(passport, DEFAULT_NAME))
        assert tree(plain) == tree(named)

    def rendered(self) -> tuple[SpecimenPreset, dict[str, bytes]]:
        preset = SpecimenPreset(DEFAULT_NAME, rich_passport())
        return preset, build_specimen_files(preset)

    def check(self, preset: SpecimenPreset, files: dict[str, bytes], document: object) -> None:
        rendered = render_specimen(preset)
        data = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        validate_specimen_sidecar(data, preset, rendered.project, files, documents=rendered.documents)

    def test_the_built_sidecar_validates(self) -> None:
        preset, files = self.rendered()
        self.check(preset, files, json.loads(files["generation.json"]))

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.__setitem__("kind", "waters"), r"^generation\.json\.kind: neither"),
            (lambda d: d.__setitem__("schema_version", "2.1"), "schema_version"),
            (lambda d: d["generator"].__setitem__("version", "0.0.0"), "generator"),
            (lambda d: d["toolchain"].__setitem__("swcraft_version", "0.0.0"), "toolchain"),
            (lambda d: d["preset"].__setitem__("sha256", "0" * 64), "preset.sha256"),
            (lambda d: d["project"]["layer_counts"].__setitem__("swc", 13), "layer_counts"),
            (
                lambda d: d["project"].__setitem__(
                    "layer_counts", dict(reversed(list(d["project"]["layer_counts"].items())))
                ),
                r"^generation\.json\.project\.layer_counts mismatch",
            ),
            (
                lambda d: d["project"].__setitem__("project_tree_sha256", "0" * 64),
                "project_tree_sha256",
            ),
            (
                lambda d: d["passport"]["components"].__setitem__("runnables", 8),
                r"^generation\.json\.passport does not match",
            ),
            (lambda d: d.__setitem__("placement", {}), r"^generation\.json keys"),
            (lambda d: d["project"].__setitem__("name", "X"), r"^generation\.json\.project keys"),
            (
                lambda d: d["preset"].__setitem__("ratio", 0.5),
                r"^generation\.json\.preset\.ratio: floating point",
            ),
        ],
    )
    def test_each_field_is_checked(self, mutate: Any, message: str) -> None:
        preset, files = self.rendered()
        document = json.loads(files["generation.json"])
        mutate(document)
        with pytest.raises(GenerationError, match=message):
            self.check(preset, files, document)

    def test_the_kind_is_read_before_anything_else(self) -> None:
        assert sidecar_kind({"kind": "specimen"}) == SPECIMEN_KIND
        assert sidecar_kind({"schema_version": "2.1"}) == "waters"
        with pytest.raises(GenerationError, match="kind"):
            sidecar_kind({"kind": "other"})

    def test_the_waters_validator_refuses_a_specimen_sidecar(self) -> None:
        _preset, files = self.rendered()
        with pytest.raises(GenerationError, match="kind"):
            validate_sidecar(
                files["generation.json"],
                cast(Any, None),
                cast(Any, None),
                cast(Any, None),
                cast(Any, None),
                files,
                documents={},
            )


# --- the two commands agree ---------------------------------------------------------------------


class TestOneCommandForm:
    @pytest.mark.parametrize("name", [None, "CalibrationSpecimen"])
    def test_specimen_equals_preset_from_passport_then_generate(self, tmp_path: Path, name: str | None) -> None:
        source = write_json(tmp_path / "passport.json", rich_passport())
        named = [] if name is None else ["--name", name]
        preset = tmp_path / "preset.yaml"
        assert run(["preset-from-passport", str(source), "--out", str(preset), *named]) == 0
        assert run(["generate", str(preset), "--out", str(tmp_path / "composed")]) == 0
        assert run(["specimen", str(source), "--out", str(tmp_path / "direct"), *named]) == 0
        composed = tree(tmp_path / "composed")
        assert composed == tree(tmp_path / "direct")
        assert "generation.json" in composed and "swcraft.yaml" in composed
        manifest = composed["swcraft.yaml"].decode("utf-8")
        assert (name or DEFAULT_NAME) in manifest


# --- the fixed point ----------------------------------------------------------------------------


class TestFixedPoint:
    @pytest.mark.parametrize("label", sorted(PASSPORTS))
    def test_the_default_name_loop_needs_nothing_carried(self, tmp_path: Path, label: str) -> None:
        first = generate(tmp_path, "first", preset_text(PASSPORTS[label]()))
        passport = sidecar(first)["passport"]
        second = generate(tmp_path, "second", preset_text(passport))
        assert project_files(first) == project_files(second)
        assert sidecar(second)["passport"] == passport
        assert sidecar(first)["project"]["project_tree_sha256"] == sidecar(second)["project"]["project_tree_sha256"]

    def test_a_named_preset_carries_its_name_over_explicitly(self, tmp_path: Path) -> None:
        name = "FixedPointSpecimen"
        first = generate(tmp_path, "first", preset_text(rich_passport(), name))
        passport = sidecar(first)["passport"]
        second = generate(tmp_path, "second", preset_text(passport, name))
        assert project_files(first) == project_files(second)
        unnamed = generate(tmp_path, "unnamed", preset_text(passport))
        assert project_files(unnamed) != project_files(first)


def test_a_waters_sidecar_is_refused_as_one() -> None:
    preset, files = TestSpecimenSidecar().rendered()
    waters = json.dumps({"schema_version": "2.1"}).encode("utf-8")
    rendered = render_specimen(preset)
    with pytest.raises(GenerationError, match=r"a WATERS sidecar, not a specimen one"):
        validate_specimen_sidecar(waters, preset, rendered.project, files, documents=rendered.documents)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"{not json", r"^generation\.json is invalid"),
        (b"[]", r"is not an object"),
        # A RecursionError of the JSON reader, refused as a GenerationError.
        (b"[" * 100000 + b"]" * 100000, r"nests deeper than the JSON reader follows$"),
    ],
    ids=["not-json", "not-an-object", "nested-too-deep"],
)
def test_a_sidecar_that_is_no_object_is_refused(data: bytes, message: str) -> None:
    preset, files = TestSpecimenSidecar().rendered()
    rendered = render_specimen(preset)
    with pytest.raises(GenerationError, match=message):
        validate_specimen_sidecar(data, preset, rendered.project, files, documents=rendered.documents)
