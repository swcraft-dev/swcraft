"""The newer element kinds in the model, the emitter, the file map and the model validation.

An external interface stub and the five type layers render as SWCraft's documents, the file map
hands them to the passport pass, and ``validate_project`` refuses a reference naming no declared
element. A task naming a partition with its budget and ``coexistence``, a partition's
``coexistence``, a sequence, requirement links on all seven site kinds at versions 1 and 2, and a
requirement without ``asil`` render as SWCraft's documents and are checked by the model validation
and the rendered-tree oracle.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens.emit import build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.metrics import layer_counts
from swcraft_specimens.model import (
    ApplicationType,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    Delegation,
    LinearCompuMethod,
    MappingSet,
    Project,
    RequirementLink,
    build_tiny_project,
    validate_project,
)
from swcraft_specimens.passport import compute_passport
from swcraft_specimens.preset import load_preset
from swcraft_specimens.specimen.hardware import PARTITION_COEXISTENCE
from swcraft_specimens.specimen.identifiers import SPECIMEN_LIMITS
from swcraft_specimens.specimen.tasks import TASK_COEXISTENCE
from swcraft_specimens.validate import validate_file_map
from tests.oracle.new_kinds import (
    C0,
    C1,
    PARTITION0,
    PARTITION1,
    ROOT,
    TASK0,
    TASK1,
    project_with_later_kinds,
    project_with_new_kinds,
)


def _document(tree: dict[str, bytes], path: str) -> Any:
    return yaml.safe_load(tree[path].decode("utf-8"))


def _keys(tree: dict[str, bytes], path: str) -> list[str]:
    return list(_document(tree, path))


def test_an_external_interface_renders_as_a_stub_not_as_client_server() -> None:
    tree = build_file_map(project_with_new_kinds())
    path = "model/interfaces/IfStub.yaml"
    assert _keys(tree, path) == ["name", "kind", "external_ref", "external_dest"]
    assert _document(tree, path) == {
        "name": "IfStub",
        "kind": "external_interface",
        "external_ref": "/External/Interfaces/IfStub",
        "external_dest": "CLIENT-SERVER-INTERFACE",
    }


@pytest.mark.parametrize(
    ("path", "document"),
    [
        ("model/data_types/units/Un1.yaml", {"name": "Un1"}),
        ("model/data_types/units/Un2.yaml", {"name": "Un2", "display_name": "km/h"}),
        (
            "model/data_types/compu_methods/Cm1.yaml",
            {"name": "Cm1", "kind": "linear", "unit": "Un1", "factor": 1, "offset": 0},
        ),
        (
            "model/data_types/compu_methods/Cm2.yaml",
            {
                "name": "Cm2",
                "kind": "texttable",
                "entries": [{"name": "E0", "value": 0}, {"name": "E1", "value": 1}],
            },
        ),
        ("model/data_types/constraints/Dc1.yaml", {"name": "Dc1", "min": 0, "max": 100}),
        (
            "model/data_types/application/At1.yaml",
            {
                "name": "At1",
                "kind": "application_type",
                "unit_ref": "Un1",
                "compu_method_ref": "Cm1",
                "data_constr_ref": "Dc1",
            },
        ),
        ("model/data_types/application/At2.yaml", {"name": "At2", "kind": "application_type"}),
        (
            "model/data_types/mapping_sets/Ms1.yaml",
            {
                "name": "Ms1",
                "maps": [
                    {"application": "At1", "implementation": "u8"},
                    {"application": "At2", "implementation": "u8"},
                ],
            },
        ),
    ],
)
def test_each_type_kind_renders_in_its_layer_with_its_fields_in_a_fixed_order(
    path: str, document: dict[str, Any]
) -> None:
    tree = build_file_map(project_with_new_kinds())
    # Equality of the parsed document holds the fields; the key list holds their order.
    assert _document(tree, path) == document
    assert _keys(tree, path) == list(document)


def test_the_five_type_layers_carry_no_gitkeep_once_written() -> None:
    tree = build_file_map(project_with_new_kinds())
    for layer in ("application", "units", "compu_methods", "constraints", "mapping_sets"):
        assert f"model/data_types/{layer}/.gitkeep" not in tree


def test_a_project_without_the_new_kinds_renders_as_before() -> None:
    project = project_with_new_kinds()
    tiny = replace(
        project,
        interfaces=project.interfaces[:2],
        units=(),
        compu_methods=(),
        data_constraints=(),
        application_types=(),
        mapping_sets=(),
    )
    tree = build_file_map(tiny)
    for layer in ("application", "units", "compu_methods", "constraints", "mapping_sets"):
        assert tree[f"model/data_types/{layer}/.gitkeep"] == b""


def test_the_file_map_hands_the_new_documents_to_the_passport_pass() -> None:
    project = project_with_new_kinds()
    tree = build_file_map(project)
    overhead = load_preset(ROOT / "tests/fixtures/tiny.yaml").task_overhead_us
    documents = validate_file_map(tree, project, task_overhead_us=overhead)
    names = {layer: [d["id" if layer == "requirements" else "name"] for d in docs] for layer, docs in documents.items()}
    assert names["units"] == ["Un1", "Un2"]
    assert names["compu_methods"] == ["Cm1", "Cm2"]
    assert names["data_constraints"] == ["Dc1"]
    assert names["application_types"] == ["At1", "At2"]
    assert names["mapping_sets"] == ["Ms1"]
    assert "IfStub" in names["interfaces"]


def test_a_file_the_model_does_not_hold_in_a_type_layer_is_refused() -> None:
    project = project_with_new_kinds()
    tree = dict(build_file_map(project))
    tree["model/data_types/units/Un9.yaml"] = b"name: Un9\n"
    overhead = load_preset(ROOT / "tests/fixtures/tiny.yaml").task_overhead_us
    with pytest.raises(GenerationError, match=r"rendered data_types/units file map mismatch"):
        validate_file_map(tree, project, task_overhead_us=overhead)


def test_the_project_with_every_new_kind_validates() -> None:
    validate_project(project_with_new_kinds())


def _with_mapping(project: Project, application: str, implementation: str) -> Project:
    return replace(project, mapping_sets=(MappingSet("Ms1", ((application, implementation),)),))


@pytest.mark.parametrize(
    ("application", "implementation", "match"),
    [
        ("AtMissing", "u8", "mapping set Ms1 names an undeclared application type 'AtMissing'"),
        ("At1", "u9", "mapping set Ms1 maps At1 to 'u9', which is no declared value type"),
        # An array is an implementation type, not a value type: mapping sets pair value types only.
        ("At1", "payload_array", "maps At1 to 'payload_array', which is no declared value type"),
    ],
)
def test_a_mapping_row_naming_an_undeclared_type_is_refused(application: str, implementation: str, match: str) -> None:
    project = _with_mapping(project_with_new_kinds(), application, implementation)
    with pytest.raises(GenerationError, match=match):
        validate_project(project)


def test_a_mapping_set_mapping_one_application_type_twice_is_refused() -> None:
    project = replace(project_with_new_kinds(), mapping_sets=(MappingSet("Ms1", (("At1", "u8"), ("At1", "u8"))),))
    with pytest.raises(GenerationError, match="mapping set Ms1 maps At1 twice"):
        validate_project(project)


@pytest.mark.parametrize(
    ("application_type", "match"),
    [
        (ApplicationType("At1", unit_ref="UnX"), "At1 unit_ref names an undeclared unit 'UnX'"),
        (
            ApplicationType("At1", compu_method_ref="CmX"),
            "At1 compu_method_ref names an undeclared linear compu method 'CmX'",
        ),
        (
            # A texttable from an application type is SWCraft's `compu-method-wrong-category`.
            ApplicationType("At1", compu_method_ref="Cm2"),
            "At1 compu_method_ref names an undeclared linear compu method 'Cm2'",
        ),
        (
            ApplicationType("At1", data_constr_ref="DcX"),
            "At1 data_constr_ref names an undeclared data constraint 'DcX'",
        ),
    ],
)
def test_an_application_type_reference_naming_an_undeclared_element_is_refused(
    application_type: ApplicationType, match: str
) -> None:
    project = project_with_new_kinds()
    project = replace(project, application_types=(application_type, *project.application_types[1:]))
    with pytest.raises(GenerationError, match=match):
        validate_project(project)


def test_a_linear_compu_method_naming_an_undeclared_unit_is_refused() -> None:
    project = project_with_new_kinds()
    project = replace(
        project,
        compu_methods=(LinearCompuMethod("Cm1", unit="UnX"), *project.compu_methods[1:]),
    )
    with pytest.raises(GenerationError, match="compu method Cm1 names an undeclared unit 'UnX'"):
        validate_project(project)


@pytest.mark.parametrize(
    ("field", "context"),
    [
        ("units", "unit names"),
        ("compu_methods", "compu method names"),
        ("data_constraints", "data constraint names"),
        ("application_types", "application type names"),
        ("mapping_sets", "mapping set names"),
    ],
)
def test_two_elements_of_one_type_layer_sharing_a_name_ignoring_case_are_refused(field: str, context: str) -> None:
    project = project_with_new_kinds()
    elements = getattr(project, field)
    twin = replace(elements[0], name=elements[0].name.upper())
    changes: dict[str, Any] = {field: (*elements, twin)}
    project = replace(project, **changes)
    with pytest.raises(GenerationError, match=f"duplicate {context} ignoring case"):
        validate_project(project)


def test_a_runnable_member_on_a_stub_port_is_refused() -> None:
    # A stub lists no member names, so no runnable may read, write or call through it.
    project = project_with_new_kinds()
    component = project.components[1]
    ports = tuple(replace(p, interface="IfStub") if p.name == "require_service" else p for p in component.ports)
    project = replace(project, components=(project.components[0], replace(component, ports=ports)))
    with pytest.raises(GenerationError, match="unresolved runnable member: IfStub.exchange"):
        validate_project(project)


# --- the specimen chain ------------------------------------------------------------------------


def _chain(*compositions: Composition, connectors: tuple[Connector, ...] | None = None) -> Project:
    """The tiny project with its compositions replaced (the connectors kept unless given)."""
    tiny = project_with_new_kinds()
    return replace(
        tiny,
        compositions=compositions,
        connectors=tiny.connectors if connectors is None else connectors,
    )


def _members(*names: str) -> tuple[CompositionMember, ...]:
    """Component members `M0001`, ... for the components, a composition member for `Cmp...`."""
    return tuple(
        CompositionMember(name, None, name) if name.startswith("Cmp") else CompositionMember(f"M{index:04d}", name)
        for index, name in enumerate(names, start=1)
    )


def test_the_specimen_chain_with_root_owned_connectors_between_root_members_validates() -> None:
    # Root -> Cmp1 -> Cmp2, both components root members, the deepest empty: the domain shape
    # refuses this (three compositions over one domain); the chain admits it.
    validate_project(
        _chain(
            Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
            Composition("Cmp1", "Cmp1", False, _members("Cmp2")),
            Composition("Cmp2", "Cmp2", False, ()),
        )
    )


def test_a_chain_with_a_component_in_two_compositions_is_refused() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, _members(C1)),
    )
    with pytest.raises(GenerationError, match="member of exactly one composition"):
        validate_project(project)


def test_a_chain_leaving_a_component_out_is_refused() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, ()),
        connectors=(),
    )
    with pytest.raises(GenerationError, match="member of exactly one composition"):
        validate_project(project)


def test_a_connector_owned_by_a_nested_composition_is_refused() -> None:
    tiny = project_with_new_kinds()
    project = _chain(
        Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, ()),
        connectors=(replace(tiny.connectors[0], composition="Cmp1"), *tiny.connectors[1:]),
    )
    with pytest.raises(GenerationError, match="connector owned by a nested composition"):
        validate_project(project)


def test_a_connector_to_a_component_below_the_root_is_refused() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, _members(C1)),
    )
    with pytest.raises(GenerationError, match="connector endpoint outside the root"):
        validate_project(project)


def test_a_composition_outside_the_chain_is_refused() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, ()),
        Composition("Cmp2", "Cmp2", False, ()),
    )
    with pytest.raises(GenerationError, match="composition outside the chain: Cmp2"):
        validate_project(project)


def test_a_chain_naming_an_undeclared_composition_is_refused() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
        Composition("Cmp2", "Cmp2", False, ()),
    )
    with pytest.raises(GenerationError, match="unresolved composition reference: Cmp1"):
        validate_project(project)


def test_a_chain_returning_to_a_composition_on_it_is_a_cycle() -> None:
    project = _chain(
        Composition("Root", "Root", True, _members(C0, C1, "Cmp1")),
        Composition("Cmp1", "Cmp1", False, _members("Cmp2")),
        Composition("Cmp2", "Cmp2", False, _members("Cmp1")),
    )
    with pytest.raises(GenerationError, match="composition cycle detected"):
        validate_project(project)


def _delegated(root_port: CompositionPort, member_port: str = "BP1") -> Project:
    """Root -> Cmp1 with C1 in Cmp1, delegated through two boundary ports; no connector.

    Cmp1's BP1 is delegated to C1's require port, the root's BP1 to Cmp1's BP1 (a composition
    member's boundary port).
    """
    return _chain(
        Composition(
            "Root",
            "Root",
            True,
            _members(C0, "Cmp1"),
            ports=(root_port,),
            delegations=(Delegation("Dlg1", "BP1", "Cmp1", member_port),),
        ),
        Composition(
            "Cmp1",
            "Cmp1",
            False,
            _members(C1),
            ports=(CompositionPort("BP1", "require", "IfData"),),
            delegations=(Delegation("Dlg1", "BP1", "M0001", "require_data"),),
        ),
        connectors=(),
    )


def test_a_delegation_onto_a_composition_members_boundary_port_resolves() -> None:
    validate_project(_delegated(CompositionPort("BP1", "require", "IfData")))


@pytest.mark.parametrize(
    ("root_port", "member_port"),
    [
        (CompositionPort("BP1", "provide", "IfData"), "BP1"),
        (CompositionPort("BP1", "require", "IfService"), "BP1"),
        (CompositionPort("BP1", "require", "IfData"), "BP9"),
    ],
)
def test_a_delegation_onto_a_mismatched_or_missing_boundary_port_is_refused(
    root_port: CompositionPort, member_port: str
) -> None:
    with pytest.raises(GenerationError, match=r"delegation direction/interface mismatch: Root.Dlg1"):
        validate_project(_delegated(root_port, member_port))


def test_delegations_are_checked_in_the_flat_shape() -> None:
    tiny = project_with_new_kinds()
    (root,) = tiny.compositions
    project = replace(
        tiny,
        compositions=(
            replace(
                root,
                ports=(CompositionPort("BP1", "require", "IfData"),),
                delegations=(Delegation("Dlg1", "BP1", C1, "no_such_port"),),
            ),
        ),
    )
    with pytest.raises(GenerationError, match=r"delegation direction/interface mismatch: Root.Dlg1"):
        validate_project(project)


# --- the later kinds ----------------------------------------------------------------------------


def _later_tree() -> dict[str, bytes]:
    return build_file_map(project_with_later_kinds())


def _overhead() -> int:
    return load_preset(ROOT / "tests/fixtures/tiny.yaml").task_overhead_us


def test_the_project_with_every_later_kind_validates_with_and_without_the_limits() -> None:
    project = project_with_later_kinds()
    validate_project(project)
    validate_project(project, SPECIMEN_LIMITS)


def test_the_limits_of_the_three_new_layers_are_128() -> None:
    # SWCraft emits no SHORT-NAME from a requirement, a task or a sequence.
    for key in ("requirements", "tasks", "sequences"):
        assert SPECIMEN_LIMITS[key] == 128, key


@pytest.mark.parametrize(
    ("field", "context"),
    [("requirements", "requirement id"), ("tasks", "task name"), ("sequences", "sequence name")],
)
def test_a_name_of_a_new_layer_above_128_is_refused(field: str, context: str) -> None:
    project = project_with_later_kinds()
    elements = getattr(project, field)
    attribute = "id" if field == "requirements" else "name"
    renamed = replace(elements[-1], **{attribute: "N" * 129})
    changes: dict[str, Any] = {field: (*elements[:-1], renamed)}
    if field == "tasks":
        changes["sequences"] = (replace(project.sequences[0], task="N" * 129),)
    with pytest.raises(GenerationError, match=f"invalid {context} 'N+': .* max 128 characters"):
        validate_project(replace(project, **changes), SPECIMEN_LIMITS)


def test_a_task_names_its_partition_with_its_budget_and_coexistence() -> None:
    tree = _later_tree()
    path = f"model/tasks/{TASK0}.yaml"
    assert _keys(tree, path) == [
        "name",
        "period",
        "priority",
        "partition",
        "wcet",
        "runnables",
        "coexistence",
        "satisfies",
    ]
    document = _document(tree, path)
    assert document["partition"] == PARTITION0
    assert document["wcet"].endswith("us")
    assert document["coexistence"] == TASK_COEXISTENCE
    # A task without a coexistence text or a link writes neither key.
    assert _keys(tree, f"model/tasks/{TASK1}.yaml") == [
        "name",
        "period",
        "priority",
        "partition",
        "wcet",
        "runnables",
    ]
    assert _document(tree, f"model/tasks/{TASK1}.yaml")["partition"] == PARTITION1


def test_a_partition_writes_its_coexistence_and_only_when_it_has_one() -> None:
    tree = _later_tree()
    assert _document(tree, f"model/hardware/{PARTITION0}.yaml") == {
        "name": PARTITION0,
        "kind": "partition",
        "core": "CORE0",
        "asil": "B",
        "coexistence": PARTITION_COEXISTENCE,
    }
    assert _document(tree, f"model/hardware/{PARTITION1}.yaml") == {
        "name": PARTITION1,
        "kind": "partition",
        "core": "CORE1",
    }


def test_a_sequence_renders_with_its_task_an_empty_title_and_its_steps() -> None:
    tree = _later_tree()
    path = "model/sequences/Sq1.yaml"
    assert _keys(tree, path) == ["name", "task", "title", "steps"]
    assert _document(tree, path) == {
        "name": "Sq1",
        "task": TASK1,
        "title": "",
        "steps": [
            {"swc": C1, "runnable": "read", "note": ""},
            {"swc": C1, "runnable": "call", "note": ""},
        ],
    }
    assert "model/sequences/.gitkeep" not in tree


def test_the_links_land_on_the_seven_site_kinds_at_their_versions() -> None:
    tree = _later_tree()
    swc0 = _document(tree, f"model/swc/dom01/{C0}.yaml")
    swc1 = _document(tree, f"model/swc/dom01/{C1}.yaml")
    assert swc0["satisfies"] == ["Rq1@2"]
    assert "satisfies" not in swc1
    ports0 = {p["name"]: p for p in swc0["ports"]}
    ports1 = {p["name"]: p for p in swc1["ports"]}
    assert ports0["provide_data"]["satisfies"] == ["Rq1@1"]
    assert "satisfies" not in ports0["provide_service"]
    assert ports1["require_data"]["satisfies"] == ["Rq2@1"]
    runnables1 = {r["name"]: r for r in swc1["runnables"]}
    assert runnables1["read"]["satisfies"] == ["Rq1@2"]
    assert "satisfies" not in runnables1["call"]
    assert _document(tree, "model/interfaces/IfData.yaml")["satisfies"] == ["Rq1@2"]
    assert "satisfies" not in _document(tree, "model/interfaces/IfService.yaml")
    assert _document(tree, "model/compositions/Root.yaml")["satisfies"] == ["Rq1@2"]
    assert _document(tree, f"model/tasks/{TASK0}.yaml")["satisfies"] == ["Rq1@2"]
    assert _document(tree, "model/hardware/CPU.yaml")["satisfies"] == ["Rq1@2"]
    assert "satisfies" not in _document(tree, "model/hardware/CORE0.yaml")


def test_a_requirement_states_its_version_and_writes_asil_only_when_it_has_one() -> None:
    tree = _later_tree()
    assert _keys(tree, "model/requirements/Rq1.yaml") == [
        "id",
        "title",
        "description",
        "version",
        "source",
    ]
    assert _document(tree, "model/requirements/Rq1.yaml")["version"] == 2
    assert _document(tree, "model/requirements/Rq2.yaml")["version"] == 1
    assert _document(tree, "model/requirements/Rq3.yaml")["asil"] == "B"


def test_the_file_map_checks_the_later_kinds_and_the_pass_counts_them() -> None:
    project = project_with_later_kinds()
    documents = validate_file_map(_later_tree(), project, task_overhead_us=_overhead())
    assert [d["name"] for d in documents["sequences"]] == ["Sq1"]
    passport = compute_passport(documents)
    assert passport["layers"]["requirements"] == 3
    assert passport["layers"]["tasks"] == 2
    assert passport["layers"]["sequences"] == 1
    assert passport["requirements"] == {
        "on_all_site_kinds": 1,
        "on_one_port": 1,
        "unallocated": 1,
        "stale_links": 1,
        "links": 8,
    }


def test_the_layer_counts_count_the_sequences() -> None:
    counts = layer_counts(project_with_later_kinds())
    assert (counts.requirements, counts.tasks, counts.sequences) == (3, 2, 1)


def test_tasks_that_run_no_runnable_leave_every_runnable_unmapped() -> None:
    project = project_with_later_kinds()
    idle = tuple(replace(task, runnables=()) for task in project.tasks)
    validate_project(replace(project, tasks=idle, sequences=()))


TINY = Path(__file__).parent / "fixtures" / "tiny.yaml"


def test_a_waters_project_whose_tasks_map_no_runnable_is_refused() -> None:
    # Only a specimen (explicit partitions) may have tasks that run no runnable; a WATERS project
    # (derived partitions) with tasks always maps every runnable.
    project = build_tiny_project(load_preset(TINY))
    assert project.partitions is None and project.tasks
    idle = tuple(replace(task, runnables=()) for task in project.tasks)
    with pytest.raises(GenerationError, match="every runnable must be assigned to exactly one task"):
        validate_project(replace(project, tasks=idle))


def test_a_busy_task_set_leaving_a_runnable_out_is_refused() -> None:
    project = project_with_later_kinds()
    task0, task1 = project.tasks
    tasks = (task0, replace(task1, runnables=task1.runnables[:1]))
    with pytest.raises(GenerationError, match="every runnable must be assigned to exactly one task"):
        validate_project(replace(project, tasks=tasks, sequences=()))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            lambda p: replace(p, tasks=(p.tasks[0], replace(p.tasks[1], partition="PaX"))),
            f"task {TASK1} names an undeclared partition 'PaX'",
        ),
        (
            lambda p: replace(p, tasks=(replace(p.tasks[0], partition=PARTITION1), p.tasks[1])),
            f"task {TASK0} names partition {PARTITION1} on core CORE1, not its core CORE0",
        ),
        (
            lambda p: replace(p, sequences=(replace(p.sequences[0], task="TsX"),)),
            "sequence Sq1 names an undeclared task 'TsX'",
        ),
        (
            lambda p: replace(p, sequences=(replace(p.sequences[0], steps=((C0, "write"),)),)),
            f"sequence Sq1 step {C0}.write is not a runnable of its task {TASK1}",
        ),
        (
            lambda p: replace(p, sequences=(replace(p.sequences[0], steps=()),)),
            "sequence Sq1 has no step",
        ),
        (
            lambda p: replace(p, sequences=(replace(p.sequences[0], steps=((C1, "read"), (C1, "read"))),)),
            f"sequence Sq1 names the step {C1}.read twice",
        ),
    ],
)
def test_an_invalid_task_or_sequence_is_refused_with_its_sentence(change: Any, match: str) -> None:
    with pytest.raises(GenerationError, match=match):
        validate_project(change(project_with_later_kinds()))


@pytest.mark.parametrize(
    ("link", "shown"),
    [
        (RequirementLink("swc", "SwcX"), "swc SwcX"),
        (RequirementLink("port", C0, "nope"), f"port {C0}.nope"),
        (RequirementLink("port", C0), f"port {C0}"),
        (RequirementLink("runnable", C0, "read"), f"runnable {C0}.read"),
        (RequirementLink("interface", "IfX"), "interface IfX"),
        (RequirementLink("composition", "CmpX"), "composition CmpX"),
        (RequirementLink("task", "TsX"), "task TsX"),
        (RequirementLink("hardware", "HwX"), "hardware HwX"),
        (RequirementLink("bogus", C0), f"bogus {C0}"),
    ],
)
def test_an_unresolved_link_is_refused_naming_its_site(link: RequirementLink, shown: str) -> None:
    project = project_with_later_kinds()
    rq1, *rest = project.requirements
    broken = replace(project, requirements=(replace(rq1, links=(link,)), *rest))
    with pytest.raises(GenerationError, match=f"unresolved requirement link: Rq1 {shown}$"):
        validate_project(broken)


def test_a_link_above_its_requirements_version_is_refused() -> None:
    project = project_with_later_kinds()
    rq1, *rest = project.requirements
    link = RequirementLink("swc", C0, None, 3)
    broken = replace(project, requirements=(replace(rq1, links=(link,)), *rest))
    with pytest.raises(GenerationError, match="requirement link Rq1@3 names no version of Rq1"):
        validate_project(broken)


def test_a_requirement_below_version_1_is_refused() -> None:
    project = project_with_later_kinds()
    rq1, *rest = project.requirements
    broken = replace(project, requirements=(replace(rq1, version=0, links=()), *rest))
    with pytest.raises(GenerationError, match="requirement Rq1 states version 0"):
        validate_project(broken)


def _rewritten(path: str, old: str, new: str) -> dict[str, bytes]:
    tree = dict(_later_tree())
    tree[path] = tree[path].replace(old.encode(), new.encode())
    assert tree[path] != _later_tree()[path]
    return tree


@pytest.mark.parametrize(
    ("path", "old", "new", "match"),
    [
        (f"model/tasks/{TASK0}.yaml", "Rq1@2", "Rq1@3", "unresolved satisfies entry 'Rq1@3'"),
        ("model/hardware/CPU.yaml", "Rq1@2", "RqX@1", "unresolved satisfies entry 'RqX@1'"),
        (f"model/swc/dom01/{C0}.yaml", "Rq1@1", "Rq1", "unresolved satisfies entry 'Rq1'"),
        (
            f"model/tasks/{TASK1}.yaml",
            f"partition: {PARTITION1}",
            f"partition: {PARTITION0}",
            f"task partition '{PARTITION0}' is not '{PARTITION1}'",
        ),
        (
            "model/sequences/Sq1.yaml",
            "runnable: call",
            "runnable: write",
            "rendered sequence step unresolved",
        ),
        (
            "model/sequences/Sq1.yaml",
            f"task: {TASK1}",
            f"task: {TASK0}",
            "rendered sequence step unresolved",
        ),
    ],
)
def test_the_rendered_tree_oracle_refuses_a_tampered_later_kind(path: str, old: str, new: str, match: str) -> None:
    with pytest.raises(GenerationError, match=match):
        validate_file_map(_rewritten(path, old, new), project_with_later_kinds(), task_overhead_us=_overhead())


@pytest.mark.parametrize(
    ("task", "match"),
    [
        # No step to iterate: the task reference is resolved on its own. A missing ``steps`` key is
        # the contract check's refusal, not this one.
        ("UndeclaredTask", "rendered sequence task unresolved"),
        (TASK1, "rendered sequence has no step"),
    ],
)
def test_the_rendered_tree_oracle_refuses_a_stepless_or_taskless_sequence(task: str, match: str) -> None:
    path = "model/sequences/Sq1.yaml"
    tree = dict(_later_tree())
    tree[path] = f"name: Sq1\ntask: {task}\ntitle: ''\nsteps: []\n".encode()
    with pytest.raises(GenerationError, match=f"{match}: {path}$"):
        validate_file_map(tree, project_with_later_kinds(), task_overhead_us=_overhead())
