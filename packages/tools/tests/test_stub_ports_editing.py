"""Contract ``7.6.0``: a component stub's ports through the editing protocol, and the guard's new question.

The editing protocol writes a stub through ``writer/elements.py``'s ``write_element`` and judges
the staged result with ``check-model`` (pass D); this module asks whether the protocol agrees
with the gate over the compositions fixture wired by
``external_stubs_support.add_component_stub_ports``, made a project by the ``examples/basic``
manifest:

* **the three verbs on a stub's port** — a ``create``, an ``update`` and a
  ``remove`` commit, and a declared ``interface`` (or ``measures``) on one is
  refused as ``swc.external-with-ports``, writing nothing;
* **the hint is no reference** — removing or renaming an interface a hint names
  commits, and the hint is left as it was (it simply stops resolving);
* **a mutation on a stub keeps its ports' hints** — through the protocol, and
  through every ``writer/mutations.py`` caller of ``swc_to_dict`` that can
  reach a stub;
* **the stub's generated names** — a stub emits no ``SWC-IMPLEMENTATION`` and no
  ``SWC-INTERNAL-BEHAVIOR``, so a stub name of 112 to 128 characters, which
  ``check-model`` and the exporter accept, is accepted by the protocol too;
* **a rated component converted into a stub asks** — a ``require`` port of a
  rated consumer whose rated source interface is cleared to ``None`` on an
  external owner withdraws ``safety.consumer-above-element``'s question about
  that port.

What this does **not** reach: the importer's minting and the report
(``test_stub_ports_import.py``), the diagrams, and any model shape outside the ones written
below — this is example-based evidence, not a proof over every stub-bearing
model. The converted stub's other withdrawals (its runnables, memory and
mappings) are the four existing families' questions and are not restated here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
import swcraft_tools.editing.registry as reg
import yaml
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import emitted_identifier_violations
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import check_cross_layer_consistency, check_identifier_conformance
from swcraft_tools.writer import mutations
from swcraft_tools.writer.arxml_generation import _check_component_names
from swcraft_tools.writer.elements import write_element

STUB = "swc/external/ExternalStub.yaml"
#: The member a seeded component carries to satisfy ``REQ_COMPOSITIONS_001`` (contract
#: ``11.0.0``: the link lives in the component, not in the requirement's retired ``links``).
_REQUIREMENT_LINK = "REQ_COMPOSITIONS_001@1"

#: The message prefix ``swc.external-with-ports`` keeps for a stub port that carries an interface.
PORTS_PREFIX = "SWC 'ExternalStub' declares 'external: true' and also ports;"

#: A stub name whose generated forms would not conform: ``_Implementation`` adds
#: 15 characters and ``_InternalBehavior`` 17, so any name from 112 characters on
#: overflows the 128 an AUTOSAR ``Identifier`` admits with the second, and from
#: 114 on with both. 120 is inside the band and legal on its own.
LONG_NAME = "S" * 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The wired compositions model as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "stub-ports-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    stubs.component_stub_ports_model_root(root / "model")
    return root


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]], review: Any = _approve) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "stub port edit", "operations": operations},
        project=str(project),
        review=review,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _load(project: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((project / "model" / relative).read_text(encoding="utf-8"))


def _ports(project: Path, relative: str = STUB) -> dict[str, dict[str, Any]]:
    return {port["name"]: port for port in _load(project, relative)["ports"]}


def _port_address(port: str, *field: str, element: str = "ExternalStub") -> dict[str, Any]:
    path: list[dict[str, str]] = [{"field": "ports"}, {"member": port}]
    path.extend({"field": name} for name in field)
    return {"layer": "swc", "element": element, "path": path}


def _link(requirement: str, component: str) -> dict[str, Any]:
    """The ``satisfies`` member that allocates the model's requirement to a new component."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": component,
            "path": [{"field": "satisfies"}, {"member": f"{requirement}@1"}],
        },
        "value": f"{requirement}@1",
    }


