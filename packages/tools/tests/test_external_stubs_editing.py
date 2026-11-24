"""The two stub kinds through the editing protocol, and the authorization moves they enable.

A stub (an ``external_interface`` in ``interfaces/`` or an ``external_type`` in
``data_types/implementation/``) is a declared element emitted as no ARXML element. Over one real, valid
project (the ``read-swc-element`` vector) this module proves:

* **the four verbs, per layer**: create, update, rename (remove + create in one change set) and remove of
  each stub kind commit the derived serializer's bytes;
* **the stub's own name is ``check-model``'s**: the writer writes an illegal one and pass D refuses it;
* **the two authorization moves**: a rated consumer's ``require`` port re-pointed to a stub asks one
  ``LOWER_ALLOCATION`` on the port's ``interface`` (the safety rule abstains on a stub, so the move would close
  a ``safety.consumer-above-element`` finding while the relation survives); a rated interface replaced by a
  same-named stub asks one per rated data element and, through the protocol, is not one change set
  (``immutable-field`` or ``identity-conflict`` refuse it before the guard);
* **the move under another name**: a rated port removed and created again on a stub, or a connection
  rewired onto a stub port, asks when a connection survives the change (an assembly connector or a
  ``require`` delegation keeping its name, provider side and consumer component while its consumer end moves
  onto the stub); the silences beside them are pinned.

What this does **not** reach: a stub's reference pair against the target it names, the provider side of a
stubbed port, and any model shape beyond the ones written below; this is example-based evidence.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
import yaml
from conftest import BASE_VECTOR, model_element_path, model_element_storage_name
from swcraft_tools.authorization import SafetyChangeKind
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.review import render_safety_review
from swcraft_tools.editing.safety_guard import safety_verdict
from swcraft_tools.schema import ExternalDataType, ExternalInterface
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import elements

# The safety guard suite's own projection helpers: the delta a host would review
# over a copy of a tree, and the findings the committed safety fixture mints.
from test_editing_authorization import _delta_over, _findings_after, _safety_fixture_delta

_SR_REF = "/Foreign/PortInterfaces/IForeign"
_TYPE_REF = "/Foreign/ImplementationDataTypes/ForeignT"

#: One stub per layer, every optional field set.
STUBS: dict[str, dict[str, str]] = {
    "interfaces": {
        "name": "IForeign",
        "kind": "external_interface",
        "external_ref": _SR_REF,
        "external_dest": "SENDER-RECEIVER-INTERFACE",
        "description": "Declared by another file of the delivery",
    },
    "implementation_types": {
        "name": "ForeignT",
        "kind": "external_type",
        "external_ref": _TYPE_REF,
        "external_dest": "IMPLEMENTATION-DATA-TYPE",
        "description": "Declared by another file of the delivery",
    },
}

#: The bytes the derived serializer writes for each: declaration order, one key per line.
EXPECTED_BYTES: dict[str, bytes] = {
    "interfaces": (
        b"name: IForeign\nkind: external_interface\nexternal_ref: /Foreign/PortInterfaces/IForeign\n"
        b"external_dest: SENDER-RECEIVER-INTERFACE\ndescription: Declared by another file of the delivery\n"
    ),
    "implementation_types": (
        b"name: ForeignT\nkind: external_type\nexternal_ref: /Foreign/ImplementationDataTypes/ForeignT\n"
        b"external_dest: IMPLEMENTATION-DATA-TYPE\ndescription: Declared by another file of the delivery\n"
    ),
}

LAYERS = tuple(STUBS)


# Helpers


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fixtures.build_fixture(tmp_path / "stub-editing-project", BASE_VECTOR)


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(
    project: Path, operations: list[dict[str, Any]], review: Any = _approve, notes: Any = None
) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "stub edit", "operations": operations},
        project=str(project),
        review=review,
        notes=notes,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _seed_stub(project: Path, layer: str) -> None:
    stub = STUBS[layer]
    stubs.put(project / "model", model_element_storage_name(stub["name"], layer=layer), stub)


# The four verbs


class TestTheProtocolWritesAStub:
    """Each verb, per layer, commits; a create raised before the stub classes were classified."""

    @pytest.mark.parametrize("layer", LAYERS)
    def test_a_create_commits_and_writes_the_derived_bytes(self, project: Path, layer: str) -> None:
        stub = STUBS[layer]
        response = _change_set(
            project,
            [{"operation": "create", "address": {"layer": layer, "element": stub["name"]}, "value": stub}],
        )

        assert response["status"] == "committed", response
        assert model_element_path(project / "model", stub["name"], layer=layer).read_bytes() == EXPECTED_BYTES[layer]

    @pytest.mark.parametrize("layer", LAYERS)
    def test_an_update_of_the_reference_pair_commits(self, project: Path, layer: str) -> None:
        _seed_stub(project, layer)
        stub = STUBS[layer]
        moved = stub["external_ref"].replace("/Foreign/", "/Elsewhere/")

        response = _change_set(
            project,
            [
                {
                    "operation": "update",
                    "address": {"layer": layer, "element": stub["name"], "path": [{"field": "external_ref"}]},
                    "value": moved,
                }
            ],
        )

        assert response["status"] == "committed", response
        written = yaml.safe_load(
            model_element_path(project / "model", stub["name"], layer=layer).read_text(encoding="utf-8")
        )
        assert written == {**stub, "external_ref": moved}

    @pytest.mark.parametrize("layer", LAYERS)
    def test_a_rename_commits_and_moves_the_file(self, project: Path, layer: str) -> None:
        _seed_stub(project, layer)
        stub = STUBS[layer]
        renamed = {**stub, "name": f"{stub['name']}Renamed"}

        response = _change_set(
            project,
            [
                {"operation": "remove", "address": {"layer": layer, "element": stub["name"]}},
                {"operation": "create", "address": {"layer": layer, "element": renamed["name"]}, "value": renamed},
            ],
        )

        assert response["status"] == "committed", response
        assert not model_element_path(project / "model", stub["name"], layer=layer).exists()
        path = model_element_path(project / "model", renamed["name"], layer=layer)
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written == renamed
        # The rename carries the old file's text: the seed is in the writer's
        # form, so only its `name:` line moves.
        old_line = f"name: {stub['name']}\n".encode()
        assert path.read_bytes() == EXPECTED_BYTES[layer].replace(old_line, f"name: {renamed['name']}\n".encode(), 1)

    @pytest.mark.parametrize("layer", LAYERS)
    def test_a_remove_commits_and_deletes_the_file(self, project: Path, layer: str) -> None:
        _seed_stub(project, layer)
        stub = STUBS[layer]

        response = _change_set(project, [{"operation": "remove", "address": {"layer": layer, "element": stub["name"]}}])

        assert response["status"] == "committed", response
        assert not model_element_path(project / "model", stub["name"], layer=layer).exists()

    @pytest.mark.parametrize("model", [ExternalInterface, ExternalDataType])
    def test_both_classes_are_classified_as_emitting_no_short_name(self, model: type) -> None:
        assert model not in elements._NAME_GUARDS
        assert issubclass(model, elements._NO_EMITTED_SHORT_NAME)


class TestTheStubsOwnNameIsCheckModels:
    """The writer does not judge a stub's name; pass D does, under the identifier rule."""

    @pytest.mark.parametrize("layer", LAYERS)
    def test_the_writer_writes_an_illegal_stub_name(self, tmp_path: Path, layer: str) -> None:
        root = tmp_path / "model"
        root.mkdir()
        stub = {**STUBS[layer], "name": "not an identifier"}
        model = ExternalInterface if layer == "interfaces" else ExternalDataType

        elements.write_element(str(root), f"{layer}/not an identifier.yaml", model.model_validate(stub), create=True)

        assert (root / layer / "not an identifier.yaml").is_file()

    @pytest.mark.parametrize("layer", LAYERS)
    def test_the_protocol_refuses_it_at_pass_d_and_writes_nothing(self, project: Path, layer: str) -> None:
        stub = {**STUBS[layer], "name": "not an identifier"}
        before = _tree(project / "model")

        response = _change_set(
            project,
            [{"operation": "create", "address": {"layer": layer, "element": stub["name"]}, "value": stub}],
        )

        assert response["response"] == "error", response
        assert response["error"] == "invalid-identifier", response
        assert _tree(project / "model") == before

    @pytest.mark.parametrize("layer", LAYERS)
    def test_the_refusal_does_not_say_a_stub_is_emitted_as_a_short_name(self, project: Path, layer: str) -> None:
        # A stub is emitted as no SHORT-NAME, so the refusal must not say the rule
        # is checked on the one the exporter emits.
        stub = {**STUBS[layer], "name": "not an identifier"}

        response = _change_set(
            project,
            [{"operation": "create", "address": {"layer": layer, "element": stub["name"]}, "value": stub}],
        )

        assert "the SHORT-NAME the exporter emits, not on the declared name" not in response["message"]
        assert (
            "an external stub is emitted as no SHORT-NAME, and its own name is held to the same rule"
            in (response["message"])
        )

    @pytest.mark.parametrize("layer", LAYERS)
    def test_beside_a_name_the_writer_refuses_the_stub_is_not_said_to_be_emitted(
        self, project: Path, layer: str
    ) -> None:
        # The writer refuses the component's name, so pass D's identifier rule is
        # asked of the in-memory staged tree and reports the stub's name beside it.
        stub = {**STUBS[layer], "name": "not an identifier"}
        component = {"name": "bad swc", "type": "application", "ports": [], "runnables": [], "group": "example"}

        response = _change_set(
            project,
            [
                {"operation": "create", "address": {"layer": "swc", "element": "bad swc"}, "value": component},
                {"operation": "create", "address": {"layer": layer, "element": stub["name"]}, "value": stub},
            ],
        )

        assert response["error"] == "invalid-identifier", response
        (about_the_stub,) = [
            entry["message"]
            for entry in (response, *response.get("additional", ()))
            if entry["addresses"] == [{"layer": layer, "element": "not an identifier"}]
        ]
        assert "external" in about_the_stub and "stub name 'not an identifier'" in about_the_stub
        assert "is emitted as an AUTOSAR SHORT-NAME" not in about_the_stub
        assert "although a stub is emitted as no SHORT-NAME" in about_the_stub