def _clean(project: Path) -> bool:
    return list(check_cross_layer_consistency(str(project / "model")).records) == []


# ---------------------------------------------------------------------------
# The three verbs on a stub's port
# ---------------------------------------------------------------------------


class TestTheProtocolWritesAStubsPort:
    def test_the_project_is_clean(self, project: Path) -> None:
        assert _clean(project)

    def test_a_bare_port_is_created(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                {
                    "operation": "create",
                    "address": _port_address("pNew"),
                    "value": {"name": "pNew", "direction": "provide"},
                }
            ],
        )

        assert response["status"] == "committed", response
        assert _ports(project)["pNew"] == {"name": "pNew", "direction": "provide"}
        assert _clean(project)

    def test_a_port_with_a_hint_is_created_and_the_hint_written(self, project: Path) -> None:
        port = {"name": "rNew", "direction": "require", "observed_interface": "ISignal"}

        response = _change_set(project, [{"operation": "create", "address": _port_address("rNew"), "value": port}])

        assert response["status"] == "committed", response
        assert _ports(project)["rNew"] == port
        assert "interface" not in _ports(project)["rNew"]

    def test_a_hint_is_set_changed_and_cleared(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                {"operation": "update", "address": _port_address("rExt", "observed_interface"), "value": "ITrigger"},
                {"operation": "update", "address": _port_address("pExt", "observed_interface"), "value": "ISignal"},
            ],
        )
        assert response["status"] == "committed", response
        assert _ports(project)["rExt"]["observed_interface"] == "ITrigger"
        assert _ports(project)["pExt"]["observed_interface"] == "ISignal"

        response = _change_set(
            project, [{"operation": "update", "address": _port_address("pExt", "observed_interface"), "value": None}]
        )
        assert response["status"] == "committed", response
        assert _ports(project)["pExt"] == {"name": "pExt", "direction": "provide"}

    def test_a_port_is_removed_with_its_delegation(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                {
                    "operation": "remove",
                    "address": {
                        "layer": "compositions",
                        "element": "Sub",
                        "path": [{"field": "delegations"}, {"member": "Sub_rExtIn_Ext"}],
                    },
                },
                {
                    "operation": "remove",
                    "address": {
                        "layer": "compositions",
                        "element": "Sub",
                        "path": [{"field": "ports"}, {"member": "rExtIn"}],
                    },
                },
                {"operation": "remove", "address": _port_address("rExt")},
            ],
        )

        assert response["status"] == "committed", response
        assert set(_ports(project)) == {"pExt"}
        assert _clean(project)

    def test_a_port_still_delegated_to_is_not_removed(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(project, [{"operation": "remove", "address": _port_address("rExt")}])

        assert response["response"] == "error", response
        assert _tree(project / "model") == before


class TestADeclaredInterfaceOnAStubsPortIsRefused:
    """``swc.external-with-ports``, restated: a stub's port never carries an ``interface`` or ``measures``."""

    def test_a_port_created_with_an_interface(self, project: Path) -> None:
        before = _tree(project / "model")
        port = {"name": "pNew", "direction": "provide", "interface": "ITrigger"}

        response = _change_set(project, [{"operation": "create", "address": _port_address("pNew"), "value": port}])

        assert response["error"] == "consistency-violation", response
        assert PORTS_PREFIX in response["message"]
        assert response["addresses"] == [{"layer": "swc", "element": "ExternalStub", "path": [{"field": "ports"}]}]
        assert _tree(project / "model") == before

    def test_an_interface_set_on_an_existing_port(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project, [{"operation": "update", "address": _port_address("pExt", "interface"), "value": "ITrigger"}]
        )

        assert response["error"] == "consistency-violation", response
        assert PORTS_PREFIX in response["message"]
        assert _tree(project / "model") == before

    def test_measures_on_a_stubs_port(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project,
            [
                {
                    "operation": "create",
                    "address": {
                        **_port_address("rExt", "measures"),
                        "path": [*_port_address("rExt", "measures")["path"], {"key": "trigger"}],
                    },
                    "value": "e2e",
                }
            ],
        )

        assert response["error"] == "consistency-violation", response
        assert PORTS_PREFIX in response["message"]
        assert _tree(project / "model") == before

    def test_the_hint_is_no_interface(self, project: Path) -> None:
        """The control: the same value as a hint commits, and nothing reads it as a declaration."""
        response = _change_set(
            project,
            [{"operation": "update", "address": _port_address("rExt", "observed_interface"), "value": "ISignal"}],
        )

        assert response["status"] == "committed", response
        assert "interface" not in _ports(project)["rExt"]


# ---------------------------------------------------------------------------
# The hint is no reference
# ---------------------------------------------------------------------------

#: An interface only a hint names, so removing or renaming it touches no declared reference.
OBSERVED = {
    "name": "IObserved",
    "kind": "sender_receiver",
    "data_elements": [{"name": "value", "implementation_type": "uint8"}],
}


@pytest.fixture
def hinted(project: Path) -> Path:
    """The project with ``IObserved`` declared and named by ``rExt``'s hint alone."""
    stubs.put(project / "model", "interfaces/IObserved.yaml", OBSERVED)

    def hint(document: dict[str, Any]) -> None:
        (port,) = [entry for entry in document["ports"] if entry["name"] == "rExt"]
        port["observed_interface"] = "IObserved"

    stubs.change(project / "model", STUB, hint)
    assert _clean(project)
    return project


class TestAnInterfaceAHintNamesIsNotHeld:
    """No rule reads the hint, so nothing refuses the interface it names going away."""

    def test_its_removal_commits_and_leaves_the_hint(self, hinted: Path) -> None:
        stub_before = (hinted / "model" / STUB).read_bytes()

        response = _change_set(
            hinted, [{"operation": "remove", "address": {"layer": "interfaces", "element": "IObserved"}}]
        )

        assert response["status"] == "committed", response
        assert not (hinted / "model" / "interfaces" / "IObserved.yaml").exists()
        assert (hinted / "model" / STUB).read_bytes() == stub_before
        assert _clean(hinted)

    def test_its_rename_commits_and_leaves_the_hint(self, hinted: Path) -> None:
        stub_before = (hinted / "model" / STUB).read_bytes()
        renamed = {**OBSERVED, "name": "IObservedRenamed"}

        response = _change_set(
            hinted,
            [
                {"operation": "remove", "address": {"layer": "interfaces", "element": "IObserved"}},
                {
                    "operation": "create",
                    "address": {"layer": "interfaces", "element": renamed["name"]},
                    "value": renamed,
                },
            ],
        )

        assert response["status"] == "committed", response
        assert (hinted / "model" / STUB).read_bytes() == stub_before
        assert _ports(hinted)["rExt"]["observed_interface"] == "IObserved"
        assert _clean(hinted)

    def test_a_declared_reference_is_still_held(self, hinted: Path) -> None:
        """The control: an interface a declared port names is refused as a broken reference."""
        before = _tree(hinted / "model")

        response = _change_set(
            hinted, [{"operation": "remove", "address": {"layer": "interfaces", "element": "ITrigger"}}]
        )

        assert response["error"] == "broken-reference", response
        assert _tree(hinted / "model") == before


# ---------------------------------------------------------------------------
# A mutation on a stub keeps its ports' hints
# ---------------------------------------------------------------------------


class TestAMutationOnAStubKeepsTheHints:
    def test_a_protocol_update_elsewhere_on_the_stub(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "ExternalStub", "path": [{"field": "description"}]},
                    "value": "Declared by another file of the delivery",
                }
            ],
        )

        assert response["status"] == "committed", response
        assert _ports(project)["pExt"] == {"name": "pExt", "direction": "provide", "observed_interface": "ITrigger"}

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda root: mutations.add_port(root, "ExternalStub", "pMore", "provide", "ITrigger"), id="add_port"
            ),
            pytest.param(lambda root: mutations.add_runnable(root, "ExternalStub", "Run"), id="add_runnable"),
            pytest.param(
                lambda root: mutations.add_timing_event(root, "ExternalStub", "Existing", 0.01), id="add_timing_event"
            ),
            pytest.param(lambda root: mutations.add_init_event(root, "ExternalStub", "Existing"), id="add_init_event"),
            pytest.param(
                lambda root: mutations.set_swc_memory_budget(root, "ExternalStub", "RAM0", 64),
                id="set_swc_memory_budget",
            ),
            pytest.param(
                lambda root: mutations.unset_swc_memory_budget(root, "ExternalStub", "RAM0"),
                id="unset_swc_memory_budget",
            ),
        ],
    )
    def test_every_mutation_that_rewrites_the_file(self, project: Path, mutate: Any) -> None:
        """Each ``swc_to_dict`` caller that can reach an existing stub keeps the hints through its rewrite.

        What each adds may be a finding; ``check-model`` judges that, these narrow writers do not.
        ``create_swc`` writes a new file and is not asked.
        """
        model_root = project / "model"
        stubs.put(
            model_root,
            "hardware/RAM0.yaml",
            {
                "name": "RAM0",
                "kind": "memory",
                "type": "RAM",
                "size": 4096,
                "accessible": ["CORE0"],
                "threshold": 0.8,
                "enforcement": "strict",
            },
        )

        def seed(document: dict[str, Any]) -> None:
            document["memory"] = {"RAM0": 8}
            document["runnables"] = [{"name": "Existing"}]

        stubs.change(model_root, STUB, seed)

        mutate(str(model_root))

        assert _ports(project)["pExt"] == {"name": "pExt", "direction": "provide", "observed_interface": "ITrigger"}
        assert _ports(project)["rExt"] == {"name": "rExt", "direction": "require"}