class TestTheWritePathNameGuardsSkipAStub:
    """A stub holds no SHORT-NAME, so a write-path name guard must not count it as one.

    An enum ``Mode`` beside a type stub ``Mode_CompuMethod``, and an interface ``INew`` with data
    element ``e`` beside an interface stub ``INew_e_Init``, are consistent models; the write-path
    guards must agree.
    """

    def test_an_enum_beside_a_stub_named_like_its_compu_method_commits(self, project: Path) -> None:
        stubs.put(
            project / "model",
            "data_types/implementation/Mode_CompuMethod.yaml",
            {**STUBS["implementation_types"], "name": "Mode_CompuMethod"},
        )
        enum = {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}]}

        response = _change_set(
            # An enum is an element of `application_types`.
            project,
            [{"operation": "create", "address": {"layer": "application_types", "element": "Mode"}, "value": enum}],
        )

        assert response["status"] == "committed", response
        assert list(check_cross_layer_consistency(str(project / "model")).records) == []

    def test_an_interface_beside_a_stub_named_like_its_init_constant_commits(self, project: Path) -> None:
        stubs.put(project / "model", "interfaces/INew_e_Init.yaml", {**STUBS["interfaces"], "name": "INew_e_Init"})
        interface = {
            "name": "INew",
            "kind": "sender_receiver",
            "data_elements": [{"name": "e", "implementation_type": "float32"}],
        }

        response = _change_set(
            project,
            [{"operation": "create", "address": {"layer": "interfaces", "element": "INew"}, "value": interface}],
        )

        assert response["status"] == "committed", response
        assert list(check_cross_layer_consistency(str(project / "model")).records) == []


# The two authorization moves


def _rate(project: Path, *, consumer: str | None = "B", element: str | None = "B") -> None:
    """``examples/basic`` with a level on both components and on ``IExample.value``; clean by construction."""
    model_root = project / "model"

    def rate_element(document: dict[str, Any]) -> None:
        if element is not None:
            document["data_elements"][0]["asil"] = element

    stubs.change(model_root, "interfaces/IExample.yaml", rate_element)
    stubs.change(model_root, "swc/example/ExampleSWC.yaml", lambda document: document.update(asil="B"))
    if consumer is not None:
        stubs.change(model_root, "swc/example/ExampleConsumerSWC.yaml", lambda document: document.update(asil=consumer))


def _repoint_consumer(interface: str) -> list[dict[str, Any]]:
    return [
        {
            "operation": "update",
            "address": {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "interface"}],
            },
            "value": interface,
        }
    ]


def _create_stub() -> list[dict[str, Any]]:
    stub = STUBS["interfaces"]
    return [{"operation": "create", "address": {"layer": "interfaces", "element": stub["name"]}, "value": stub}]


def _questions(project: Path, operations: list[dict[str, Any]], *, safety_approved: bool = True) -> tuple[Any, Any]:
    """The response and the safety questions one change set was asked, as tuples."""
    response, asked, _notes, _rendered = _witnessed(project, operations, safety_approved=safety_approved)
    return response, asked


_REPOINTED = (SafetyChangeKind.LOWER_ALLOCATION.value, "ExampleConsumerSWC.rIn", "interface", "IExample", "IForeign")


class TestARatedConsumerRePointedToAStubAsks:
    """Move 1: a rated consumer re-pointed to a stub asks one question."""

    def test_the_baseline_is_clean(self, project: Path) -> None:
        _rate(project)
        assert list(check_cross_layer_consistency(str(project / "model")).records) == []

    def test_it_asks_one_question_and_commits_on_both_answers(self, project: Path) -> None:
        _rate(project)

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign"))

        assert response["status"] == "committed", response
        assert asked == [[_REPOINTED]]

    def test_a_declined_answer_abandons_and_writes_nothing(self, project: Path) -> None:
        _rate(project)
        before = _tree(project / "model")

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign"), safety_approved=False)

        assert asked == [[_REPOINTED]]
        assert response.get("status") != "committed", response
        assert _tree(project / "model") == before

    def test_the_prompt_line_names_the_port_and_both_interfaces(self, project: Path) -> None:
        _rate(project)
        lines: list[str] = []

        def review(proposal: Any) -> ApprovalDecision:
            lines.extend(change.describe() for change in proposal.safety.changes)
            return _approve(proposal)

        _change_set(project, _create_stub() + _repoint_consumer("IForeign"), review)

        assert len(lines) == 1
        assert lines[0].startswith("'ExampleConsumerSWC.rIn': interface IExample -> IForeign — "), lines[0]

    def test_on_the_safety_fixture_it_closes_a_finding_and_asks(self, tmp_path: Path) -> None:
        """The shape a structural move must not have: the finding closes while the relation survives."""

        def edit(root: Path) -> None:
            stubs.put(root, "interfaces/IForeign.yaml", STUBS["interfaces"])

            def repoint(document: dict[str, Any]) -> None:
                for port in document["ports"]:
                    if port["name"] == "rPlain":
                        port["interface"] = "IForeign"

            stubs.change(root, "swc/safety/PlainWorker.yaml", repoint)

        before = Counter(_findings_after(tmp_path / "before", lambda root: None))
        after = Counter(_findings_after(tmp_path / "after", edit))
        delta, baseline, staged = _safety_fixture_delta(tmp_path / "delta", edit)

        verdict = safety_verdict(delta, baseline, staged=staged)

        assert dict(before - after) == {"safety.consumer-above-element": 1}
        assert not after - before
        assert [(c.kind.value, c.resource, c.field, c.old, c.new) for c in verdict.changes] == [
            (SafetyChangeKind.LOWER_ALLOCATION.value, "PlainWorker.rPlain", "interface", "IPlainSignals", "IForeign")
        ]


class TestTheOwnersOtherBranches:
    """The owner's remaining conditions, each met one other way than the cases above."""

    def test_a_stub_already_in_the_baseline_asks(self, project: Path) -> None:
        _rate(project)
        _seed_stub(project, "interfaces")

        response, asked = _questions(project, _repoint_consumer("IForeign"))

        assert response["status"] == "committed", response
        assert asked == [[_REPOINTED]]

    def test_a_consumer_rated_only_by_allocation_asks(self, project: Path) -> None:
        """No ``asil`` on the consumer: its effective level is the requirement's."""
        _rate(project, consumer=None)
        stubs.change(project / "model", "requirements/REQ_EXAMPLE_001.yaml", lambda document: document.update(asil="B"))
        assert list(check_cross_layer_consistency(str(project / "model")).records) == []

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign"))

        assert response["status"] == "committed", response
        assert asked == [[_REPOINTED]]

    def test_a_port_flipped_to_provide_in_the_same_change_set_is_not_this_question(self, project: Path) -> None:
        """A ``provide`` port names no element the consumer rule reads: nothing withdrawn, nothing asked.

        The connector goes too: two provide ends are the connector rules' refusal."""
        _rate(project)
        flip = [
            {
                "operation": "update",
                "address": {
                    "layer": "swc",
                    "element": "ExampleConsumerSWC",
                    "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "direction"}],
                },
                "value": "provide",
            },
            {"operation": "remove", "address": {"layer": "connectors", "element": "Example_to_Consumer"}},
        ]

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign") + flip)

        assert response["status"] == "committed", response
        assert _REPOINTED not in [question for questions in asked for question in questions]


class TestWhatTheRePointDoesNotAsk:
    """The owner's three conditions, each withdrawn once: nothing to protect, nothing asked."""

    def test_an_unrated_consumer_is_not_asked(self, project: Path) -> None:
        _rate(project, consumer=None)

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign"))

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_a_source_carrying_no_rated_element_is_not_asked(self, project: Path) -> None:
        _rate(project, element=None)

        response, asked = _questions(project, _create_stub() + _repoint_consumer("IForeign"))

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_a_port_already_on_a_stub_re_pointed_to_another_is_not_asked(self, project: Path) -> None:
        _rate(project)
        _seed_stub(project, "interfaces")
        stubs.put(project / "model", "interfaces/IForeignToo.yaml", {**STUBS["interfaces"], "name": "IForeignToo"})
        stubs.change(
            project / "model",
            "swc/example/ExampleConsumerSWC.yaml",
            lambda document: document["ports"][0].update(interface="IForeign"),
        )

        response, asked = _questions(project, _repoint_consumer("IForeignToo"))

        assert response["status"] == "committed", response
        assert asked == [[]]

    def test_a_rated_consumer_re_pointed_to_a_declared_interface_is_not_this_question(self, project: Path) -> None:
        """A declared target stays visible to the consumer rule, which judges it; nothing is withdrawn."""
        _rate(project)
        stubs.put(
            project / "model",
            "interfaces/IExampleToo.yaml",
            {
                "name": "IExampleToo",
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": "float32", "asil": "B"}],
            },
        )

        response, asked = _questions(project, _repoint_consumer("IExampleToo"))

        assert response["status"] == "committed", response
        assert asked == [[]]


class TestARatedInterfaceReplacedByASameNamedStub:
    """Move 2: asked by the data element's own owner, and not reachable as one change set."""

    def test_the_projection_asks_once_per_rated_element_and_nothing_more(self, project: Path, tmp_path: Path) -> None:
        _rate(project)
        delta, baseline, staged = _delta_over(
            tmp_path / "copy",
            project / "model",
            lambda root: stubs.put(root, "interfaces/IExample.yaml", {**STUBS["interfaces"], "name": "IExample"}),
        )

        verdict = safety_verdict(delta, baseline, staged=staged)

        assert [(c.kind.value, c.resource, c.field, c.old, c.new) for c in verdict.changes] == [
            (SafetyChangeKind.LOWER_ALLOCATION.value, "IExample.value", "asil", "B", None)
        ]

    def test_an_update_of_the_kind_is_refused_before_the_guard(self, project: Path) -> None:
        _rate(project)
        before = _tree(project / "model")

        response, asked = _questions(
            project,
            [
                {
                    "operation": "update",
                    "address": {"layer": "interfaces", "element": "IExample", "path": [{"field": "kind"}]},
                    "value": "external_interface",
                }
            ],
        )

        assert response["error"] == "immutable-field", response
        assert asked == []
        assert _tree(project / "model") == before

    def test_a_remove_beside_a_same_named_create_is_refused_before_the_guard(self, project: Path) -> None:
        _rate(project)
        before = _tree(project / "model")

        response, asked = _questions(
            project,
            [
                {"operation": "remove", "address": {"layer": "interfaces", "element": "IExample"}},
                {
                    "operation": "create",
                    "address": {"layer": "interfaces", "element": "IExample"},
                    "value": {**STUBS["interfaces"], "name": "IExample"},
                },
            ],
        )

        assert response["error"] == "identity-conflict", response
        assert asked == []
        assert _tree(project / "model") == before


# The sixth family: a surviving connection witnesses the move under another name.
# The fifth family compares a port with itself, so a port removed and created again
# on a stub is a new port. The sixth owner family (`stubbed-connection`) reads a
# connection that survives the change, with its consumer end moved onto a stub-bound
# port; its silences are pinned, each stated limit saying so.

_CONSUMER = "ExampleConsumerSWC"
_CONSUMER_FILE = "swc/example/ExampleConsumerSWC.yaml"
_PRODUCER_FILE = "swc/example/ExampleSWC.yaml"
_CLEARED = stubs.RATED_CONSUMER_CLEARED


def _port_address(component: str, port: str, *fields: str) -> dict[str, Any]:
    path: list[dict[str, str]] = [{"field": "ports"}, {"member": port}]
    path.extend({"field": field} for field in fields)
    return {"layer": "swc", "element": component, "path": path}


def _create_port(component: str, port: str, direction: str, interface: str | None) -> dict[str, Any]:
    value: dict[str, Any] = {"name": port, "direction": direction}
    if interface is not None:
        value["interface"] = interface
    return {"operation": "create", "address": _port_address(component, port), "value": value}