# ---------------------------------------------------------------------------
# A stub's generated names are not charged
# ---------------------------------------------------------------------------


def _long_stub(*, external: bool) -> dict[str, Any]:
    if external:
        return {
            "name": LONG_NAME,
            "type": "application",
            "external": True,
            "external_ref": f"/Foreign/{LONG_NAME}",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        }
    return {"name": LONG_NAME, "type": "application", "ports": [], "runnables": []}


def _create_long(project: Path, *, external: bool) -> dict[str, Any]:
    return _change_set(
        project,
        [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": LONG_NAME},
                "value": {**_long_stub(external=external), "group": "external"},
            },
            _link("REQ_COMPOSITIONS_001", LONG_NAME),
        ],
    )


class TestAStubsGeneratedNamesAreNotCharged:
    """A stub emits neither generated form, so no guard may count it: the protocol agrees with ``check-model``."""

    def test_the_name_is_inside_the_band(self) -> None:
        assert len(LONG_NAME) <= MAX_IDENTIFIER_LENGTH
        assert len(LONG_NAME) + len("_Implementation") > MAX_IDENTIFIER_LENGTH

    def test_check_model_and_the_exporter_accept_it(self, project: Path) -> None:
        stubs.put(project / "model", f"swc/external/{LONG_NAME}.yaml", _long_stub(external=True))
        stubs.change(
            project / "model",
            f"swc/external/{LONG_NAME}.yaml",
            lambda document: document.setdefault("satisfies", []).append(_REQUIREMENT_LINK),
        )

        assert _clean(project)
        assert check_identifier_conformance(str(project / "model")).records == ()
        _check_component_names([SWComponent.model_validate(_long_stub(external=True))])

    def test_the_protocol_creates_it(self, project: Path) -> None:
        response = _create_long(project, external=True)

        assert response["status"] == "committed", response
        assert (project / "model" / "swc" / "external" / f"{LONG_NAME}.yaml").is_file()
        assert _clean(project)

    def test_a_declared_component_of_that_name_is_still_refused(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _create_long(project, external=False)

        assert response["error"] == "invalid-identifier", response
        assert "_Implementation" in response["message"]
        assert _tree(project / "model") == before

    def test_the_element_writers_guard(self, tmp_path: Path) -> None:
        (tmp_path / "swc").mkdir()
        write_element(
            str(tmp_path), "swc/external/Long.yaml", SWComponent.model_validate(_long_stub(external=True)), create=True
        )
        assert (tmp_path / "swc" / "external" / "Long.yaml").is_file()

        with pytest.raises(ValueError, match="generated SWC-IMPLEMENTATION name"):
            write_element(
                str(tmp_path), "swc/core/Long.yaml", SWComponent.model_validate(_long_stub(external=False)), create=True
            )
        assert not (tmp_path / "swc" / "core" / "Long.yaml").exists()

    def test_the_pre_materialization_guard(self) -> None:
        def staged(external: bool) -> StagedModel:
            component = SWComponent.model_validate(_long_stub(external=external))
            return StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})

        assert emitted_identifier_violations(staged(external=True)) == ()
        violations = emitted_identifier_violations(staged(external=False))
        assert len(violations) == 2
        assert [
            f"{LONG_NAME}{suffix}" in violation.message
            for violation in violations
            for suffix in (
                "_Implementation",
                "_InternalBehavior",
            )
        ] == [True, False, False, True]

    def test_an_illegal_stub_name_is_still_refused_and_not_said_to_be_emitted(self) -> None:
        stub = {**_long_stub(external=True), "name": "S" * (MAX_IDENTIFIER_LENGTH + 1)}
        component = SWComponent.model_validate(stub)

        (violation,) = emitted_identifier_violations(
            StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})
        )
        assert "is emitted as an AUTOSAR SHORT-NAME" not in violation.message
        assert "although a stub is emitted as no SHORT-NAME" in violation.message