def _remove_port(component: str, port: str) -> dict[str, Any]:
    return {"operation": "remove", "address": _port_address(component, port)}


def _connector_end(connector: str, end: str, value: dict[str, str]) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {"layer": "connectors", "element": connector, "path": [{"field": end}]},
        "value": value,
    }


def _seed_port(model_root: Path, relative: str, port: str, direction: str, interface: str) -> None:
    stubs.change(
        model_root,
        relative,
        lambda document: document["ports"].append({"name": port, "direction": direction, "interface": interface}),
    )


def _seed_connector(model_root: Path, name: str, source: tuple[str, str], target: tuple[str, str]) -> None:
    stubs.put(
        model_root,
        f"connectors/{name}.yaml",
        {"name": name, "from": {"swc": source[0], "port": source[1]}, "to": {"swc": target[0], "port": target[1]}},
    )


def _link(component: str) -> dict[str, Any]:
    """The ``satisfies`` member that allocates the model's requirement to a new component."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": component,
            "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
        },
        "value": "REQ_EXAMPLE_001@1",
    }


def _rated_consumer(project: Path) -> None:
    """The shared ``RatedConsumer`` (:func:`external_stubs_support.add_rated_consumer`), placed in the root and wired.

    Adds a ``Root`` member and the connector ``Example_to_Rated`` from ``ExampleSWC.pOut`` to its ``rIn``.
    """
    model = project / "model"
    stubs.add_rated_consumer(model)
    stubs.change(
        model,
        "compositions/Root.yaml",
        lambda document: document["members"].append({"name": "RatedConsumer", "type": "RatedConsumer"}),
    )
    _seed_connector(model, "Example_to_Rated", ("ExampleSWC", "pOut"), ("RatedConsumer", "rIn"))


def _other_consumer(*, member: bool, port: str = "rStub") -> list[dict[str, Any]]:
    """A second rated consumer whose one ``require`` port is on the stub; placed in the root if *member*."""
    component = {
        "name": "OtherConsumer",
        "type": "application",
        "asil": "B",
        "ports": [{"name": port, "direction": "require", "interface": "IForeign"}],
        "memory": {"RAM0": 64},
        "handle_termination_and_restart": "no_support",
        "group": "example",
    }
    operations: list[dict[str, Any]] = [
        {"operation": "create", "address": {"layer": "swc", "element": "OtherConsumer"}, "value": component},
        _link("OtherConsumer"),
    ]
    if member:
        operations.append(
            {
                "operation": "create",
                "address": {
                    "layer": "compositions",
                    "element": "Root",
                    "path": [{"field": "members"}, {"member": "OtherConsumer"}],
                },
                "value": {"name": "OtherConsumer", "type": "OtherConsumer"},
            }
        )
    return operations


def _bundled_move(*, new_port: str = "rNew", connector: bool = True) -> list[dict[str, Any]]:
    """``rIn`` removed and ``rNew`` created on the stub, and -- if *connector* -- the connector moved onto it."""
    operations = [_remove_port(_CONSUMER, "rIn"), _create_port(_CONSUMER, new_port, "require", "IForeign")]
    if connector:
        operations.append(_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": new_port}))
    return operations


def _witnessed(
    project: Path, operations: list[dict[str, Any]], *, safety_approved: bool = True, sink: Any = None
) -> tuple[dict[str, Any], list[list[tuple[str, str, str, Any, Any]]], list[tuple[str, ...]], list[str]]:
    """The response, and the questions, the notes and the rendered safety prompt of each review call.

    *safety_approved* answers the safety question; *sink*, when given, receives the
    host notes ``handle_request`` emits.
    """
    asked: list[list[tuple[str, str, str, Any, Any]]] = []
    notes: list[tuple[str, ...]] = []
    rendered: list[str] = []

    def review(proposal: Any) -> ApprovalDecision:
        asked.append(
            [
                (change.kind.value, change.resource, change.field, change.old, change.new)
                for change in proposal.safety.changes
            ]
        )
        notes.append(tuple(proposal.safety.notes))
        if proposal.safety.changes:
            rendered.append(render_safety_review(proposal))
        return ApprovalDecision(
            change_set_id=proposal.change_set_id,
            baseline=proposal.baseline,
            approved=True,
            safety_approved=safety_approved,
        )

    return _change_set(project, operations, review, sink), asked, notes, rendered


def _assert_witness_note(
    note: str, *, witnesses: tuple[str, ...], new_port: str, owner: str = f"{_CONSUMER}.rIn"
) -> None:
    """The connection note states the fact the model carries, and never that the signal is the same."""
    assert owner in note, note
    for witness in witnesses:
        assert witness in note, note
    assert new_port in note, note
    assert "uncheckable" in note, note
    assert "same signal" not in note, note


def _clean(project: Path) -> None:
    assert list(check_cross_layer_consistency(str(project / "model")).records) == []


_ON_STUB = "whose interface {} is an external interface stub"
_ONE = "this connection's consumer interface became uncheckable"
_MANY = "these connections' consumer interfaces became uncheckable"


def _note(owner: str, *groups: str, ending: str = _ONE) -> str:
    """The connection note spelled out whole, so every wording branch is compared and none only searched for."""
    return f"{owner}: {'; '.join(groups)} — {ending}"


class TestASurvivingConnectorWitnessesTheMove:
    """The asks on an assembly connector, and the one bundled shape no connection witnesses."""

    def test_the_rename_and_re_point_bundled_with_the_connector_update_asks(self, project: Path) -> None:
        """A rename bundled with a re-point and a connector update asks, witnessed by the connector.

        No port identity links ``rIn`` and ``rNew``; ``Example_to_Consumer`` keeps its
        name, provider end and consumer component while its consumer end moves.
        """
        _rate(project)

        response, asked, notes, _rendered = _witnessed(project, _create_stub() + _bundled_move())

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Consumer",), new_port="rNew")

    def test_the_same_move_with_no_connection_to_witness_it_stays_silent(self, project: Path) -> None:
        """The pin's other half: an unconnected port carries no evidence (a stated limit)."""
        _rate(project)
        (project / "model" / "connectors" / "Example_to_Consumer.yaml").unlink()
        _clean(project)

        response, asked, notes, _rendered = _witnessed(project, _create_stub() + _bundled_move(connector=False))

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_connector_only_rewire_to_an_existing_stub_port_asks(self, project: Path) -> None:
        """No port changes at all: ``rIn`` stays, still judged, and loses the one connection it had."""
        _rate(project)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"})]
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Consumer",), new_port="rStub")
        assert note == _note(
            f"{_CONSUMER}.rIn",
            f"connector Example_to_Consumer now ends on port rStub, {_ON_STUB.format('IForeign')}",
        )

    def test_a_declined_rewire_abandons_with_the_note_in_its_reason(self, project: Path) -> None:
        """A declined rewire abandons, and its reason carries the note.

        The family renders as family 5's line, which for a connector-only rewire names an
        interface change ``rIn`` did not make; the note corrects it, and a declined
        weakening's reason is the one place the line travels without the prompt.
        """
        _rate(project)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)
        before = _tree(project / "model")
        sink: list[str] = []

        response, _asked, shown, _rendered = _witnessed(
            project,
            [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"})],
            safety_approved=False,
            sink=sink.append,
        )

        assert response.get("status") == "abandoned", response
        assert _tree(project / "model") == before
        ((note,),) = shown
        (reason,) = sink
        assert "'ExampleConsumerSWC.rIn': interface IExample -> IForeign" in reason, reason
        assert f"Note: {note}." in reason, reason
        assert reason.index("IExample -> IForeign") < reason.index(note) < reason.index("always on")

    def test_a_consumer_lowered_in_the_same_change_set_does_not_cancel_it(self, project: Path) -> None:
        """The baseline level decides; the lowering is the first family's own question, beside this one."""
        _rate(project)
        lower = {
            "operation": "update",
            "address": {"layer": "swc", "element": _CONSUMER, "path": [{"field": "asil"}]},
            "value": "QM",
        }

        response, asked, notes, _rendered = _witnessed(project, _create_stub() + _bundled_move() + [lower])

        assert response.get("status") == "committed", response
        assert len(asked) == 1
        assert sorted(asked[0], key=repr) == sorted(
            [_REPOINTED, (SafetyChangeKind.LOWER_ALLOCATION.value, _CONSUMER, "asil", "B", "QM")], key=repr
        )
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Consumer",), new_port="rNew")

    def test_a_consumer_converted_into_a_component_stub_with_a_renamed_port_asks(self, project: Path) -> None:
        """The second stub kind: the new port declares no interface on a component the staged model makes external."""
        _rate(project)
        _rated_consumer(project)
        _clean(project)
        operations = [
            *stubs.convert_rated_consumer(),
            _remove_port("RatedConsumer", "rIn"),
            _create_port("RatedConsumer", "rNew", "require", None),
            _connector_end("Example_to_Rated", "to", {"swc": "RatedConsumer", "port": "rNew"}),
        ]

        response, asked, notes, _rendered = _witnessed(project, operations)

        assert response.get("status") == "committed", response
        assert asked == [[_CLEARED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Rated",), new_port="rNew", owner="RatedConsumer.rIn")
        assert note == _note(
            "RatedConsumer.rIn",
            "connector Example_to_Rated now ends on port rNew, which declares no interface on an external component",
        )


class TestTwoPortChangesCannotCancel:
    """Owner by owner, never a count: a connection moved the other way withdraws nothing from the first."""

    def test_a_rated_port_rewired_again_from_a_stub_provider_still_asks(self, project: Path) -> None:
        """``rIn`` and ``rStub`` swap providers: one connection still ends on ``rIn``, and it asks.

        A count of connections into the rated port would cancel; the owner-by-owner comparison does not.
        """
        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        _seed_port(model, _PRODUCER_FILE, "pForeign", "provide", "IForeign")
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        _seed_connector(model, "Foreign_to_Consumer", ("ExampleSWC", "pForeign"), (_CONSUMER, "rStub"))
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            [
                _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
                _connector_end("Foreign_to_Consumer", "to", {"swc": _CONSUMER, "port": "rIn"}),
            ],
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Consumer",), new_port="rStub")
        assert "Foreign_to_Consumer" not in note


def _fan_in(model_root: Path, names: tuple[str, ...]) -> None:
    """One more provider port on ``ExampleSWC`` per extra connector, each wired into ``rIn``."""
    for index, name in enumerate(names):
        port = f"pOut{index + 2}"
        _seed_port(model_root, _PRODUCER_FILE, port, "provide", "IExample")
        _seed_connector(model_root, name, ("ExampleSWC", port), (_CONSUMER, "rIn"))


def _move_all(names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_connector_end(name, "to", {"swc": _CONSUMER, "port": "rStub"}) for name in ("Example_to_Consumer", *names)]


class TestOneLossIsOneOwner:
    """Two connections witnessing one loss give one question and one note naming both."""

    def test_two_witnesses_give_one_owner_and_one_note_naming_both(self, project: Path) -> None:
        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        _fan_in(model, ("Second_to_Consumer",))
        _clean(project)

        response, asked, notes, _rendered = _witnessed(project, _move_all(("Second_to_Consumer",)))

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("Example_to_Consumer", "Second_to_Consumer"), new_port="rStub")
        assert note == _note(
            f"{_CONSUMER}.rIn",
            "connector Example_to_Consumer and connector Second_to_Consumer now end on port rStub, "
            + _ON_STUB.format("IForeign"),
            ending=_MANY,
        )

    def test_witnesses_on_two_stub_ports_are_one_owner_asked_with_the_first_witness_interface(
        self, project: Path
    ) -> None:
        """Two groups in one note; the question's ``new`` is the first witness's staged interface.

        Connectors are read in baseline order.
        """
        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        stubs.put(model, "interfaces/IForeignToo.yaml", {**STUBS["interfaces"], "name": "IForeignToo"})
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        _seed_port(model, _CONSUMER_FILE, "rStubToo", "require", "IForeignToo")
        _fan_in(model, ("Second_to_Consumer",))
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            [
                _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
                _connector_end("Second_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStubToo"}),
            ],
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        assert note == _note(
            f"{_CONSUMER}.rIn",
            f"connector Example_to_Consumer now ends on port rStub, {_ON_STUB.format('IForeign')}",
            f"connector Second_to_Consumer now ends on port rStubToo, {_ON_STUB.format('IForeignToo')}",
            ending=_MANY,
        )

    def test_at_the_summary_limit_the_note_names_what_fits_and_counts_the_rest(self, project: Path) -> None:
        """The note is bounded by the sentence itself, never left to the flattener's elision."""
        from swcraft_tools.editing.review import _SUMMARY_LIMIT

        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        names = tuple(f"Witness{index}_{'x' * 90}" for index in range(2, 7))
        _fan_in(model, names)
        _clean(project)

        response, asked, notes, rendered = _witnessed(project, _move_all(names))

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        assert len(note) <= _SUMMARY_LIMIT, len(note)
        assert "Example_to_Consumer" in note
        named = sum(f"connector {name} " in note for name in ("Example_to_Consumer", *names))
        assert 1 <= named < 6, note
        assert note.endswith(f"; and {6 - named} more connections moved off it — {_MANY}"), note
        (prompt,) = rendered
        (line,) = [line for line in prompt.splitlines() if line.startswith("  note: ")]
        assert line == f"  note: {note}"

    def test_one_counted_witness_is_spelled_singular(self) -> None:
        """Two witnesses on two ports of which only the first fits: the count names one connection, the ending two."""
        from swcraft_tools.editing.safety_guard import _connection_note, _Witness

        witnesses = [
            _Witness(f"connector {'a' * 150}", "rStub", "IForeign"),
            _Witness("connector B", "rStubToo", "IForeignToo"),
        ]

        note = _connection_note(_CONSUMER, "rIn", witnesses)

        assert note == _note(
            f"{_CONSUMER}.rIn",
            f"connector {'a' * 150} now ends on port rStub, {_ON_STUB.format('IForeign')}; "
            "and 1 more connection moved off it",
            ending=_MANY,
        )

    def test_a_note_that_cannot_fit_one_witness_is_left_to_the_prompts_elision(self) -> None:
        """Names near the identifier limit: the sentence is returned whole and the prompt cuts it.

        The cut may take the sentence's ending with it; that is this path's pinned behaviour.
        """
        from swcraft_tools.editing.review import flatten
        from swcraft_tools.editing.safety_guard import _CONNECTION_NOTE_LIMIT, _connection_note, _Witness

        long = "N" * 128
        witnesses = [_Witness(f"connector {long}", long, long), _Witness("connector B", long, long)]

        note = _connection_note(long, long, witnesses)

        assert len(note) > _CONNECTION_NOTE_LIMIT
        assert note == _note(
            f"{long}.{long}",
            f"connector {long} now ends on port {long}, {_ON_STUB.format(long)}; and 1 more connection moved off it",
            ending=_MANY,
        )
        shown = flatten(note, _CONNECTION_NOTE_LIMIT)
        assert len(shown) <= _CONNECTION_NOTE_LIMIT
        assert shown.endswith(f"… (+{len(note) - shown.index('…')} characters)"), shown
        assert "uncheckable" not in shown

    def test_the_notes_bound_is_the_prompts_own(self) -> None:
        """The guard spells the prompt's line bound a second time, since it cannot import the renderer back."""
        from swcraft_tools.editing.review import _SUMMARY_LIMIT
        from swcraft_tools.editing.safety_guard import _CONNECTION_NOTE_LIMIT

        assert _CONNECTION_NOTE_LIMIT == _SUMMARY_LIMIT


class TestFamilyFiveWins:
    """Where the fifth family already asks, a surviving connection adds no owner and no note."""

    def test_the_in_place_re_point_keeps_its_question_byte_identical(self, project: Path) -> None:
        _rate(project)

        response, asked, notes, _rendered = _witnessed(project, _create_stub() + _repoint_consumer("IForeign"))

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[_REPOINTED]], [()])

    def test_the_in_place_component_stub_keeps_its_question_byte_identical(self, project: Path) -> None:
        """``test_stub_ports_editing``'s ``_CLEARED``, with a connector that survives on the same, cleared port."""
        _rate(project)
        _rated_consumer(project)
        _clean(project)
        operations = [
            *stubs.convert_rated_consumer(),
            {"operation": "update", "address": _port_address("RatedConsumer", "rIn", "interface"), "value": None},
        ]

        response, asked, notes, _rendered = _witnessed(project, operations)

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[_CLEARED]], [()])

    def test_a_port_replaced_under_its_own_name_is_refused_before_the_guard(self, project: Path) -> None:
        """A port replaced under its own name cannot be staged, so it never reaches the sixth family.

        A ``remove`` and a ``create`` at the same identity is refused before the review.
        """
        _rate(project)
        before = _tree(project / "model")

        response, asked, _notes, _rendered = _witnessed(
            project,
            _create_stub()
            + [
                _remove_port(_CONSUMER, "rIn"),
                _create_port(_CONSUMER, "rOld", "require", "IExample"),
                _create_port(_CONSUMER, "rIn", "require", "IForeign"),
            ],
        )

        assert response["error"] == "identity-conflict", response
        assert asked == []
        assert _tree(project / "model") == before