def _declared(name: str) -> dict[str, Any]:
    """A declared component; it states ``no_support`` (a component without it is refused)."""
    return {
        "name": name,
        "type": "application",
        "ports": [],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    }


def _stub(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "application",
        "external": True,
        "external_ref": f"/Foreign/{name}",
        "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
    }


def _create_component(project: Path, component: dict[str, Any], group: str) -> dict[str, Any]:
    return _change_set(
        project,
        [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": component["name"]},
                "value": {**component, "group": group},
            },
            _link("REQ_COMPOSITIONS_001", component["name"]),
        ],
    )


def _seed_component(project: Path, component: dict[str, Any], group: str) -> None:
    stubs.put(project / "model", f"swc/{group}/{component['name']}.yaml", component)
    stubs.change(
        project / "model",
        f"swc/{group}/{component['name']}.yaml",
        lambda document: document.setdefault("satisfies", []).append(_REQUIREMENT_LINK),
    )


class TestTheReservedImplementationNameAsksForAnEmittingBase:
    """``{A}_Implementation`` collides only with a component that emits it, and a stub emits none.

    The write path's reserved-name guard asks for an emitting base, as ``check-model``
    (``name.swc-implementation-collision``) and the exporter do.
    """

    def test_a_declared_component_named_for_a_stubs_implementation_commits(self, project: Path) -> None:
        response = _create_component(project, _declared("ExternalStub_Implementation"), "core")

        assert response["status"] == "committed", response
        assert _clean(project)

    def test_a_stub_beside_an_existing_implementation_name_commits(self, project: Path) -> None:
        _seed_component(project, _declared("Lone_Implementation"), "core")
        assert _clean(project)

        response = _create_component(project, _stub("Lone"), "external")

        assert response["status"] == "committed", response
        assert _clean(project)

    def test_the_mutation_path_agrees(self, project: Path) -> None:
        mutations.create_swc(str(project / "model"), "ExternalStub_Implementation", "core")

        assert (project / "model" / "swc" / "core" / "ExternalStub_Implementation.yaml").is_file()

    def test_the_guard_reads_the_flag_check_model_loads(self, project: Path) -> None:
        """``external: "true"`` loads as a stub (pydantic's lax ``bool``), so the guard must call it one too."""
        _seed_component(project, {**_stub("Lax"), "external": "true"}, "external")
        (lax,) = [swc for swc in read_swc_model(str(project / "model")) if swc.name == "Lax"]
        assert lax.external is True

        mutations.create_swc(str(project / "model"), "Lax_Implementation", "core")

        assert (project / "model" / "swc" / "core" / "Lax_Implementation.yaml").is_file()

    def test_a_declared_twin_of_a_declared_component_is_still_refused(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _create_component(project, _declared("Beta_Implementation"), "core")

        assert response["response"] == "error", response
        assert "collides with the auto-generated SWC-IMPLEMENTATION name for 'Beta'" in response["message"]
        assert _tree(project / "model") == before

    def test_a_stub_named_for_a_declared_components_implementation_is_still_refused(self, project: Path) -> None:
        """``check-model`` reports it, so the protocol refuses it: the name is judged, the stub emits nothing."""
        before = _tree(project / "model")

        response = _create_component(project, _stub("Beta_Implementation"), "external")

        assert response["response"] == "error", response
        assert _tree(project / "model") == before

        _seed_component(project, _stub("Beta_Implementation"), "external")
        assert "name.swc-implementation-collision" in [
            record.rule for record in check_cross_layer_consistency(str(project / "model")).records
        ]

    def test_a_declared_component_beside_an_existing_implementation_name_is_still_refused(self, project: Path) -> None:
        _seed_component(project, _stub("Lone_Implementation"), "external")
        before = _tree(project / "model")

        response = _create_component(project, _declared("Lone"), "core")

        assert response["response"] == "error", response
        assert _tree(project / "model") == before


# ---------------------------------------------------------------------------
# A rated component converted into a stub
# ---------------------------------------------------------------------------


@pytest.fixture
def rated(tmp_path: Path) -> Path:
    """``examples/basic`` with ``IExample.value`` and both components at ``B``, and a third rated consumer.

    ``RatedConsumer`` carries nothing a stub may not carry but its ``require`` port's interface
    and one memory budget, so a change set can convert it into a stub by fields. Clean by
    construction.
    """
    project = fixtures.build_fixture(tmp_path / "rated-project", "read-swc-element")
    model_root = project / "model"
    stubs.change(model_root, "interfaces/IExample.yaml", lambda document: document["data_elements"][0].update(asil="B"))
    for relative in ("swc/example/ExampleSWC.yaml", "swc/example/ExampleConsumerSWC.yaml"):
        stubs.change(model_root, relative, lambda document: document.update(asil="B"))
    stubs.add_rated_consumer(model_root)
    assert _clean(project)
    return project


def _convert(*, interface: Any = None) -> list[dict[str, Any]]:
    """``RatedConsumer`` made a stub by fields: the triple, its budget dropped, its port's interface set."""
    return [
        *stubs.convert_rated_consumer(),
        {
            "operation": "update",
            "address": _port_address("rIn", "interface", element="RatedConsumer"),
            "value": interface,
        },
    ]


def _questions(project: Path, operations: list[dict[str, Any]], *, safety_approved: bool = True) -> tuple[Any, Any]:
    asked: list[list[tuple[str, str, str, Any, Any]]] = []
    lines: list[str] = []

    def review(proposal: Any) -> ApprovalDecision:
        asked.append(
            [
                (change.kind.value, change.resource, change.field, change.old, change.new)
                for change in proposal.safety.changes
            ]
        )
        lines.extend(change.describe() for change in proposal.safety.changes)
        return ApprovalDecision(
            change_set_id=proposal.change_set_id,
            baseline=proposal.baseline,
            approved=True,
            safety_approved=safety_approved,
        )

    return _change_set(project, operations, review), (asked, lines)


_CLEARED = stubs.RATED_CONSUMER_CLEARED


class TestARatedComponentConvertedIntoAStubAsks:
    """Clearing a rated port's interface on a converted stub asks ``safety.consumer-above-element``'s question."""

    def test_it_asks_one_question_and_commits_on_both_answers(self, rated: Path) -> None:
        response, (asked, _lines) = _questions(rated, _convert())

        assert response["status"] == "committed", response
        assert asked == [[_CLEARED]]
        assert _ports(rated, stubs.RATED_CONSUMER_FILE)["rIn"] == {"name": "rIn", "direction": "require"}

    def test_a_declined_answer_abandons_and_writes_nothing(self, rated: Path) -> None:
        before = _tree(rated / "model")

        response, (asked, _lines) = _questions(rated, _convert(), safety_approved=False)

        assert asked == [[_CLEARED]]
        assert response.get("status") != "committed", response
        assert _tree(rated / "model") == before

    def test_the_prompt_line_reads_none_rather_than_a_python_value(self, rated: Path) -> None:
        _response, (_asked, lines) = _questions(rated, _convert())

        assert len(lines) == 1
        assert lines[0].startswith("'RatedConsumer.rIn': interface IExample -> none — "), lines[0]

    def test_an_unrated_consumer_is_not_asked(self, rated: Path) -> None:
        stubs.change(rated / "model", stubs.RATED_CONSUMER_FILE, lambda document: document.pop("asil"))
        assert _clean(rated)

        response, (asked, _lines) = _questions(rated, _convert())

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_a_source_carrying_no_rated_element_is_not_asked(self, rated: Path) -> None:
        stubs.change(
            rated / "model", "interfaces/IExample.yaml", lambda document: document["data_elements"][0].pop("asil")
        )
        assert _clean(rated)

        response, (asked, _lines) = _questions(rated, _convert())

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_a_declared_component_cannot_clear_the_interface_at_all(self, rated: Path) -> None:
        """The owner is external or the change set never reaches the question: pass D refuses first."""
        before = _tree(rated / "model")

        response, (asked, _lines) = _questions(
            rated,
            [
                {
                    "operation": "update",
                    "address": _port_address("rIn", "interface", element="RatedConsumer"),
                    "value": None,
                }
            ],
        )

        assert response["error"] == "broken-reference", response
        assert asked == []
        assert _tree(rated / "model") == before

    def test_a_stub_created_new_asks_nothing(self, rated: Path) -> None:
        """Declaring a stub with bare ports withdraws no protection: nothing was judged before."""
        stub = {
            "name": "NewStub",
            "type": "application",
            "external": True,
            "external_ref": "/Foreign/NewStub",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "asil": "B",
            "ports": [{"name": "rIn", "direction": "require", "observed_interface": "IExample"}],
            "group": "example",
        }

        response, (asked, _lines) = _questions(
            rated,
            [
                {"operation": "create", "address": {"layer": "swc", "element": "NewStub"}, "value": stub},
                _link("REQ_EXAMPLE_001", "NewStub"),
            ],
        )

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_the_hint_is_not_read_by_the_guard(self, rated: Path) -> None:
        """Keeping the source as a hint does not answer the question: the hint is no declaration."""
        operations = [
            *_convert(),
            {
                "operation": "update",
                "address": _port_address("rIn", "observed_interface", element="RatedConsumer"),
                "value": "IExample",
            },
        ]

        response, (asked, _lines) = _questions(rated, operations)

        assert response["status"] == "committed", response
        assert asked == [[_CLEARED]]