class TestSeveralOwners:
    """Two losses in one verdict, and one loss witnessed by a connector and a delegation."""

    def test_two_rated_ports_rewired_ask_twice_in_connector_order_with_a_note_each(self, project: Path) -> None:
        _rate(project)
        _rated_consumer(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        _seed_port(model, stubs.RATED_CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, rendered = _witnessed(
            project,
            [
                _connector_end("Example_to_Rated", "to", {"swc": "RatedConsumer", "port": "rStub"}),
                _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
            ],
        )

        assert response.get("status") == "committed", response
        rated = (SafetyChangeKind.LOWER_ALLOCATION.value, "RatedConsumer.rIn", "interface", "IExample", "IForeign")
        assert asked == [[_REPOINTED, rated]]
        why = f"now ends on port rStub, {_ON_STUB.format('IForeign')}"
        assert notes == [
            (
                _note(f"{_CONSUMER}.rIn", f"connector Example_to_Consumer {why}"),
                _note("RatedConsumer.rIn", f"connector Example_to_Rated {why}"),
            )
        ]
        (prompt,) = rendered
        assert [line for line in prompt.splitlines() if line.startswith("  note: ")] == [
            f"  note: {note}" for note in notes[0]
        ]


class TestWhatNoConnectionWitnesses:
    """The silences: nothing withdrawn, the relation gone, or a stated limit, each pinned."""

    def test_a_pure_port_rename_the_connector_follows_stays_silent(self, project: Path) -> None:
        _rate(project)
        operations = [
            _remove_port(_CONSUMER, "rIn"),
            _create_port(_CONSUMER, "rRenamed", "require", "IExample"),
            _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rRenamed"}),
        ]

        response, asked, notes, _rendered = _witnessed(project, operations)

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_pure_connector_rename_stays_silent(self, project: Path) -> None:
        _rate(project)
        connector = yaml.safe_load(
            (project / "model" / "connectors" / "Example_to_Consumer.yaml").read_text(encoding="utf-8")
        )
        operations = [
            {"operation": "remove", "address": {"layer": "connectors", "element": "Example_to_Consumer"}},
            {
                "operation": "create",
                "address": {"layer": "connectors", "element": "Example_to_Consumer_Renamed"},
                "value": {**connector, "name": "Example_to_Consumer_Renamed"},
            },
        ]

        response, asked, notes, _rendered = _witnessed(project, operations)

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_genuine_disconnect_stays_silent(self, project: Path) -> None:
        _rate(project)

        response, asked, notes, _rendered = _witnessed(
            project, [{"operation": "remove", "address": {"layer": "connectors", "element": "Example_to_Consumer"}}]
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_removal_of_the_connection_beside_the_bundled_move_stays_silent(self, project: Path) -> None:
        """The relation the rule judged is gone with the connector: a structural move."""
        _rate(project)
        remove = {"operation": "remove", "address": {"layer": "connectors", "element": "Example_to_Consumer"}}

        response, asked, notes, _rendered = _witnessed(
            project, _create_stub() + _bundled_move(connector=False) + [remove]
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_an_unrelated_provider_change_stays_silent(self, project: Path) -> None:
        _rate(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            [
                {
                    "operation": "update",
                    "address": _port_address("ExampleSWC", "pOut", "description"),
                    "value": "Reworded, nothing rewired",
                }
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_new_stub_added_alone_stays_silent(self, project: Path) -> None:
        _rate(project)

        response, asked, notes, _rendered = _witnessed(project, _create_stub())

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_known_to_known_rewire_stays_silent(self, project: Path) -> None:
        """A declared target stays visible to the consumer rule: nothing is withdrawn."""
        _rate(project)
        _seed_port(project / "model", _CONSUMER_FILE, "rKnown", "require", "IExample")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rKnown"})]
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_connection_already_on_a_stub_in_the_baseline_stays_silent(self, project: Path) -> None:
        """Nothing judged before: the consumer end's baseline interface is already opaque."""
        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        stubs.put(model, "interfaces/IForeignToo.yaml", {**STUBS["interfaces"], "name": "IForeignToo"})
        stubs.set_port(model, _CONSUMER_FILE, "rIn", interface="IForeign")
        _seed_port(model, _CONSUMER_FILE, "rStubToo", "require", "IForeignToo")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStubToo"})]
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_the_provider_end_changed_in_the_same_change_set_stays_silent(self, project: Path) -> None:
        """A stated limit: both ends moved -- onto a provider of the stub -- is not this family's evidence."""
        _rate(project)
        model = project / "model"
        _seed_stub(project, "interfaces")
        _seed_port(model, _PRODUCER_FILE, "pForeign", "provide", "IForeign")
        _seed_port(model, _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            [
                _connector_end("Example_to_Consumer", "from", {"swc": "ExampleSWC", "port": "pForeign"}),
                _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_the_consumer_component_changed_stays_silent(self, project: Path) -> None:
        """A stated limit: the connection moved onto another component's stub port.

        The original component carries a stub-bound ``rStub`` too, so only the
        consumer-component condition keeps it silent.
        """
        _rate(project)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            _other_consumer(member=True)
            + [_connector_end("Example_to_Consumer", "to", {"swc": "OtherConsumer", "port": "rStub"})],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_dangling_consumer_end_is_pass_ds_refusal_and_never_a_question(self, project: Path) -> None:
        _rate(project)
        before = _tree(project / "model")

        response, asked, _notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rMissing"})]
        )

        assert response["error"] == "broken-reference", response
        assert asked == []
        assert _tree(project / "model") == before

    @pytest.mark.parametrize(
        ("consumer", "element"), [(None, "B"), ("B", None)], ids=["unrated-consumer", "unrated-element"]
    )
    def test_a_rewire_with_nothing_rated_to_withdraw_stays_silent(
        self, project: Path, consumer: str | None, element: str | None
    ) -> None:
        """The connector-only rewire with the consumer or the data element unrated: nothing to withdraw."""
        _rate(project, consumer=consumer, element=element)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"})]
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_connector_moved_onto_a_provide_port_is_pass_ds_refusal(self, project: Path) -> None:
        """The staged ``require`` condition decides nothing a commit can reach: pass D refuses the direction first."""
        _rate(project)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "pStub", "provide", "IForeign")
        before = _tree(project / "model")

        response, asked, _notes, _rendered = _witnessed(
            project, [_connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "pStub"})]
        )

        assert response["error"] == "consistency-violation", response
        assert asked == []
        assert _tree(project / "model") == before

    def test_a_changed_composition_field_stays_silent(self, project: Path) -> None:
        """A stated limit: a connector whose raw ``composition`` field moved is not the same connector.

        ``None`` and ``Root`` name one composition here: the field is compared raw.
        """
        _rate(project)
        _seed_stub(project, "interfaces")
        _seed_port(project / "model", _CONSUMER_FILE, "rStub", "require", "IForeign")
        _clean(project)

        response, asked, notes, _rendered = _witnessed(
            project,
            [
                {
                    "operation": "update",
                    "address": {
                        "layer": "connectors",
                        "element": "Example_to_Consumer",
                        "path": [{"field": "composition"}],
                    },
                    "value": "Root",
                },
                _connector_end("Example_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])


# The delegation fixture


def _delegate(project: Path) -> None:
    """``examples/basic`` rated, with each component inside a composition of its own.

    ``ProducerGroup`` delegates ``pOuter`` to ``ExampleSWC.pOut``, ``ConsumerGroup``
    delegates ``rOuter`` to ``ExampleConsumerSWC.rIn``, and one assembly connector joins
    the boundary ports, so the rated consumer's only connection is the delegation.
    """
    _rate(project)
    model = project / "model"
    for name, port, direction, member in (
        ("ProducerGroup", "pOuter", "provide", ("ExampleSWC", "pOut")),
        ("ConsumerGroup", "rOuter", "require", (_CONSUMER, "rIn")),
    ):
        stubs.put(
            model,
            f"compositions/{name}.yaml",
            {
                "name": name,
                "title": f"{name} composition",
                "ports": [{"name": port, "direction": direction, "interface": "IExample"}],
                "members": [{"name": member[0], "type": member[0]}],
                "delegations": [
                    {"name": f"{name}_{port}", "port": port, "member": member[0], "member_port": member[1]}
                ],
            },
        )
    stubs.change(
        model,
        "compositions/Root.yaml",
        lambda document: document.update(
            members=[
                {"name": "ProducerGroup", "composition": "ProducerGroup"},
                {"name": "ConsumerGroup", "composition": "ConsumerGroup"},
            ]
        ),
    )
    stubs.put(
        model,
        "connectors/Example_to_Consumer.yaml",
        {
            "name": "Example_to_Consumer",
            "from": {"composition": "ProducerGroup", "port": "pOuter"},
            "to": {"composition": "ConsumerGroup", "port": "rOuter"},
        },
    )


def _delegation_field(composition: str, delegation: str, field: str, value: str) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {
            "layer": "compositions",
            "element": composition,
            "path": [{"field": "delegations"}, {"member": delegation}, {"field": field}],
        },
        "value": value,
    }


@pytest.fixture
def delegated(project: Path) -> Path:
    _delegate(project)
    _clean(project)
    return project


class TestASurvivingRequireDelegationWitnessesTheMove:
    """The delegation arm: a changed ``member_port`` is the witness, everything else about it unchanged."""

    def test_the_fixture_routes_the_consumers_only_connection_through_the_delegation(self, delegated: Path) -> None:
        connector = yaml.safe_load(
            (delegated / "model" / "connectors" / "Example_to_Consumer.yaml").read_text(encoding="utf-8")
        )
        assert connector["to"] == {"composition": "ConsumerGroup", "port": "rOuter"}

    def test_the_inner_switch_to_a_stub_bound_port_asks(self, delegated: Path) -> None:
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                _create_port(_CONSUMER, "rStub", "require", "IForeign"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        assert note == _note(
            f"{_CONSUMER}.rIn",
            f"delegation ConsumerGroup.ConsumerGroup_rOuter now ends on port rStub, {_ON_STUB.format('IForeign')}",
        )

    def test_the_bundled_rename_and_re_point_through_the_delegation_asks(self, delegated: Path) -> None:
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + _bundled_move(connector=False)
            + [_delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rNew")],
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        (note,) = notes[0]
        _assert_witness_note(note, witnesses=("ConsumerGroup_rOuter",), new_port="rNew")

    def test_a_connector_and_a_delegation_witnessing_one_loss_are_one_owner(self, delegated: Path) -> None:
        """A connector and a delegation witnessing one loss are one owner: one question, one note, connector first.

        ``LocalProducer`` inside ``ConsumerGroup`` feeds ``rIn`` beside the delegation; both move onto ``rStub``.
        """
        model = delegated / "model"
        stubs.put(
            model,
            "swc/example/LocalProducer.yaml",
            {
                "name": "LocalProducer",
                "type": "application",
                "asil": "B",
                "ports": [{"name": "pLocal", "direction": "provide", "interface": "IExample"}],
                "memory": {"RAM0": 64},
                "handle_termination_and_restart": "no_support",
                # The link lives in the component.
                "satisfies": ["REQ_EXAMPLE_001@1"],
            },
        )
        stubs.change(
            model,
            "compositions/ConsumerGroup.yaml",
            lambda document: document["members"].append({"name": "LocalProducer", "type": "LocalProducer"}),
        )
        stubs.put(
            model,
            "connectors/Local_to_Consumer.yaml",
            {
                "name": "Local_to_Consumer",
                "composition": "ConsumerGroup",
                "from": {"swc": "LocalProducer", "port": "pLocal"},
                "to": {"swc": _CONSUMER, "port": "rIn"},
            },
        )
        _clean(delegated)

        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                _create_port(_CONSUMER, "rStub", "require", "IForeign"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rStub"),
                _connector_end("Local_to_Consumer", "to", {"swc": _CONSUMER, "port": "rStub"}),
            ],
        )

        assert response.get("status") == "committed", response
        assert asked == [[_REPOINTED]]
        assert notes == [
            (
                _note(
                    f"{_CONSUMER}.rIn",
                    "connector Local_to_Consumer and delegation ConsumerGroup.ConsumerGroup_rOuter now end on port "
                    f"rStub, {_ON_STUB.format('IForeign')}",
                    ending=_MANY,
                ),
            )
        ]

    def test_a_boundary_only_switch_stays_silent(self, delegated: Path) -> None:
        """The outer port moved onto the stub, the inner port did not: the rated port is still what is judged."""
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                {
                    "operation": "create",
                    "address": {
                        "layer": "compositions",
                        "element": "ConsumerGroup",
                        "path": [{"field": "ports"}, {"member": "rOuterStub"}],
                    },
                    "value": {"name": "rOuterStub", "direction": "require", "interface": "IForeign"},
                },
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "port", "rOuterStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_changed_outer_port_stays_silent_whatever_the_member_port_did(self, delegated: Path) -> None:
        """A stated limit: a delegation whose outer ``port`` moved is not the same delegation.

        Its ``member_port`` move alone would ask; only the outer-port condition keeps it silent.
        """
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                {
                    "operation": "create",
                    "address": {
                        "layer": "compositions",
                        "element": "ConsumerGroup",
                        "path": [{"field": "ports"}, {"member": "rOuterStub"}],
                    },
                    "value": {"name": "rOuterStub", "direction": "require", "interface": "IForeign"},
                },
                _create_port(_CONSUMER, "rStub", "require", "IForeign"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "port", "rOuterStub"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_require_delegation_moved_onto_a_provide_port_is_pass_ds_refusal(self, delegated: Path) -> None:
        """A delegation joins two ports of one direction, so pass D refuses the move before the review."""
        before = _tree(delegated / "model")

        response, asked, _notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                _create_port(_CONSUMER, "pStub", "provide", "IForeign"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "pStub"),
            ],
        )

        assert response["error"] == "consistency-violation", response
        assert asked == []
        assert _tree(delegated / "model") == before

    def test_a_provide_delegation_switched_to_a_stub_stays_silent(self, delegated: Path) -> None:
        """A producer's side: the stub declares no level a producer could fall short of (a stated limit)."""
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                _create_port("ExampleSWC", "pStub", "provide", "IForeign"),
                _delegation_field("ProducerGroup", "ProducerGroup_pOuter", "member_port", "pStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_changed_inner_member_stays_silent(self, delegated: Path) -> None:
        """A stated limit: the delegation names another member (the same type under a new name).

        Type and port both qualify, so only the member-name condition keeps it
        silent; members are read by name, with no rename reading.
        """
        members = [{"field": "members"}]
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + [
                _create_port(_CONSUMER, "rStub", "require", "IForeign"),
                {
                    "operation": "remove",
                    "address": {
                        "layer": "compositions",
                        "element": "ConsumerGroup",
                        "path": [*members, {"member": _CONSUMER}],
                    },
                },
                {
                    "operation": "create",
                    "address": {
                        "layer": "compositions",
                        "element": "ConsumerGroup",
                        "path": [*members, {"member": "Consumer"}],
                    },
                    "value": {"name": "Consumer", "type": _CONSUMER},
                },
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member", "Consumer"),
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])

    def test_a_changed_member_type_stays_silent(self, delegated: Path) -> None:
        """A stated limit: the member now instantiates another type, so the consumer component changed.

        ``rStub`` is stub-bound on both types: only the member-type condition keeps it silent.
        """
        response, asked, notes, _rendered = _witnessed(
            delegated,
            _create_stub()
            + _other_consumer(member=False)
            + [
                _create_port(_CONSUMER, "rStub", "require", "IForeign"),
                {
                    "operation": "update",
                    "address": {
                        "layer": "compositions",
                        "element": "ConsumerGroup",
                        "path": [{"field": "members"}, {"member": _CONSUMER}, {"field": "type"}],
                    },
                    "value": "OtherConsumer",
                },
                _delegation_field("ConsumerGroup", "ConsumerGroup_rOuter", "member_port", "rStub"),
            ],
        )

        assert response.get("status") == "committed", response
        assert (asked, notes) == ([[]], [()])


class TestBothNotesInOneVerdict:
    """The ambiguity note and the connection note side by side, in that order, below the list."""

    _TWIN = {
        "type": "application",
        "asil": "B",
        "description": "An identical twin.",
        "runnables": [{"name": "Run", "events": [{"type": "timing", "period": 0.05}]}],
        "memory": {"RAM0": 64},
        "handle_termination_and_restart": "no_support",
    }

    @classmethod
    def _twins(cls, project: Path) -> None:
        model = project / "model"
        for name in ("TwinA", "TwinB"):
            # Each twin carries its own link to the example requirement.
            stubs.put(model, f"swc/app/{name}.yaml", {"name": name, **cls._TWIN, "satisfies": ["REQ_EXAMPLE_001@1"]})
        stubs.change(
            model,
            "compositions/Root.yaml",
            lambda document: document["members"].extend(
                [{"name": "TwinA", "type": "TwinA"}, {"name": "TwinB", "type": "TwinB"}]
            ),
        )

    @classmethod
    def _crosswise(cls) -> list[dict[str, Any]]:
        """``TwinA``/``TwinB`` out, ``TwinC``/``TwinD`` in, identical: which became which cannot be told."""
        operations: list[dict[str, Any]] = []
        for name in ("TwinA", "TwinB"):
            operations.append({"operation": "remove", "address": {"layer": "swc", "element": name}})
            operations.append(
                {
                    "operation": "remove",
                    "address": {
                        "layer": "compositions",
                        "element": "Root",
                        "path": [{"field": "members"}, {"member": name}],
                    },
                }
            )
        for name in ("TwinC", "TwinD"):
            operations.append(
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": name},
                    "value": {"name": name, "group": "app", **cls._TWIN},
                }
            )
            operations.append(
                {
                    "operation": "create",
                    "address": {
                        "layer": "compositions",
                        "element": "Root",
                        "path": [{"field": "members"}, {"member": name}],
                    },
                    "value": {"name": name, "type": name},
                }
            )
            operations.append(_link(name))
        return operations

    def test_the_twins_alone_carry_the_ambiguity_note(self, project: Path) -> None:
        """The control for the case below: the crosswise pairing on its own notes, and asks both removals."""
        _rate(project)
        self._twins(project)
        _clean(project)

        response, asked, notes, _rendered = _witnessed(project, self._crosswise())

        assert response.get("status") == "committed", response
        assert {row[1] for row in asked[0]} == {"TwinA", "TwinB"}
        (ambiguity,) = notes[0]
        assert "no rename was recognised" in ambiguity

    def test_the_ambiguity_note_comes_first_and_the_connection_note_after_it(self, project: Path) -> None:
        _rate(project)
        self._twins(project)
        _clean(project)

        response, asked, notes, rendered = _witnessed(project, self._crosswise() + _create_stub() + _bundled_move())

        assert response.get("status") == "committed", response
        assert _REPOINTED in asked[0]
        ambiguity, connection = notes[0]
        assert "no rename was recognised" in ambiguity
        _assert_witness_note(connection, witnesses=("Example_to_Consumer",), new_port="rNew")
        (prompt,) = rendered
        note_lines = [line for line in prompt.splitlines() if line.startswith("  note: ")]
        assert note_lines == [f"  note: {ambiguity}", f"  note: {connection}"]
