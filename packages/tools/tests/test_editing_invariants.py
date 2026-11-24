"""The protocol's guarantees as properties over generated inputs, not as examples.

The acceptance vectors each pin one exchange; this module states the same guarantees over a
corpus: plain comprehensions over the reference model's own inventory (:data:`BASE_ELEMENTS`,
globbed off ``examples/basic`` at import), with no property-testing library.

1. **The model-commit invariant.** A change set that does not reach ``committed`` leaves the whole
   model tree byte-identical, over every generated rejection in all four failure classes and over
   raised faults too.
2. **The security invariants**: no response carries a storage path, no request accepts one, every
   client value that reaches a path is a single validated segment, and no read mutates.
3. **The registry invariants** live in ``test_editing_registry.py``.
4. **The coverage relation's algebra** lives in ``test_address_coverage_relation.py``.
5. **The exporter regression.** ``examples/basic`` builds ARXML byte-identical to the ``basic-4.2.2``
   golden and Sphinx-Needs output byte-identical to ``fixtures/reference_build/needs.json``; a
   difference is investigated, never refreshed to pass (``fixtures/reference_build/README.md``).

And one meta-property: **the existing suite's expectations were not weakened**: the legacy modules
over the code the protocol touched are held to their inventory; an expectation may be added, never
dropped or disabled.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import impl_composites_support as composites
import pytest
import yaml
from conftest import (
    BASE_VECTOR,
    REPO_ROOT,
    read_golden,
)
from node_support import requires_node
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, PathStep
from swcraft_tools.editing.diagnosis import UndiagnosedInconsistency
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.protocol import ERROR_CLASS_MAP, FAILURE_CLASSES, READ_FORMS
from swcraft_tools.editing.registry import PathSegmentError
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.project import is_absolute_anywhere
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    SEVERITY_ERROR,
    check_cross_layer_consistency,
    severity_of,
)
from swcraft_tools.validator.data_types import REF_PATH
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs

# The generated corpus: the reference model's own inventory

#: Substrings that would mean a response disclosed where an element is stored, plus the three
#: manifest root keys a passport-shaped leak would carry.
STORAGE_MARKERS = ("/", "\\", ".yaml", "model_root", "dist_root", "views_root")

#: Values that are not a single path segment, each refused before any filesystem access at both
#: places a client value reaches a path: an element identity and a placement field.
NOT_A_SEGMENT = ("..", ".", "../evil", "a/b", "a\\b", "", "   ", "/abs", "x\x00y", "swc/example/ExampleSWC.yaml")


@dataclass(frozen=True)
class BaseElement:
    """One element of the reference model, with what a generator needs of it.

    ``payload`` is ``document`` plus the layer's placement field, what a ``create`` here would carry;
    built at collection time, before any fixture.
    """

    layer: str
    identity: str
    kind: str
    document: Mapping[str, Any]
    payload: Mapping[str, Any]

    @property
    def id(self) -> str:
        return f"{self.layer}/{self.identity}"

    def address(self, *path: PathStep) -> dict[str, Any]:
        return Address(layer=self.layer, element=self.identity, path=path).to_json()


def _kind_of(layer: str, document: Mapping[str, Any]) -> str:
    """The element kind of *document*, off the registry rather than off a table."""
    kinds = reg.REGISTRY.layer(layer).top_level_kinds
    if len(kinds) == 1:
        return kinds[0].kind
    discriminator = kinds[0].discriminator_field
    assert discriminator is not None, f"{layer} has several kinds and no discriminator"
    return document[discriminator]


def _base_elements() -> tuple[BaseElement, ...]:
    """Every element of ``examples/basic``, globbed at import.

    Through the harness's own layer table, never the registry's storage record, which is the thing
    under test.
    """
    found: list[BaseElement] = []
    for layer, directory in fixtures.LAYER_DIRECTORIES.items():
        root = fixtures.BASE_MODEL_ROOT / directory
        pattern = "**/*.yaml" if layer in fixtures.NESTED_LAYERS else "*.yaml"
        identity_field = fixtures.LAYER_IDENTITY_FIELDS[layer]
        for path in sorted(root.glob(pattern), key=lambda path: path.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            payload = dict(document)
            if layer in fixtures.NESTED_LAYERS:
                payload["group"] = path.parent.name
            found.append(
                BaseElement(
                    layer=layer,
                    identity=document[identity_field],
                    kind=_kind_of(layer, document),
                    document=document,
                    payload=payload,
                )
            )
    return tuple(found)


BASE_ELEMENTS: tuple[BaseElement, ...] = _base_elements()


def _mutable_scalar(element: BaseElement) -> tuple[str, Any] | None:
    """The first mutable scalar field the element carries, and its value (so every kind gets an update)."""
    descriptor = reg.REGISTRY.element_kind(element.kind)
    for name, value in element.document.items():
        field = descriptor.get_field(name)
        if field is None or field.immutable:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        return name, value
    return None


def _moved(value: Any) -> Any:
    """A value of the same type that differs from *value*."""
    if isinstance(value, str):
        return f"{value} (moved by a property probe)"
    if isinstance(value, int):
        return value + 1
    return value + 1.0


#: ``(element, field, current value)`` for every element with a mutable scalar field; the guard keeps
#: a future kind with none from silently dropping out.
MUTABLE_SCALARS: tuple[tuple[BaseElement, str, Any], ...] = tuple(
    (element, *pair) for element in BASE_ELEMENTS if (pair := _mutable_scalar(element)) is not None
)

#: The two data type kinds whose `base` a reference-role rule judges: a moved `base` names nothing,
#: so those rows are refused by construction and counted apart from the vacuity floor.
ROLE_BASE_KINDS = frozenset({"value_type", "reference_type"})


# Dispatch helpers


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path — byte-identity, not a digest."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


#: The fixture project's directory name, not ``project``: a property asserts no response carries a
#: component of the project's location, and a generic name would fire on ordinary prose.
PROJECT_DIRNAME = "swcraft-invariant-probe-project"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fixtures.build_fixture(tmp_path / PROJECT_DIRNAME, BASE_VECTOR)


def _approve(proposal: ChangeSetProposal) -> Any:
    """A review boundary that always approves, so each rejection must hold through the commit's own checks."""
    from swcraft_tools.editing.authorization import ApprovalDecision

    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)


def _change_set(project_root: Path, *operations: Mapping[str, Any], baseline: str | None = None) -> dict[str, Any]:
    return {
        "operation": "change_set",
        "baseline": baseline if baseline is not None else baseline_token(project_root / "model"),
        "intent": "a generated change set",
        "operations": [dict(operation) for operation in operations],
    }


@dataclass(frozen=True)
class Outcome:
    """What one dispatch did: an answer, or the exception it raised."""

    response: dict[str, Any] | None
    raised: BaseException | None

    @property
    def committed(self) -> bool:
        return self.response is not None and self.response.get("status") == "committed"

    @property
    def error(self) -> str | None:
        return None if self.response is None else self.response.get("error")

    @property
    def failure_class(self) -> str | None:
        return None if self.response is None else self.response.get("failure_class")

    def describe(self) -> str:
        if self.raised is not None:
            return f"raised {type(self.raised).__name__}: {self.raised}"
        return f"answered {self.response}"


def _dispatch(
    request: Any,
    project_root: Path,
    *,
    scope: list[dict[str, Any]] | None = None,
    review: Any = _approve,
) -> Outcome:
    """Run *request* through the engine, capturing an exception as an outcome.

    *review* ``None`` stops the lifecycle at ``staged``, so a loop over an accepted corpus runs
    against one unmoved model.
    """
    try:
        return Outcome(
            handle_request(request, project=project_root, approved_scope=scope, review=review),
            None,
        )
    except Exception as raised:  # noqa: BLE001 - a fault is an outcome here, not a test error
        return Outcome(None, raised)


# The generated rejection corpus


@dataclass(frozen=True)
class Rejection:
    """One generated change set that must not reach ``committed``.

    ``expected_class`` is declared wherever the failure class is determined by construction, which is
    every rejection whose staged result the validator refuses over a reference. No rejection may raise.
    """

    name: str
    operations: tuple[Mapping[str, Any], ...] = ()
    expected_class: str | None = None
    scope: tuple[Mapping[str, Any], ...] | None = None
    stale: bool = False
    expected_error: str | None = None
    expected_addresses: tuple[Mapping[str, Any], ...] | None = None
    #: A literal request instead of a change set from *operations*: a ``read`` that resolves against
    #: nothing is a refusal from the same closed enum.
    request: Mapping[str, Any] | None = None


def _unknown_field_rejections() -> Iterator[Rejection]:
    """A field no element kind declares, at every element of the model."""
    for element in BASE_ELEMENTS:
        yield Rejection(
            name=f"unknown-field/{element.id}",
            operations=({"operation": "update", "address": element.address(FieldStep("no_such_field")), "value": 1},),
            expected_class="invalid",
        )


def _immutable_field_rejections() -> Iterator[Rejection]:
    """The identity of every element, and the discriminator of every kind that has one."""
    for element in BASE_ELEMENTS:
        identity_field = reg.REGISTRY.layer(element.layer).identity_field
        yield Rejection(
            name=f"immutable-identity/{element.id}",
            operations=(
                {"operation": "update", "address": element.address(FieldStep(identity_field)), "value": "Renamed"},
            ),
            expected_class="invalid",
        )
        discriminator = reg.REGISTRY.element_kind(element.kind).discriminator_field
        if discriminator is not None:
            yield Rejection(
                name=f"immutable-discriminator/{element.id}",
                operations=(
                    {"operation": "update", "address": element.address(FieldStep(discriminator)), "value": "base_type"},
                ),
                expected_class="invalid",
            )


def _type_mismatch_rejections() -> Iterator[Rejection]:
    """A list where every element kind declares a scalar."""
    for element, field, _ in MUTABLE_SCALARS:
        yield Rejection(
            name=f"type-mismatch/{element.id}.{field}",
            operations=(
                {"operation": "update", "address": element.address(FieldStep(field)), "value": ["not", "a", "scalar"]},
            ),
            expected_class="invalid",
        )


def _identity_conflict_rejections() -> Iterator[Rejection]:
    """A ``create`` at a resolving address, per element, with its own document: only the address is objectionable."""
    for element in BASE_ELEMENTS:
        yield Rejection(
            name=f"identity-conflict/{element.id}",
            operations=(
                {
                    "operation": "create",
                    "address": {"layer": element.layer, "element": element.identity},
                    "value": dict(element.payload),
                },
            ),
            expected_class="invalid",
        )


def _update(layer: str, element: str, path: list[Mapping[str, Any]], value: Any) -> dict[str, Any]:
    return {"operation": "update", "address": {"layer": layer, "element": element, "path": list(path)}, "value": value}


def _create(layer: str, element: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": element}, "value": dict(value)}


def _create_in(layer: str, element: str, path: list[Mapping[str, Any]], value: Any) -> dict[str, Any]:
    """A ``create`` at a nested member or map entry.

    The membership channel: an ``update`` of a whole collection is the reordering primitive, so a
    probe adding a member uses the verb that passes the identity-conflict, multiset and same-identity checks.
    """
    return {"operation": "create", "address": {"layer": layer, "element": element, "path": list(path)}, "value": value}


def _remove_in(layer: str, element: str, path: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {"operation": "remove", "address": {"layer": layer, "element": element, "path": list(path)}}


def _at(layer: str, element: str, *path: Mapping[str, Any]) -> dict[str, Any]:
    address: dict[str, Any] = {"layer": layer, "element": element}
    if path:
        address["path"] = [dict(step) for step in path]
    return address


#: The one mapping ``examples/basic``'s task carries, repeated where a second is appended so the
#: sequence stays satisfiable.
_MAPPED_PAIR: Mapping[str, Any] = {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}

#: A second mapping ``examples/basic`` can make: ``ExampleSWC``'s one runnable.
_SECOND_PAIR: Mapping[str, Any] = {"swc": "ExampleSWC", "runnable": "ExampleRun"}

#: The same runnable named by the occurrence ``examples/basic``'s root places it at.
_INSTANCE_PAIR: Mapping[str, Any] = {"instance": "ExampleSWC", "runnable": "ExampleRun"}


def _three_keys(pair: Mapping[str, Any]) -> dict[str, Any]:
    """*pair* as the tools emit it: three keys, the absent selector ``None``.

    A test writing an address or payload keeps two keys (an absent optional identity field reads as
    ``null``); one asserting what a tool emits compares against this.
    """
    return {"swc": pair["swc"], "instance": None, "runnable": pair["runnable"]}


def _reference_rejections() -> Iterator[Rejection]:
    """One change set per pass D reference rule, with the referrers it must name.

    Each is a determined ``broken-reference``. ``expected_addresses`` is an unordered multiset over the
    primary violation and its ``additional``, as in the vector harness: ``errorResult`` states no order.
    """
    yield Rejection(
        name="broken-reference/port.interface",
        operations=(
            _update(
                "swc",
                "ExampleSWC",
                [{"field": "ports"}, {"member": "pOut"}, {"field": "interface"}],
                "INoSuchInterface",
            ),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        # ONE violation, the root cause: the port's own dangling reference. The connector rule
        # abstains, since an undeclared interface cannot be judged as satisfied, so a client gets one
        # repair for one defect.
        expected_addresses=(_at("swc", "ExampleSWC", {"field": "ports"}, {"member": "pOut"}, {"field": "interface"}),),
    )
    yield Rejection(
        name="broken-reference/removed-interface",
        operations=({"operation": "remove", "address": {"layer": "interfaces", "element": "IExample"}},),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at("swc", "ExampleSWC", {"field": "ports"}, {"member": "pOut"}, {"field": "interface"}),
            _at("swc", "ExampleConsumerSWC", {"field": "ports"}, {"member": "rIn"}, {"field": "interface"}),
        ),
    )
    yield Rejection(
        name="broken-reference/task-partition",
        # A partition NAME no hardware element declares: the field is a `str` reference, so an
        # integer would be a `schema-violation` one pass earlier and miss the reference rule.
        operations=(_update("tasks", "Task_10ms", [{"field": "partition"}], "PARTITION7"),),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at("tasks", "Task_10ms", {"field": "partition"}),
            {"layer": "hardware"},
        ),
    )
    # The link lives in the element: a component's `satisfies` member naming no declared requirement.
    yield Rejection(
        name="broken-reference/requirement-link",
        operations=(
            _create_in("swc", "ExampleSWC", [{"field": "satisfies"}, {"member": "NO_SUCH_REQ@1"}], "NO_SUCH_REQ@1"),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(_at("swc", "ExampleSWC", {"field": "satisfies"}, {"member": "NO_SUCH_REQ@1"}),),
    )
    yield Rejection(
        name="broken-reference/connector-endpoint-swc",
        operations=(_update("connectors", "Example_to_Consumer", [{"field": "from"}, {"field": "swc"}], "NoSuchSWC"),),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(_at("connectors", "Example_to_Consumer", {"field": "from"}, {"field": "swc"}),),
    )
    yield Rejection(
        name="broken-reference/connector-endpoint-port",
        operations=(_update("connectors", "Example_to_Consumer", [{"field": "to"}, {"field": "port"}], "noSuchPort"),),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(_at("connectors", "Example_to_Consumer", {"field": "to"}, {"field": "port"}),),
    )
    yield Rejection(
        name="broken-reference/removed-data-type",
        operations=({"operation": "remove", "address": {"layer": "implementation_types", "element": "float32"}},),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at(
                "interfaces",
                "IExample",
                {"field": "data_elements"},
                {"member": "value"},
                {"field": "implementation_type"},
            ),
        ),
    )
    yield Rejection(
        name="broken-reference/removed-argument-data-type",
        operations=({"operation": "remove", "address": {"layer": "implementation_types", "element": "uint16"}},),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=tuple(
            _at(
                "interfaces",
                "IDiagnosticService",
                {"field": "operations"},
                {"member": operation},
                {"field": "arguments"},
                {"member": "dtc_id"},
                {"field": "implementation_type"},
            )
            for operation in ("ReadDtc", "ClearDtc")
        ),
    )
    yield Rejection(
        name="broken-reference/task-mapping-swc",
        operations=(
            _create_in(
                "tasks",
                "Task_10ms",
                [{"field": "runnables"}, {"member": {"swc": "NoSuchSWC", "runnable": "NoSuchRun"}}],
                {"swc": "NoSuchSWC", "runnable": "NoSuchRun"},
            ),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at(
                "tasks",
                "Task_10ms",
                {"field": "runnables"},
                {"member": {"swc": "NoSuchSWC", "instance": None, "runnable": "NoSuchRun"}},
                {"field": "swc"},
            ),
        ),
    )
    yield Rejection(
        name="broken-reference/task-mapping-runnable",
        operations=(
            _create_in(
                "tasks",
                "Task_10ms",
                [{"field": "runnables"}, {"member": {"swc": "ExampleSWC", "runnable": "NoSuchRun"}}],
                {"swc": "ExampleSWC", "runnable": "NoSuchRun"},
            ),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at(
                "tasks",
                "Task_10ms",
                {"field": "runnables"},
                {"member": {"swc": "ExampleSWC", "instance": None, "runnable": "NoSuchRun"}},
                {"field": "runnable"},
            ),
        ),
    )
    yield Rejection(
        name="broken-reference/sequence-task",
        operations=(_update("sequences", "task10ms_consumer_cycle", [{"field": "task"}], "NoSuchTask"),),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(_at("sequences", "task10ms_consumer_cycle", {"field": "task"}),),
    )
    yield Rejection(
        name="broken-reference/sequence-step-unmapped",
        # Created before the old step is removed: `steps` may not be empty, and
        # every operation is validated against the schema as it is applied.
        operations=(
            _create_in(
                "sequences",
                "task10ms_consumer_cycle",
                [{"field": "steps"}, {"member": {"swc": "ExampleSWC", "runnable": "ExampleRun"}}],
                {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            ),
            _remove_in("sequences", "task10ms_consumer_cycle", [{"field": "steps"}, {"member": dict(_MAPPED_PAIR)}]),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(
            _at(
                "sequences",
                "task10ms_consumer_cycle",
                {"field": "steps"},
                {"member": {"swc": "ExampleSWC", "instance": None, "runnable": "ExampleRun"}},
            ),
        ),
    )
    # The two sequence-step rules the validator reaches only through a task mapping that itself
    # dangles (membership in `task.runnables` is not trusted); both addresses are asserted, which
    # tells the pair from the task rule firing alone.
    for label, pair, field in (
        ("swc", {"swc": "NoSuchSWC", "runnable": "NoSuchRun"}, "swc"),
        ("runnable", {"swc": "ExampleSWC", "runnable": "NoSuchRun"}, "runnable"),
    ):
        yield Rejection(
            name=f"broken-reference/sequence-step-{label}",
            operations=(
                _create_in("tasks", "Task_10ms", [{"field": "runnables"}, {"member": dict(pair)}], dict(pair)),
                _create_in(
                    "sequences", "task10ms_consumer_cycle", [{"field": "steps"}, {"member": dict(pair)}], dict(pair)
                ),
                _remove_in(
                    "sequences", "task10ms_consumer_cycle", [{"field": "steps"}, {"member": dict(_MAPPED_PAIR)}]
                ),
            ),
            expected_class="invalid",
            expected_error="broken-reference",
            # Written with two keys above, emitted with three.
            expected_addresses=(
                _at("tasks", "Task_10ms", {"field": "runnables"}, {"member": _three_keys(pair)}, {"field": field}),
                _at(
                    "sequences",
                    "task10ms_consumer_cycle",
                    {"field": "steps"},
                    {"member": _three_keys(pair)},
                    {"field": field},
                ),
            ),
        )
    yield Rejection(
        name="broken-reference/memory-pool",
        operations=(_create_in("swc", "ExampleSWC", [{"field": "memory"}, {"key": "NoSuchPool"}], 16),),
        expected_class="invalid",
        expected_error="broken-reference",
        # Every `hardware.*` reference rule names the layer beside the referrers, so a client knows
        # where to supply the missing element.
        expected_addresses=(
            _at("swc", "ExampleSWC", {"field": "memory"}, {"key": "NoSuchPool"}),
            {"layer": "hardware"},
        ),
    )
    yield Rejection(
        # A value type's `base` resolves in `base_types` alone, so a value type over a value type is
        # a base resolving nowhere, `data-type.value-type-undeclared-base`, answered at the referrer.
        name="broken-reference/value-type-base-undeclared",
        operations=(
            _create(
                "implementation_types",
                "ProbeValueA",
                {"name": "ProbeValueA", "kind": "value_type", "base": "float32_base"},
            ),
            _create(
                "implementation_types",
                "ProbeValueB",
                {"name": "ProbeValueB", "kind": "value_type", "base": "ProbeValueA"},
            ),
        ),
        expected_class="invalid",
        expected_error="broken-reference",
        expected_addresses=(_at("implementation_types", "ProbeValueB", {"field": "base"}),),
    )


def _constraint_rejections() -> Iterator[Rejection]:
    """One change set per cross-layer check that ``consistency-violation`` answers.

    They are not ``broken-reference`` because every reference in them resolves: what fails is a
    constraint over resolved values, a uniqueness rule over names, or arithmetic over the assembled model.
    """
    yield Rejection(
        name="consistency-violation/second-root-composition",
        operations=(
            # A second `root: true` composition with only the required `title`, so the ONLY fault is
            # the cardinality (`composition.multiple-roots`); also the vector
            # `negative/consistency-violation-second-root.json`.
            _create("compositions", "Root2", {"name": "Root2", "title": "A second root", "root": True}),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        # Both compositions are named: exactly one declares the root, and the rule chooses neither.
        expected_addresses=(_at("compositions", "Root"), _at("compositions", "Root2")),
    )
    yield Rejection(
        name="consistency-violation/connector-endpoint-direction",
        operations=(
            _update("connectors", "Example_to_Consumer", [{"field": "to"}, {"field": "swc"}], "ExampleSWC"),
            _update("connectors", "Example_to_Consumer", [{"field": "to"}, {"field": "port"}], "pOut"),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        expected_addresses=(_at("connectors", "Example_to_Consumer", {"field": "to"}, {"field": "port"}),),
    )
    yield Rejection(
        name="consistency-violation/data-type-cycle",
        operations=(
            # A record is an element of `application_types`.
            _create(
                "application_types",
                "ProbeRecord",
                {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "self_", "type": "ProbeRecord"}]},
            ),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        expected_addresses=(_at("application_types", "ProbeRecord"),),
    )
    yield Rejection(
        name="consistency-violation/task-period-multiple",
        operations=(_update("tasks", "Task_10ms", [{"field": "period"}], 0.003),),
        expected_class="invalid",
        expected_error="consistency-violation",
        # Both ends of the arithmetic: the task period that moved, and the event
        # period it no longer divides. Either is a legal repair.
        expected_addresses=(
            _at("tasks", "Task_10ms", {"field": "period"}),
            _at(
                "swc",
                "ExampleConsumerSWC",
                {"field": "runnables"},
                {"member": "ExampleConsumerRun"},
                {"field": "events"},
                {"member": {"type": "timing", "period": 0.01}},
            ),
        ),
    )
    yield Rejection(
        name="consistency-violation/runnable-mapped-to-two-tasks",
        operations=(
            _create(
                "tasks",
                "Task_Probe",
                {
                    "name": "Task_Probe",
                    "period": 0.01,
                    "priority": 4,
                    "partition": "PARTITION0",
                    "wcet": "100us",
                    "runnables": [dict(_MAPPED_PAIR)],
                },
            ),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        # Both mappings that claim the pair: the protocol cannot say which the repair drops.
        expected_addresses=(
            _at("tasks", "Task_10ms", {"field": "runnables"}, {"member": _three_keys(_MAPPED_PAIR)}),
            _at("tasks", "Task_Probe", {"field": "runnables"}, {"member": _three_keys(_MAPPED_PAIR)}),
        ),
    )
    yield Rejection(
        name="consistency-violation/sequence-step-order",
        operations=(
            _create_in(
                "tasks",
                "Task_10ms",
                [{"field": "runnables"}, {"member": {"swc": "ExampleSWC", "runnable": "ExampleRun"}}],
                {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            ),
            _create_in(
                "sequences",
                "task10ms_consumer_cycle",
                [{"field": "steps"}, {"member": {"swc": "ExampleSWC", "runnable": "ExampleRun"}}],
                {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            ),
            # A create appends, so the steps stay in order; the breaking reordering is the one change
            # no address expresses, the whole collection's `update` with its membership preserved.
            _update(
                "sequences",
                "task10ms_consumer_cycle",
                [{"field": "steps"}],
                [{"swc": "ExampleSWC", "runnable": "ExampleRun"}, dict(_MAPPED_PAIR)],
            ),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        # The whole collection: an address has no ordinal, so reordering updates `steps` as a whole.
        expected_addresses=(_at("sequences", "task10ms_consumer_cycle", {"field": "steps"}),),
    )
    yield Rejection(
        name="consistency-violation/memory-threshold-breach",
        operations=(_update("hardware", "RAM0", [{"field": "size"}], 1024),),
        expected_class="invalid",
        expected_error="consistency-violation",
        expected_addresses=(
            _at("hardware", "RAM0", {"field": "size"}),
            _at("swc", "ExampleSWC", {"field": "memory"}, {"key": "RAM0"}),
            _at("swc", "ExampleConsumerSWC", {"field": "memory"}, {"key": "RAM0"}),
        ),
    )
    # A second core named `CORE0` is `identity-conflict` at address validation, so no duplicate-id
    # case belongs here; `hardware.duplicate-name` is exercised by
    # `packages/tools/tests/test_hardware_topology.py`.
    yield Rejection(
        name="consistency-violation/duplicate-port-name",
        # A duplicate cannot be introduced into an existing collection (a held member is
        # `identity-conflict`, a membership-changing `update` is refused), so the element declares it
        # twice in its own create payload.
        operations=(
            _create(
                "swc",
                "ProbeSWC",
                {
                    "name": "ProbeSWC",
                    "group": "example",
                    "type": "application",
                    "memory": {"RAM0": 512},
                    "ports": [
                        {"name": "pProbe", "direction": "provide", "interface": "IExample"},
                        {"name": "pProbe", "direction": "provide", "interface": "IExample"},
                    ],
                    # Carried so the duplicate is the one finding.
                    "handle_termination_and_restart": "no_support",
                    # Carried so this vector's payload does not move.
                    "satisfies": ["REQ_EXAMPLE_001@1"],
                },
            ),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        expected_addresses=(_at("swc", "ProbeSWC", {"field": "ports"}, {"member": "pProbe"}),),
    )


def _stale_rejections() -> Iterator[Rejection]:
    """A change set stated against a model that moved between token and dispatch."""
    for element, field, value in MUTABLE_SCALARS:
        yield Rejection(
            name=f"stale/{element.id}.{field}",
            operations=({"operation": "update", "address": element.address(FieldStep(field)), "value": _moved(value)},),
            expected_class="stale",
            stale=True,
        )


def _unauthorized_rejections() -> Iterator[Rejection]:
    """Four scopes missing a well-formed update per element: none, wrong operation, other element, deeper path."""
    for element, field, value in MUTABLE_SCALARS:
        operation = {"operation": "update", "address": element.address(FieldStep(field)), "value": _moved(value)}
        shapes: tuple[tuple[str, tuple[Mapping[str, Any], ...]], ...] = (
            ("empty", ()),
            (
                "wrong-operation",
                ({"operation": "remove", "address": {"layer": element.layer, "element": element.identity}},),
            ),
            (
                "other-element",
                ({"operation": "update", "address": {"layer": element.layer, "element": "SomeOtherElement"}},),
            ),
            (
                "deeper-path",
                (
                    {
                        "operation": "update",
                        "address": element.address(FieldStep(field), FieldStep("deeper")),
                    },
                ),
            ),
        )
        for label, scope in shapes:
            yield Rejection(
                name=f"unauthorized-{label}/{element.id}.{field}",
                operations=(operation,),
                expected_class="unauthorized",
                scope=scope,
            )


def _partial_rejections() -> Iterator[Rejection]:
    """An ``update`` whose address only a *later* operation in the same set creates."""
    payloads: tuple[tuple[str, str, Mapping[str, Any]], ...] = (
        ("implementation_types", "ProbeAlias", {"name": "ProbeAlias", "kind": "value_type", "base": "float32_base"}),
        (
            "interfaces",
            "IProbe",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": "float32"}],
            },
        ),
        ("tasks", "Task_Probe", {"name": "Task_Probe", "period": 0.02, "priority": 3, "partition": "PARTITION0"}),
        (
            "swc",
            "ProbeSWC",
            {"name": "ProbeSWC", "group": "example", "type": "application"},
        ),
    )
    for layer, identity, payload in payloads:
        yield Rejection(
            name=f"partial/{layer}/{identity}",
            operations=(
                {
                    "operation": "update",
                    "address": {"layer": layer, "element": identity, "path": [{"field": "description"}]},
                    "value": "a description of a thing that does not exist yet",
                },
                {"operation": "create", "address": {"layer": layer, "element": identity}, "value": dict(payload)},
            ),
            expected_class="partial",
        )


def _writer_refusal_rejections() -> Iterator[Rejection]:
    """The two conditions a writer refuses before pass D can be asked.

    ``write_element``'s guards run during materialization, so a reserved name and a placement value
    that is not one path segment reach the engine as an exception, translated by
    ``engine._materialization_refusal`` into codes the enum has; a bare ``ValueError`` is forbidden.
    """
    yield Rejection(
        name="reserved-name/data-type-compu-method",
        operations=(
            # Both are elements of `application_types`.
            _create(
                "application_types", "Gear", {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]}
            ),
            _create(
                "application_types",
                "Gear_CompuMethod",
                {"name": "Gear_CompuMethod", "kind": "application_type"},
            ),
        ),
        expected_class="invalid",
        expected_error="consistency-violation",
        # The `COMPU-METHOD` the writer generates for the enum this change set declares claims the
        # name; the guard judges the outcome, so the enum may arrive in the same request.
        # `{x}_CompuMethod` is the one data-type name reserved for a kind, and a form collides only
        # with a type of its owner's package, so the occupant is an application type.
        expected_addresses=(_at("application_types", "Gear_CompuMethod"),),
    )
    for value in NOT_A_SEGMENT:
        yield Rejection(
            name=f"placement-value/{value!r}",
            operations=(
                _create(
                    "swc",
                    "ProbeSWC",
                    {"name": "ProbeSWC", "group": value, "type": "application", "memory": {"RAM": 16}},
                ),
            ),
            expected_class="invalid",
            expected_error="schema-violation",
            # The placement field, never the composed path: "this value is not a legal `group`".
            expected_addresses=(_at("swc", "ProbeSWC", {"field": "group"}),),
        )


def _read_miss_rejections() -> Iterator[Rejection]:
    """A ``read`` that resolves against nothing, at each of the four miss shapes.

    The closed enum covers every rejection, reads included. ``unknown-field`` versus
    ``broken-reference`` is the split passes B and C make (``passes.MISS_CODES``).
    """
    shapes: tuple[tuple[str, Mapping[str, Any], str], ...] = (
        ("no-such-element", _at("swc", "NoSuchSWC"), "broken-reference"),
        ("no-such-field", _at("swc", "ExampleSWC", {"field": "no_such_field"}), "unknown-field"),
        ("no-such-member", _at("swc", "ExampleSWC", {"field": "ports"}, {"member": "noSuchPort"}), "broken-reference"),
        ("no-such-key", _at("swc", "ExampleSWC", {"field": "memory"}, {"key": "NoSuchPool"}), "broken-reference"),
        (
            "not-traversable",
            _at("swc", "ExampleSWC", {"field": "type"}, {"field": "deeper"}),
            "unknown-field",
        ),
    )
    for label, address, code in shapes:
        yield Rejection(
            name=f"read-miss/{label}",
            request={"operation": "read", "address": address},
            expected_class="invalid",
            expected_error=code,
            expected_addresses=(address,),
        )


def _request_of(project_root: Path, rejection: Rejection) -> Mapping[str, Any]:
    """The request one generated rejection dispatches."""
    if rejection.request is not None:
        return deepcopy(rejection.request)
    return _change_set(project_root, *rejection.operations)


def _run(project_root: Path, rejection: Rejection) -> Outcome:
    """Dispatch one generated rejection against *project_root*.

    The token is read before ``make_stale`` moves the model, which is what makes a ``stale`` case
    stale. Each case leaves the model byte-identical, so one project serves a whole loop.
    """
    request = _request_of(project_root, rejection)
    if rejection.stale:
        fixtures.make_stale(project_root)
    return _dispatch(request, project_root, scope=list(rejection.scope) if rejection.scope is not None else None)


REJECTIONS: tuple[Rejection, ...] = (
    *_unknown_field_rejections(),
    *_immutable_field_rejections(),
    *_type_mismatch_rejections(),
    *_identity_conflict_rejections(),
    *_reference_rejections(),
    *_constraint_rejections(),
    *_writer_refusal_rejections(),
    *_read_miss_rejections(),
    *_stale_rejections(),
    *_unauthorized_rejections(),
    *_partial_rejections(),
)


# 1. The model-commit invariant


class TestEveryRejectionLeavesTheModelByteIdentical:
    """The model-commit invariant over the whole tree: a neighbour written before a refusal fails it."""

    def test_the_corpus_is_not_trivially_small(self) -> None:
        # A generator producing nothing would make every property vacuous.
        assert len(REJECTIONS) > 60
        assert len(BASE_ELEMENTS) >= 18

    def test_the_corpus_reaches_all_four_failure_classes(self) -> None:
        declared = {rejection.expected_class for rejection in REJECTIONS if rejection.expected_class}
        assert declared == FAILURE_CLASSES

    @pytest.mark.parametrize("rejection", REJECTIONS, ids=lambda entry: entry.name)
    def test_a_rejected_change_set_moves_no_byte(self, rejection: Rejection, project: Path) -> None:
        request = _request_of(project, rejection)
        if rejection.stale:
            # The token was read from the model this edit moves; compare against the moved state,
            # the one the dispatch starts from.
            fixtures.make_stale(project)
        before = _tree(project / "model")

        outcome = _dispatch(request, project, scope=list(rejection.scope) if rejection.scope is not None else None)

        assert not outcome.committed, f"{rejection.name} was committed: {outcome.describe()}"
        assert _tree(project / "model") == before, f"{rejection.name} moved the model: {outcome.describe()}"

    @pytest.mark.parametrize("rejection", REJECTIONS, ids=lambda entry: entry.name)
    def test_a_rejection_is_answered_in_the_closed_error_vocabulary(self, rejection: Rejection, project: Path) -> None:
        outcome = _run(project, rejection)

        # No fault is declared in this corpus: a `None` response is a defect.
        assert outcome.response is not None, f"{rejection.name} raised instead of answering: {outcome.describe()}"
        assert outcome.error in ERROR_CLASS_MAP, outcome.describe()
        # `failure_class` is derived from the map, the source of truth; a response may not contradict it.
        assert outcome.failure_class == ERROR_CLASS_MAP[outcome.error]
        if rejection.expected_class is not None:
            assert outcome.failure_class == rejection.expected_class, outcome.describe()

    def test_the_audit_log_is_the_only_thing_a_rejection_may_write(self, project: Path) -> None:
        """The model stays byte-identical and the audit record is the one write, so a write-nothing engine fails."""
        before = _tree(project)
        for rejection in REJECTIONS:
            if rejection.stale:
                continue  # this family moves the model on purpose, before dispatching
            _run(project, rejection)
        after = _tree(project)

        # `as_posix()`, not `str()`: `_tree` spells keys POSIX, and `str(Path("dist") / "audit.jsonl")`
        # is `dist\\audit.jsonl` on Windows. `test_platform_io.py`'s Rule P scan does not reach a bare
        # `str(path)` compared with a POSIX literal.
        assert set(after) - set(before) == {(Path("dist") / "audit.jsonl").as_posix()}
        assert {name: data for name, data in after.items() if name in before} == before


# 1b. Pass D answers every validator refusal from the closed enum

#: The generated single-field updates whose staged result is refused, by corpus id and the code
#: that must answer (moving a task's ``period`` off the periods it drives is a real cross-layer
#: violation). An EXACT set: a newly refused or a newly accepted scalar both fail.
REFUSED_SCALARS: Mapping[str, str] = {
    # Pass B refuses the value itself: `type` is an enum and `threshold` is bounded.
    "swc/ExampleSWC.type": "schema-violation",
    "swc/ExampleConsumerSWC.type": "schema-violation",
    "hardware/CORE0.threshold": "schema-violation",
    "hardware/RAM0.type": "schema-violation",
    # Pass D: the sequence's own `task` no longer resolves.
    "sequences/task10ms_consumer_cycle.task": "broken-reference",
    # Pass D: `_moved` renames a partition's `core` to a core nobody declares
    # (`hardware.partition-undeclared-core`).
    "hardware/PARTITION0.core": "broken-reference",
    # Pass D: `_moved` renames a value type's `base` to a type nobody declares
    # (`data-type.value-type-undeclared-base`), one row per value type of the reference model.
    **{
        f"implementation_types/{name}.base": "broken-reference"
        for name in (
            "bool",
            "boolean",
            "float32",
            "float64",
            "int16",
            "int32",
            "int64",
            "int8",
            "uint16",
            "uint32",
            "uint64",
            "uint8",
        )
    },
    # Pass D: every reference resolves and the arithmetic over the assembled model does not.
    "tasks/Task_10ms.period": "consistency-violation",
}


def _all_addresses(response: Mapping[str, Any]) -> list[str]:
    """Every ``errorResult`` address, primary and ``additional``, as sorted ``repr``s (no stated order)."""
    details = [response] + list(response.get("additional", ()))
    return sorted(repr(address) for detail in details for address in detail.get("addresses", ()))


class TestEveryValidatorRefusalIsAnsweredInTheClosedVocabulary:
    """Every validator refusal is answered in the closed vocabulary.

    * every generated rejection is answered, with a code and the addresses responsible; nothing raises;
    * a dangling reference is answered at the referrers, an assembled-result violation at the elements
      the rule is about;
    * ``UndiagnosedInconsistency`` means only an engine/validator disagreement, a rule
      ``diagnosis.RULE_CODES`` does not name, which
      ``packages/tools/tests/test_validator_rule_coverage.py`` makes a failing test.
    """

    def test_the_corpus_covers_both_pass_d_codes_it_can_reach(self) -> None:
        # A case per code: the reference rules and the assembled-result rules each need evidence.
        answered = {entry.expected_error for entry in REJECTIONS if entry.expected_error}
        assert {"broken-reference", "consistency-violation"} <= answered

    def test_the_corpus_covers_every_validator_rule_group_that_answers(self) -> None:
        # One case per reachable `RULE_CODES` group, so a rule that stops firing cannot hide.
        from swcraft_tools.editing import diagnosis

        assert len([entry for entry in REJECTIONS if entry.expected_error == "broken-reference"]) >= 9
        # The constraint family alone: a reserved-name materialization refusal also carries
        # `consistency-violation`.
        constraints = [entry for entry in _constraint_rejections() if entry.expected_error == "consistency-violation"]
        # One case per constraint check reachable through a change set: a check added or retired moves
        # the count. `hardware.duplicate-name` is not reachable (a held identity is `identity-conflict`).
        assert len(constraints) == 8
        # The error rules alone: pass D reads the error half of a result, so a warning rule has no code.
        error_rules = {rule for rule in CONSISTENCY_RULES if severity_of(rule) == SEVERITY_ERROR}
        assert set(diagnosis.RULE_CODES) == error_rules, (
            "the reverse coverage relation is `test_validator_rule_coverage.py`'s to state; this is the "
            "cross-check that the corpus here is written against the same table"
        )

    @pytest.mark.parametrize(
        "rejection", [entry for entry in REJECTIONS if entry.expected_error], ids=lambda entry: entry.name
    )
    def test_a_refusal_is_answered_at_the_addresses_responsible(self, rejection: Rejection, project: Path) -> None:
        outcome = _run(project, rejection)

        assert outcome.response is not None, f"{rejection.name}: {outcome.describe()}"
        assert outcome.error == rejection.expected_error, outcome.describe()
        assert rejection.expected_addresses is not None
        assert _all_addresses(outcome.response) == sorted(map(repr, rejection.expected_addresses)), outcome.describe()

    @pytest.mark.parametrize("rejection", REJECTIONS, ids=lambda entry: entry.name)
    def test_no_rejection_raises_at_all(self, rejection: Rejection, project: Path) -> None:
        """No exception at all: a bare ``ValueError``, a ``PlacementRefusal`` or a typed read miss fails as well."""
        outcome = _run(project, rejection)

        assert outcome.raised is None, (
            f"{rejection.name} raised instead of answering: {outcome.describe()}.\n"
            f"Every rejection is one `errorResult` with a code from the protocol's closed error-code enum. If the "
            f"exception is an `UndiagnosedInconsistency`, a validator rule has no protocol code: every error rule "
            f"`validator.CONSISTENCY_RULES` declares must be in `diagnosis.RULE_CODES`, and if none of the codes "
            f"fits, the enum needs another member — the decision record's error-code block, its prose bullet, "
            f"`protocol.schema.json`, `ERROR_CLASS_MAP` and a vector, all in one change. If it is anything else, "
            f"a condition reaches the client as an exception instead of as an answer."
        )
        # Its own statement: the one exception class with a named remedy.
        assert not isinstance(outcome.raised, UndiagnosedInconsistency)

    def test_a_generated_field_update_is_answered_from_the_closed_enum(self, project: Path) -> None:
        """Accepted-shape corpus: a refused ``MUTABLE_SCALARS`` update gets :data:`REFUSED_SCALARS`'s code, no raise."""
        observed: dict[str, str] = {}
        for element, field, value in MUTABLE_SCALARS:
            request = _change_set(
                project, {"operation": "update", "address": element.address(FieldStep(field)), "value": _moved(value)}
            )
            # No review boundary: staged only, against one unmoved model.
            outcome = _dispatch(request, project, review=None)
            case = f"{element.id}.{field}"
            assert outcome.response is not None, f"{case}: {outcome.describe()}"
            if outcome.response.get("status") != "staged":
                assert outcome.error in ERROR_CLASS_MAP, f"{case}: {outcome.describe()}"
                observed[case] = str(outcome.error)

        assert observed == dict(REFUSED_SCALARS)


#: A rule shipped as a warning (`validator.WARNING_RULES`), the only finding the ``REQ_PROBE``
#: create leaves.
_THE_WARNING_RULE = "trace.requirement-unallocated"

#: A requirement no element satisfies: `trace.requirement-unallocated`'s one finding.
_UNSATISFIED_PROBE_REQUIREMENT: dict[str, Any] = {
    "id": "REQ_PROBE",
    "title": "Probe",
    "description": "No element satisfies this requirement.",
    "version": 1,
    "source": "architecture",
}

#: A ``ProbeSWC`` satisfying no requirement (no finding), with the ``RAM0`` budget and termination
#: handling carried so no other rule fires.
_UNLINKED_PROBE_SWC: dict[str, Any] = {
    "name": "ProbeSWC",
    "group": "example",
    "type": "application",
    "memory": {"RAM0": 512},
    "handle_termination_and_restart": "no_support",
}


class TestPassDJudgesErrorFindingsOnly:
    """A warning is not a refusal, and the response never names one.

    ``pass_d`` reads ``consistent`` and ``diagnose`` the error half of a
    :class:`~swcraft_tools.validator.ConsistencyResult`: warnings alone commit, errors with warnings
    are refused for the errors. Driven through the approving boundary, since only a commit leaves
    the warning standing in the real model.
    """

    def test_a_change_set_whose_only_finding_is_a_warning_commits(self, project: Path) -> None:
        request = _change_set(project, _create("requirements", "REQ_PROBE", _UNSATISFIED_PROBE_REQUIREMENT))

        outcome = _dispatch(request, project)

        assert outcome.committed, outcome.describe()
        # The response carries no warning, not the word anywhere; `validate` reports it.
        assert outcome.response is not None
        assert set(outcome.response) == {"response", "status", "baseline", "delta", "audit_ref"}
        assert "warning" not in json.dumps(outcome.response).lower()
        result = check_cross_layer_consistency(str(project / "model"))
        assert result.consistent is True
        assert result.records == ()
        assert [
            (record.rule, {(subject.layer, subject.element) for subject in record.subjects})
            for record in result.warnings
        ] == [(_THE_WARNING_RULE, {("requirements", "REQ_PROBE")})]

    def test_a_change_set_creating_a_component_that_satisfies_nothing_commits_with_no_finding(
        self, project: Path
    ) -> None:
        """The unlinked ``ProbeSWC`` create commits and leaves no finding at all."""
        outcome = _dispatch(_change_set(project, _create("swc", "ProbeSWC", _UNLINKED_PROBE_SWC)), project)

        assert outcome.committed, outcome.describe()
        result = check_cross_layer_consistency(str(project / "model"))
        assert result.consistent is True
        assert result.records == ()
        assert result.warnings == ()

    def test_errors_and_warnings_together_are_refused_for_the_errors_alone(self, project: Path) -> None:
        before = _tree(project / "model")
        # The warning's create with one error beside it (`port.undeclared-interface`).
        payload = {
            **_UNLINKED_PROBE_SWC,
            "ports": [{"name": "pProbe", "direction": "provide", "interface": "INoSuchInterface"}],
        }

        outcome = _dispatch(
            _change_set(
                project,
                _create("requirements", "REQ_PROBE", _UNSATISFIED_PROBE_REQUIREMENT),
                _create("swc", "ProbeSWC", payload),
            ),
            project,
        )

        assert outcome.response is not None, outcome.describe()
        assert outcome.error == "broken-reference", outcome.describe()
        assert outcome.response.get("additional", []) == [], outcome.describe()
        assert _all_addresses(outcome.response) == sorted(
            map(repr, (_at("swc", "ProbeSWC", {"field": "ports"}, {"member": "pProbe"}, {"field": "interface"}),))
        ), outcome.describe()
        assert _tree(project / "model") == before, outcome.describe()


# 2. The security invariants


#: Response keys whose value is prose rather than contract: a description may quote a path, and
#: ``deltaEntry.summary`` renders ``layer/element``. Narrow: the project's own location is refused
#: everywhere, prose included.
PROSE_KEYS = frozenset({"description", "summary", "detail", "message", "note"})


def _strings(value: Any, *, key: str | None = None) -> Iterator[tuple[str | None, str]]:
    """Every string in a response as ``(the key it sits under, the text)``."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, Mapping):
        for name, item in value.items():
            yield None, str(name)
            yield from _strings(item, key=str(name))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _strings(item, key=key)


def _texts(payload: Any) -> list[str]:
    return [text for _, text in _strings(payload)]


def _leaks(payload: Any) -> list[str]:
    return [
        text
        for key, text in _strings(payload)
        if key not in PROSE_KEYS and any(marker in text for marker in STORAGE_MARKERS)
    ]


def _read_addresses() -> Iterator[dict[str, Any]]:
    """Every address the reference model can be read at: layer, element, field."""
    for layer_name in reg.REGISTRY.layer_names:
        yield {"layer": layer_name}
    for element in BASE_ELEMENTS:
        yield {"layer": element.layer, "element": element.identity}
        for field in reg.REGISTRY.all_fields(element.kind):
            yield {"layer": element.layer, "element": element.identity, "path": [{"field": field}]}


READ_ADDRESSES: tuple[dict[str, Any], ...] = tuple(_read_addresses())

#: Every readable address in every form plus the default: a layer and a collection default to
#: ``summary``, so the default alone would never reach ``full`` or ``index``.
READ_REQUESTS: tuple[dict[str, Any], ...] = tuple(
    {"operation": "read", "address": address} | ({} if form is None else {"form": form})
    for address in READ_ADDRESSES
    for form in (None, *READ_FORMS)
)


class TestNoResponseCarriesAStoragePath:
    """No response carries a storage path: discovery, every readable address, the generated change sets."""

    def test_the_corpus_covers_the_whole_readable_model(self) -> None:
        assert len(READ_ADDRESSES) > 80
        # Four requests per address: a corpus of response SHAPES.
        assert len(READ_REQUESTS) == len(READ_ADDRESSES) * (len(READ_FORMS) + 1)

    def test_no_discover_response_carries_one(self, project: Path) -> None:
        requests = [{"operation": "discover"}] + [
            {"operation": "discover", "layer": name} for name in reg.REGISTRY.layer_names
        ]
        for request in requests:
            assert not _leaks(handle_request(request, project=project)), request

    def test_no_read_response_carries_one(self, project: Path) -> None:
        for request in READ_REQUESTS:
            assert not _leaks(handle_request(request, project=project)), request

    def test_no_change_set_response_carries_one(self, project: Path) -> None:
        for rejection in REJECTIONS:
            outcome = _run(project, rejection)
            if outcome.response is not None:
                assert not _leaks(outcome.response), rejection.name

    def test_no_raised_exception_carries_one_either(self, project: Path) -> None:
        # A raised exception reaches a log and a terminal as a response reaches a client (a reader
        # error must not name the absolute path of the private `mkdtemp` shadow tree).
        root = project.resolve()
        raised = [
            outcome.raised for rejection in REJECTIONS if (outcome := _run(project, rejection)).raised is not None
        ]
        for fault in raised:
            assert str(root) not in str(fault), fault
            assert root.name not in str(fault), fault

    def test_no_accepted_change_set_response_carries_one(self, project: Path) -> None:
        """A well-formed update of every element's first mutable scalar names no path.

        At least half of the stageable cases must stage. The rows a value or reference type's moved
        `base` makes refused by construction (`data-type.value-type-undeclared-base`) are counted
        separately, asserted to be at least the number of such types; lowering the floor instead would
        hide the next real refusal. Does not reach: a stageable row refused for another reason counts
        against the floor.
        """
        staged = 0
        role_refused = 0
        for element, field, value in MUTABLE_SCALARS:
            request = _change_set(
                project,
                {"operation": "update", "address": element.address(FieldStep(field)), "value": _moved(value)},
            )
            # No review boundary: staged only, against one unmoved model.
            outcome = _dispatch(request, project, review=None)
            if outcome.response is None:
                continue
            staged += outcome.response.get("status") == "staged"
            role_refused += (
                element.kind in ROLE_BASE_KINDS
                and field == "base"
                and outcome.response.get("error") == "broken-reference"
            )
            assert not _leaks(outcome.response), element.id
        moved_bases = sum(
            1 for element, field, _ in MUTABLE_SCALARS if element.kind in ROLE_BASE_KINDS and field == "base"
        )
        assert moved_bases == 12
        assert role_refused >= moved_bases
        assert staged >= (len(MUTABLE_SCALARS) - role_refused) // 2

    @pytest.mark.parametrize("vector_name", ["create-at-occupied-storage-target", "update-at-relocated-storage-target"])
    def test_an_exchange_whose_file_is_not_named_after_its_element_carries_none(
        self, vector_name: str, tmp_path: Path
    ) -> None:
        # A create that took a collision suffix and an update of a file named otherwise name no file:
        # the file is in the audit log's written-target record only.
        vector = fixtures.VECTORS[vector_name]
        project_root = fixtures.build_fixture(tmp_path / PROJECT_DIRNAME, vector_name)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project_root))
        outcome = _dispatch(request, project_root)

        assert outcome.response is not None, outcome.describe()
        assert outcome.response.get("status") == "committed", outcome.describe()
        assert not _leaks(outcome.response), outcome.response
        assert all("~1" not in text and "Speed_kph.yaml" not in text for text in _texts(outcome.response))

    def test_a_nested_placement_is_not_published_as_a_group(self, project: Path) -> None:
        # The reference model stores every SWC flat or one directory deep, so `group` never held a
        # `/`. `model/swc/a/b/Foo.yaml` would publish `group: "a/b"`, a fragment of its storage path;
        # planting the layout makes the marker table reach it (revert `read._publishable` and this fails).
        nested = project / "model" / "swc" / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "NestedSWC.yaml").write_text(
            "name: NestedSWC\ntype: application\nports: []\nrunnables: []\n", encoding="utf-8"
        )
        element = {"layer": "swc", "element": "NestedSWC"}
        responses = [
            handle_request({"operation": "read", "address": element}, project=project),
            # The layer read is the other shape that puts a `group` in a response.
            handle_request({"operation": "read", "address": {"layer": "swc"}}, project=project),
        ]
        for response in responses:
            assert not _leaks(response), response
            # Refused, with the code and address the write side gives the identical value.
            assert response["error"] == "schema-violation"
            assert response["addresses"] == [{"layer": "swc", "element": "NestedSWC", "path": [{"field": "group"}]}]

    def test_no_response_of_any_shape_carries_the_projects_own_location(self, project: Path) -> None:
        # The fixture root is a tmp path no model string can hold, so any component of it is a leak.
        root = project.resolve()
        payloads: list[Any] = [handle_request({"operation": "discover"}, project=project)]
        payloads += [handle_request(request, project=project) for request in READ_REQUESTS]
        payloads += [
            outcome.response for rejection in REJECTIONS if (outcome := _run(project, rejection)).response is not None
        ]
        for payload in payloads:
            for text in _texts(payload):
                assert str(root) not in text
                assert root.name not in text


# 2a. The same invariant over the external stubs


def _is_an_autosar_reference(text: str, project_root: Path) -> bool:
    """*text* spelled as an absolute AUTOSAR reference path that names no location on this host.

    A stub's ``external_ref`` looks like an absolute POSIX path but names an element of another ARXML
    document. Narrow: the whole string must match the reference grammar (every segment an
    Identifier), must not begin with or hold the project's location or directory name, and must not
    exist on this host, the MCP reply gate's rule for the same spelling.
    """
    if not text.startswith("/") or REF_PATH.fullmatch(text) is None:
        return False
    spellings = {project_root.as_posix(), project_root.resolve().as_posix()}
    if any(text.startswith(spelling) for spelling in spellings) or project_root.name in text:
        return False
    return not Path(text).exists()


def _leaks_beside_references(payload: Any, project_root: Path) -> list[str]:
    """:func:`_leaks`, less the strings that are AUTOSAR reference paths (:func:`_is_an_autosar_reference`)."""
    return [text for text in _leaks(payload) if not _is_an_autosar_reference(text, project_root)]


@pytest.fixture
def stub_project(tmp_path: Path) -> Path:
    """``external_stubs_support``'s stub-bearing project, under :data:`PROJECT_DIRNAME`."""
    return stubs.external_stubs_model_root(tmp_path / PROJECT_DIRNAME).parent


def _stub_read_requests() -> list[dict[str, Any]]:
    """Every read that reaches a stub or a port on one, in every form and the default."""
    addresses: list[dict[str, Any]] = [
        {"layer": layer} for layer in ("interfaces", "implementation_types", "swc", "compositions")
    ]
    stub_elements = [("interfaces", name, "external_interface") for name, _ref, _dest in stubs.INTERFACE_STUBS]
    stub_elements.append(("implementation_types", stubs.TYPE_STUB[0], "external_type"))
    for layer, name, kind in stub_elements:
        addresses.append({"layer": layer, "element": name})
        addresses += [
            {"layer": layer, "element": name, "path": [{"field": field}]} for field in reg.REGISTRY.all_fields(kind)
        ]
    addresses += [
        {"layer": "implementation_types", "element": "RefForeign_T"},
        {"layer": "swc", "element": "ExampleSWC"},
        {"layer": "swc", "element": "ExampleConsumerSWC"},
        {"layer": "compositions", "element": "Root"},
    ]
    return [
        {"operation": "read", "address": address} | ({} if form is None else {"form": form})
        for address in addresses
        for form in (None, *READ_FORMS)
    ]


#: One change set per answer a stub can be given: ``(label, operation, status or error code)``,
#: staged only, against the one unmoved stub model.
STUB_CHANGE_SETS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "create an interface stub",
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IForeignNew"},
            "value": {
                "name": "IForeignNew",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IForeignNew",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        },
        "staged",
    ),
    (
        "move a stub's reference path",
        {
            "operation": "update",
            "address": {"layer": "interfaces", "element": "IForeignSR", "path": [{"field": "external_ref"}]},
            "value": "/Elsewhere/PortInterfaces/IForeignSR",
        },
        "staged",
    ),
    (
        "create a type stub whose DEST is not admitted",
        {
            "operation": "create",
            "address": {"layer": "implementation_types", "element": "ForeignU"},
            "value": {
                "name": "ForeignU",
                "kind": "external_type",
                "external_ref": "/Foreign/ImplementationDataTypes/ForeignU",
                "external_dest": "SW-BASE-TYPE",
            },
        },
        "consistency-violation",
    ),
    (
        "create an interface stub with a path that is no reference",
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IForeignBad"},
            "value": {
                "name": "IForeignBad",
                "kind": "external_interface",
                "external_ref": "/Foreign/Port-Interfaces/IForeignBad",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        },
        "consistency-violation",
    ),
    (
        "create a stub whose name is no Identifier",
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "I Foreign"},
            "value": {
                "name": "I Foreign",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IForeign",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        },
        "invalid-identifier",
    ),
    (
        "create an interface whose init is typed by a stub",
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IInitOnStub"},
            "value": {
                "name": "IInitOnStub",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": stubs.TYPE_STUB[0], "init": 1}],
            },
        },
        "consistency-violation",
    ),
    (
        "remove a stub the ports still name",
        {"operation": "remove", "address": {"layer": "interfaces", "element": "IForeignSR"}},
        "broken-reference",
    ),
)


class TestNoResponseOverAnExternalStubCarriesAStoragePath:
    """No response over an external stub carries a storage path.

    The reference model holds no stub, so its sweep never met an ``external_ref``. Every read reaching a
    stub and one change set per answer are scanned with the same markers, less
    :func:`_is_an_autosar_reference`; the exemption is exercised and passes nothing naming this project.
    Does not reach a stub shape ``external_stubs_support`` does not build, or the MCP tools' replies
    (``test_no_tool_reply_carries_a_path.py``).
    """

    def test_no_read_of_a_stub_carries_one(self, stub_project: Path) -> None:
        references = 0
        for request in _stub_read_requests():
            response = handle_request(request, project=stub_project)
            assert response.get("response") == "read", (request, response)
            assert not _leaks_beside_references(response, stub_project), request
            references += bool(_leaks(response))
        # Non-vacuous: the exemption is what lets these through, not the absence of a path.
        assert references >= len(READ_FORMS), references

    @pytest.mark.parametrize(
        ("label", "operation", "answer"), STUB_CHANGE_SETS, ids=[row[0] for row in STUB_CHANGE_SETS]
    )
    def test_no_change_set_answer_over_a_stub_carries_one(
        self, stub_project: Path, label: str, operation: dict[str, Any], answer: str
    ) -> None:
        outcome = _dispatch(_change_set(stub_project, operation), stub_project, review=None)
        assert outcome.response is not None, f"{label}: {outcome.describe()}"
        assert answer in (outcome.response.get("status"), outcome.error), f"{label}: {outcome.describe()}"
        assert not _leaks_beside_references(outcome.response, stub_project), outcome.response
        root = stub_project.resolve()
        for text in _texts(outcome.response):
            assert str(root) not in text and root.name not in text, text

    def test_the_exemption_passes_a_reference_and_nothing_that_names_a_location(self, stub_project: Path) -> None:
        reference = "/Foreign/PortInterfaces/IForeignSR"
        assert _leaks({"value": reference}) == [reference]
        assert _leaks_beside_references({"value": reference}, stub_project) == []
        # An existing directory in the reference grammar is a location here and never exempt. `/tmp`
        # names nothing on Windows, so it is correctly exempt there: probed, not assumed by platform.
        existing_rooted = ("/tmp",) if Path("/tmp").is_dir() else ()
        for location in (
            (stub_project / "model").as_posix(),
            (stub_project.resolve() / "model").as_posix(),
            "interfaces/IForeignSR.yaml",
            "/interfaces/IForeignSR.yaml",
            *existing_rooted,
            "C:\\Users\\someone\\project",
        ):
            assert _leaks_beside_references({"value": location}, stub_project) == [location], location


# 2b. The same invariant over the implementation composites


#: The three implementation composite kinds, and the member kind of the record.
IMPL_COMPOSITE_KINDS = ("implementation_record", "implementation_array", "pointer")


@pytest.fixture
def impl_project(tmp_path: Path) -> Path:
    """``impl_composites_support``'s project, under :data:`PROJECT_DIRNAME`."""
    return composites.impl_composites_model_root(tmp_path / PROJECT_DIRNAME).parent


def _impl_composite_read_requests() -> list[dict[str, Any]]:
    """Every read that reaches one of the three kinds, in every form and the default."""
    addresses: list[dict[str, Any]] = [
        {"layer": layer} for layer in ("implementation_types", "mapping_sets", "interfaces")
    ]
    slices = [{"operation": "read", "address": {"layer": "implementation_types"}, "members": "elements"}]
    for document in composites.DATA_TYPES:
        name, kind = document["name"], document["kind"]
        if kind not in IMPL_COMPOSITE_KINDS:
            continue
        addresses.append({"layer": "implementation_types", "element": name})
        addresses += [
            {"layer": "implementation_types", "element": name, "path": [{"field": field}]}
            for field in reg.REGISTRY.all_fields(kind)
        ]
        for member in document.get("elements", ()):
            step = [{"field": "elements"}, {"member": member["name"]}]
            addresses.append({"layer": "implementation_types", "element": name, "path": step})
            addresses += [
                {"layer": "implementation_types", "element": name, "path": [*step, {"field": field}]}
                for field in reg.REGISTRY.all_fields("implementation_element")
            ]
    addresses += [{"layer": "implementation_types", "element": alias} for alias in ("WheelSpeeds_Ref", "Frame_Ref")]
    requests = [{"operation": "read", "address": address} for address in addresses] + slices
    return [request | ({} if form is None else {"form": form}) for request in requests for form in (None, *READ_FORMS)]


def _impl_create(document: dict[str, Any], layer: str = "implementation_types") -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": document["name"]}, "value": document}


#: One change set per answer the three kinds can be given: ``(label, operation, status or error
#: code)``, staged only. Each refusal code they reach is here once; the rule matrices are
#: ``test_impl_composites_editing.py``'s and ``test_impl_composites_validator.py``'s.
IMPL_COMPOSITE_CHANGE_SETS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "create an implementation record",
        _impl_create(
            {"name": "Probe_T", "kind": "implementation_record", "elements": [{"name": "a", "type": "uint8"}]}
        ),
        "staged",
    ),
    (
        "create an implementation array of a record",
        _impl_create(
            {
                "name": "Probes_T",
                "kind": "implementation_array",
                "element_type": "WheelSpeeds_T",
                "length": 2,
                "element_name": "Probe",
            }
        ),
        "staged",
    ),
    (
        "create a pointer to an array",
        _impl_create({"name": "Probes_Ptr", "kind": "pointer", "target": "WheelSpeedHistory_T"}),
        "staged",
    ),
    (
        "move a pointer's target",
        {
            "operation": "update",
            "address": {"layer": "implementation_types", "element": "WheelSpeeds_Ptr", "path": [{"field": "target"}]},
            "value": "Frame_T",
        },
        "staged",
    ),
    (
        "an array of length zero",
        _impl_create(
            {
                "name": "Probes_T",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 0,
                "element_name": "B",
            }
        ),
        "schema-violation",
    ),
    (
        "a key no element declares",
        _impl_create(
            {
                "name": "Probe_T",
                "kind": "implementation_record",
                "elements": [{"name": "a", "type": "uint8", "offset": 0}],
            }
        ),
        "unknown-field",
    ),
    (
        "a pointer over a held name",
        _impl_create({"name": "WheelSpeeds_T", "kind": "pointer", "target": "uint8"}),
        "identity-conflict",
    ),
    (
        "an element name that is no Identifier",
        _impl_create(
            {"name": "Probe_T", "kind": "implementation_record", "elements": [{"name": "a b", "type": "uint8"}]}
        ),
        "invalid-identifier",
    ),
    (
        "an element_name that is no Identifier",
        _impl_create(
            {
                "name": "Probes_T",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 2,
                "element_name": "b c",
            }
        ),
        "invalid-identifier",
    ),
    (
        "a pointer to an undeclared type",
        _impl_create({"name": "Probe_Ptr", "kind": "pointer", "target": "Nope"}),
        "broken-reference",
    ),
    (
        "remove a record the array, the pointer and the alias still name",
        {"operation": "remove", "address": {"layer": "implementation_types", "element": "WheelSpeeds_T"}},
        "broken-reference",
    ),
    (
        "close a pointer cycle through a record's member",
        {
            "operation": "update",
            "address": {"layer": "implementation_types", "element": "WheelSpeeds_Ptr", "path": [{"field": "target"}]},
            "value": "Handle_T",
        },
        "consistency-violation",
    ),
    (
        "map an application record onto a pointer",
        _impl_create(
            {"name": "ProbeMaps", "maps": [{"application": "WheelSpeeds", "implementation": "WheelSpeeds_Ptr"}]},
            layer="mapping_sets",
        ),
        "consistency-violation",
    ),
    (
        "map an application array onto an implementation record",
        _impl_create(
            {"name": "ProbeMaps", "maps": [{"application": "WheelSpeedHistory", "implementation": "WheelSpeeds_T"}]},
            layer="mapping_sets",
        ),
        "consistency-violation",
    ),
)

#: What refusals sharing one code must say, so each row reaches its own rule's message.
IMPL_COMPOSITE_REFUSAL_WORDS: dict[str, str] = {
    "close a pointer cycle through a record's member": "Cycle detected",
    "map an application record onto a pointer": "[constr_1066]",
    "map an application array onto an implementation record": "positional check of a composite map",
    "an element name that is no Identifier": "element name 'a b'",
    "an element_name that is no Identifier": "element name 'b c'",
}


class TestNoResponseOverTheImplementationCompositesCarriesAStoragePath:
    """No response over the implementation composites carries a storage path.

    The reference model declares none of the kinds. Every read reaching them and one change set per
    answer are scanned with the same markers and no exemption. Does not reach a shape
    ``impl_composites_support`` does not build, or the MCP tools' replies
    (``test_no_tool_reply_carries_a_path.py``).
    """

    def test_no_read_of_the_kinds_carries_one(self, impl_project: Path) -> None:
        requests = _impl_composite_read_requests()
        answered = 0
        for request in requests:
            response = handle_request(request, project=impl_project)
            assert response.get("response") == "read", (request, response)
            assert not _leaks(response), request
            answered += 1
        # Non-vacuous: every kind, every field, every member field, in every form.
        assert answered == len(requests) >= 40 * (len(READ_FORMS) + 1)

    @pytest.mark.parametrize(
        ("label", "operation", "answer"), IMPL_COMPOSITE_CHANGE_SETS, ids=[row[0] for row in IMPL_COMPOSITE_CHANGE_SETS]
    )
    def test_no_change_set_answer_over_the_kinds_carries_one(
        self, impl_project: Path, label: str, operation: dict[str, Any], answer: str
    ) -> None:
        outcome = _dispatch(_change_set(impl_project, operation), impl_project, review=None)
        assert outcome.response is not None, f"{label}: {outcome.describe()}"
        assert answer in (outcome.response.get("status"), outcome.error), f"{label}: {outcome.describe()}"
        if label in IMPL_COMPOSITE_REFUSAL_WORDS:
            assert IMPL_COMPOSITE_REFUSAL_WORDS[label] in outcome.response.get("message", ""), outcome.response
        assert not _leaks(outcome.response), outcome.response
        root = impl_project.resolve()
        for text in _texts(outcome.response):
            assert str(root) not in text and root.name not in text, text

    def test_every_refusal_code_the_kinds_reach_is_answered_once(self) -> None:
        """The table is the protocol's answers over the kinds, not a sample of one code."""
        answers = {answer for _label, _operation, answer in IMPL_COMPOSITE_CHANGE_SETS}
        assert answers == {
            "staged",
            "schema-violation",
            "unknown-field",
            "identity-conflict",
            "invalid-identifier",
            "broken-reference",
            "consistency-violation",
        }


class TestAModelThatDoesNotParseIsAnswered:
    """A model file the reader refuses is a protocol answer, never an exception.

    A half-authored project or an interrupted commit's residue is ordinary input, and the reader's
    refusal names the file's absolute path: unanswered, it leaked a path through ``read`` and the
    private shadow tree through a change set, and left no lifecycle record. ``consistency-violation``:
    the client's request is well formed and the defect is the model on disk.
    """

    #: The two ways a model file fails to load (``yaml.YAMLError`` and Pydantic's ``ValidationError``).
    BROKEN = {"malformed-yaml": "name: [oops\n", "missing-required-field": "name: Broken\nperiod: 0.01\n"}

    @staticmethod
    def _break(project: Path, content: str) -> None:
        (project / "model" / "tasks" / "Broken.yaml").write_text(content, encoding="utf-8")

    @pytest.mark.parametrize("kind", sorted(BROKEN))
    def test_read_answers_rather_than_raising(self, project: Path, kind: str) -> None:
        self._break(project, self.BROKEN[kind])
        outcome = _dispatch({"operation": "read", "address": {"layer": "tasks", "element": "Task_10ms"}}, project)
        assert outcome.raised is None, outcome.describe()
        assert outcome.error == "consistency-violation", outcome.describe()
        assert outcome.response is not None
        assert outcome.response["addresses"] == [{"layer": "tasks", "element": "Broken"}]

    @pytest.mark.parametrize("kind", sorted(BROKEN))
    def test_a_change_set_answers_and_leaves_a_lifecycle_record(self, project: Path, kind: str) -> None:
        self._break(project, self.BROKEN[kind])
        request = _change_set(
            project,
            {
                "operation": "update",
                "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
                "value": "moved",
            },
        )
        outcome = _dispatch(request, project)
        assert outcome.raised is None, outcome.describe()
        assert outcome.error == "consistency-violation", outcome.describe()
        entries = [
            entry
            for line in (project / "dist" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if (entry := json.loads(line))["operation"] == "change_set_rejected"
        ]
        assert len(entries) == 1, entries

    @pytest.mark.parametrize("kind", sorted(BROKEN))
    def test_neither_answer_carries_a_path(self, project: Path, kind: str) -> None:
        # The reader's own message is never passed through, the shadow tree's location least of all.
        self._break(project, self.BROKEN[kind])
        root = project.resolve()
        responses = [
            handle_request(
                {"operation": "read", "address": {"layer": "tasks", "element": "Task_10ms"}}, project=project
            ),
            handle_request(
                _change_set(
                    project,
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
                        "value": "moved",
                    },
                ),
                project=project,
            ),
        ]
        for response in responses:
            for text in _texts(response):
                assert str(root) not in text
                assert root.name not in text
                assert ".yaml" not in text
                assert "swcraft-session-" not in text

    @pytest.mark.parametrize("kind", sorted(BROKEN))
    def test_discover_still_answers_normally(self, project: Path, kind: str) -> None:
        # `discover` parses no element, so it needs no guard; asserted so a later change cannot
        # quietly make it need one.
        self._break(project, self.BROKEN[kind])
        response = handle_request({"operation": "discover"}, project=project)
        assert response["response"] == "discover"


class TestNoRequestAcceptsAStoragePath:
    """The protocol accepts addresses and typed payloads only.

    A path-shaped value is refused as an address, an identity or an extra request key with
    ``schema-violation``, never interpreted; the change-set forms leave the model byte-identical.
    """

    PATHS = (
        "swc/example/ExampleSWC.yaml",
        "model/tasks/Task_10ms.yaml",
        "../../etc/passwd",
        "/absolute/model.yaml",
        "swc\\example\\ExampleSWC.yaml",
        ".",
    )

    @pytest.mark.parametrize("path", PATHS)
    def test_an_address_that_is_a_string_is_refused(self, project: Path, path: str) -> None:
        response = handle_request({"operation": "read", "address": path}, project=project)
        assert response["error"] == "schema-violation"

    @pytest.mark.parametrize("path", PATHS)
    def test_a_path_as_an_element_identity_is_refused(self, project: Path, path: str) -> None:
        for layer_name in reg.REGISTRY.layer_names:
            response = handle_request(
                {"operation": "read", "address": {"layer": layer_name, "element": path}}, project=project
            )
            assert response["error"] == "schema-violation", (layer_name, path)

    @pytest.mark.parametrize("key", ["path", "file", "model_root", "source", "target"])
    def test_no_request_shape_has_a_field_that_could_hold_one(self, project: Path, key: str) -> None:
        before = _tree(project / "model")
        requests: tuple[dict[str, Any], ...] = (
            {"operation": "discover", key: "model/swc"},
            {"operation": "read", "address": {"layer": "swc"}, key: "model/swc"},
            {**_change_set(project), key: "model/swc"},
        )
        for request in requests:
            outcome = _dispatch(request, project)
            assert outcome.response is not None and outcome.response["error"] == "schema-violation", request
        assert _tree(project / "model") == before

    @pytest.mark.parametrize("key", ["path", "file", "yaml", "text"])
    def test_no_operation_shape_has_one_either(self, project: Path, key: str) -> None:
        before = _tree(project / "model")
        operation = {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
            "value": 6,
            key: "model/tasks/Task_10ms.yaml",
        }
        outcome = _dispatch(_change_set(project, operation), project)
        assert outcome.response is not None and outcome.response["error"] == "schema-violation"
        assert _tree(project / "model") == before


class TestEveryClientValueThatReachesAPathIsOneSegment:
    """Every client value that reaches a path is one segment; there are exactly two such values.

    The identity is refused by ``$defs/identity``'s pattern, the placement field by the registry's
    segment check. Neither is sanitized, and no path is created anywhere under the project.
    """

    @pytest.mark.parametrize("bad", NOT_A_SEGMENT)
    def test_a_traversal_identity_never_reaches_the_filesystem(self, project: Path, bad: str) -> None:
        before = _tree(project)
        operation = {
            "operation": "create",
            "address": {"layer": "implementation_types", "element": bad},
            "value": {"name": "Probe", "kind": "value_type", "base": "float32_base"},
        }

        outcome = _dispatch(_change_set(project, operation), project)

        assert outcome.response is not None and outcome.response["error"] == "schema-violation", outcome.describe()
        # `as_posix()` for the reason given at the sibling assertion above.
        assert set(_tree(project)) - set(before) <= {(Path("dist") / "audit.jsonl").as_posix()}
        assert {k: v for k, v in _tree(project).items() if k.startswith("model")} == {
            k: v for k, v in before.items() if k.startswith("model")
        }

    @pytest.mark.parametrize("bad", NOT_A_SEGMENT)
    def test_a_placement_value_that_is_not_a_segment_never_reaches_the_filesystem(
        self, project: Path, bad: str
    ) -> None:
        before = _tree(project)
        operation = {
            "operation": "create",
            "address": {"layer": "swc", "element": "ProbeSWC"},
            "value": {"name": "ProbeSWC", "group": bad, "type": "application"},
        }

        outcome = _dispatch(_change_set(project, operation), project)

        # The writer's refusal is a `ValueError` no error class fits (`engine._change_set`).
        assert outcome.response is None or outcome.response.get("error") in ERROR_CLASS_MAP, outcome.describe()
        if outcome.raised is not None:
            assert isinstance(outcome.raised, ValueError), outcome.describe()
        assert not [name for name in _tree(project) if name not in before and "ProbeSWC" in name]
        assert {k: v for k, v in _tree(project).items() if k.startswith("model")} == {
            k: v for k, v in before.items() if k.startswith("model")
        }

    def test_the_two_values_are_the_only_two(self) -> None:
        """There is no third client value that reaches a path.

        The registry composes ``(layer, identity, placement)`` and the layer is a closed vocabulary;
        the composition rule at every layer is pinned in ``test_editing_registry.py``.
        """
        placement_fields = {
            (kind, field.name) for kind in reg.REGISTRY.kinds.values() for field in kind.fields if field.placement
        }
        assert {name for _, name in placement_fields} == {"group"}
        with pytest.raises(PathSegmentError):
            reg.REGISTRY.compose_path("swc", "Probe", {"group": "a/b"})


class TestNoReadMutates:
    """No read mutates the project, audit log included (reads are unaudited), over every readable address."""

    def test_reading_every_address_leaves_the_project_byte_identical(self, project: Path) -> None:
        before = _tree(project)
        for request in READ_REQUESTS:
            handle_request(request, project=project)
        assert _tree(project) == before

    def test_discovering_every_layer_leaves_the_project_byte_identical(self, project: Path) -> None:
        before = _tree(project)
        handle_request({"operation": "discover"}, project=project)
        for name in reg.REGISTRY.layer_names:
            handle_request({"operation": "discover", "layer": name}, project=project)
        assert _tree(project) == before

    def test_a_read_that_misses_writes_nothing_either(self, project: Path) -> None:
        before = _tree(project)
        for layer_name in reg.REGISTRY.layer_names:
            # A miss is `broken-reference`, and leaves no audit record: a refused read has no
            # lifecycle to record either.
            response = handle_request(
                {"operation": "read", "address": {"layer": layer_name, "element": "NoSuchElement"}},
                project=project,
            )
            assert response["error"] == "broken-reference", layer_name
        assert _tree(project) == before


# 5. The exporter regression

#: The Sphinx-Needs capture; the ARXML half is compared with the ``basic-4.2.2`` golden (the README).
REFERENCE_BUILD = Path(__file__).resolve().parent / "fixtures" / "reference_build"

BASIC_MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"


class TestTheExporterProducesTheSameBytesAsBeforeThisPhase:
    """The generated artifacts, each against its committed record.

    ARXML against the ``basic-4.2.2`` golden, Sphinx-Needs output against
    ``fixtures/reference_build/needs.json``; a difference is investigated, never refreshed to pass.
    The generators are invoked directly so the assertion runs in the fast gate and writes into
    ``tmp_path``. The diagrams are held by ``test_diagram_generation.py``'s golden
    ``fixtures/diagrams_golden/basic/``.
    """

    def test_the_arxml_is_byte_identical(self, tmp_path: Path) -> None:
        output = tmp_path / "model.arxml"
        generate_arxml(str(BASIC_MODEL_ROOT), str(output))
        assert output.read_text(encoding="utf-8") == read_golden("basic-4.2.2")

    def test_the_needs_json_is_byte_identical(self, tmp_path: Path) -> None:
        """The Sphinx-Needs JSON: the capture's bytes."""
        output = tmp_path / "needs.json"
        generate_needs(str(BASIC_MODEL_ROOT), str(output), "swcraft-basic-example")
        assert output.read_bytes() == (REFERENCE_BUILD / "needs.json").read_bytes()

    def test_a_read_through_the_protocol_does_not_disturb_the_generators(self, tmp_path: Path) -> None:
        """Generating after protocol reads gives an untouched copy's bytes: reads are no write channel."""
        project = fixtures.build_fixture(tmp_path / "project", BASE_VECTOR)
        untouched = tmp_path / "untouched"
        shutil.copytree(project / "model", untouched)

        for request in READ_REQUESTS:
            handle_request(request, project=project)

        after_reads = tmp_path / "after" / "model.arxml"
        reference = tmp_path / "reference" / "model.arxml"
        generate_arxml(str(project / "model"), str(after_reads))
        generate_arxml(str(untouched), str(reference))
        assert after_reads.read_bytes() == reference.read_bytes(), "model.arxml"

        generate_needs(str(project / "model"), str(tmp_path / "after" / "needs.json"), "project")
        generate_needs(str(untouched), str(tmp_path / "reference" / "needs.json"), "project")
        assert (tmp_path / "after" / "needs.json").read_bytes() == (
            tmp_path / "reference" / "needs.json"
        ).read_bytes(), "the Sphinx-Needs JSON"

        # The diagrams: a directory of views.
        node = requires_node()
        generate_diagrams(str(project / "model"), str(tmp_path / "after" / "views"), "project", node=node)
        generate_diagrams(str(untouched), str(tmp_path / "reference" / "views"), "project", node=node)
        after_views = {entry.name: entry.read_bytes() for entry in (tmp_path / "after" / "views").iterdir()}
        reference_views = {entry.name: entry.read_bytes() for entry in (tmp_path / "reference" / "views").iterdir()}
        assert after_views and after_views == reference_views


# 6. The existing suite's expectations were not weakened

#: The legacy test modules of the areas the editing protocol could have disturbed (differ, session,
#: authorization, reader, writer, CLI, MCP). An expectation may be added, never dropped or disabled.
LEGACY_SUITES: dict[str, tuple[str, ...]] = {
    "packages/tools/tests": (
        "test_differ.py",
        "test_differ_summary.py",
        "test_differ_authorization_compat.py",
        "test_differ_coverage.py",
        "test_session.py",
        "test_authorization_scope.py",
        "test_authorization_safety.py",
        "test_reader.py",
        "test_snapshot.py",
        "test_writer_behavior.py",
        "test_writer_model.py",
        "test_writer_swc.py",
        "test_writer_data_types.py",
        "test_writer_generators.py",
        "test_identifier_coverage.py",
        "test_cli.py",
        "test_cli_docs.py",
        "test_cli_query.py",
        "test_impact.py",
        "test_validator.py",
    ),
    # The address-keyed authorization gate is held by `test_editing_authorization.py`; resource recoverability,
    # identifier refusals and `resource_report` by the protocol suites below and `test_resource_report_tool.py`.
    "packages/mcp/tests": (
        "test_diff_impact_tools.py",
        "test_audit_completeness.py",
        "test_editing_protocol_decision_table.py",
        "test_resource_recoverability.py",
        "test_protocol_change_set.py",
    ),
}

#: The suites whose test NAMES are frozen, a subset of the inventory: those over the production
#: modules the protocol changed, where "adjust the test rather than the code" was available. The
#: sibling checks still run over the whole inventory.
FLOOR_SUITES: dict[str, tuple[str, ...]] = {
    "packages/tools/tests": (
        "test_reader.py",
        "test_snapshot.py",
        "test_differ.py",
        "test_differ_authorization_compat.py",
        "test_differ_coverage.py",
        "test_session.py",
        "test_writer_behavior.py",
        "test_writer_model.py",
        "test_writer_swc.py",
        "test_writer_data_types.py",
        "test_writer_generators.py",
        "test_identifier_coverage.py",
        "test_validator.py",
    ),
}

#: Every test function each frozen module declared before the editing protocol: the floor it is
#: held to, generated once and frozen.
EXPECTATION_FLOOR = "tests/fixtures/expectation_floor.txt"


def _modules(suites: dict[str, tuple[str, ...]]) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (f"{directory}/{name}", REPO_ROOT / directory / name) for directory, names in suites.items() for name in names
    )


LEGACY_MODULES: tuple[tuple[str, Path], ...] = _modules(LEGACY_SUITES)
FLOOR_MODULES: tuple[tuple[str, Path], ...] = _modules(FLOOR_SUITES)


class TestTheExistingSuiteWasNotWeakened:
    """The frozen legacy inventory and its name floor are well-formed.

    A closed half holds the legacy suites to them: no expectation of the floor dropped, none
    turned into an expected failure, no new skip.
    """

    def test_every_named_module_exists(self) -> None:
        missing = [name for name, path in LEGACY_MODULES if not path.is_file()]
        assert not missing, f"the frozen inventory names modules that are gone: {missing}"

    def test_the_name_floor_is_a_subset_of_the_legacy_inventory(self) -> None:
        # The floor may never name a module the inventory does not (its skips would go unchecked);
        # the fixture is held to the same set.
        assert {name for name, _ in FLOOR_MODULES} <= {name for name, _ in LEGACY_MODULES}
        assert set(_expectation_floor()) == {name for name, _ in FLOOR_MODULES}


def _expectation_floor() -> dict[str, set[str]]:
    """The frozen inventory, parsed from its committed text file."""
    floor: dict[str, set[str]] = {}
    current = ""
    for line in (REPO_ROOT / "packages" / "tools" / EXPECTATION_FLOOR).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith("\t"):
            current = line.strip()
            floor[current] = set()
        else:
            floor[current].add(line.strip())
    return floor


# A two-key payload or address names the member whose absent selector reads as `null`


class TestATwoKeyIdentityNamesTheMember:
    """Written with two keys, read and emitted with three."""

    def test_a_member_create_with_a_two_key_payload_commits_and_is_emitted_with_three(self, project: Path) -> None:
        outcome = _dispatch(
            _change_set(
                project,
                _create_in(
                    "tasks", "Task_10ms", [{"field": "runnables"}, {"member": dict(_SECOND_PAIR)}], dict(_SECOND_PAIR)
                ),
            ),
            project,
        )
        assert outcome.committed, outcome.describe()
        (entry,) = outcome.response["delta"]
        assert entry["address"]["path"] == [{"field": "runnables"}, {"member": _three_keys(_SECOND_PAIR)}]

    def test_a_member_update_through_a_two_key_address_commits(self, project: Path) -> None:
        outcome = _dispatch(
            _change_set(
                project,
                _update(
                    "sequences",
                    "task10ms_consumer_cycle",
                    [{"field": "steps"}, {"member": dict(_MAPPED_PAIR)}, {"field": "note"}],
                    "the consumer reads",
                ),
            ),
            project,
        )
        assert outcome.committed, outcome.describe()
        (entry,) = outcome.response["delta"]
        assert entry["address"]["path"][1] == {"member": _three_keys(_MAPPED_PAIR)}

    def test_a_whole_collection_reorder_written_with_two_keys_commits(self, project: Path) -> None:
        outcome = _dispatch(
            _change_set(
                project,
                _create_in(
                    "tasks", "Task_10ms", [{"field": "runnables"}, {"member": dict(_SECOND_PAIR)}], dict(_SECOND_PAIR)
                ),
                _update("tasks", "Task_10ms", [{"field": "runnables"}], [dict(_SECOND_PAIR), dict(_MAPPED_PAIR)]),
            ),
            project,
        )
        assert outcome.committed, outcome.describe()
        written = yaml.safe_load((project / "model" / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8"))
        assert [entry["runnable"] for entry in written["runnables"]] == ["ExampleRun", "ExampleConsumerRun"]

    def test_a_member_payload_without_the_runnable_is_refused(self, project: Path) -> None:
        outcome = _dispatch(
            _change_set(
                project,
                _create_in(
                    "tasks",
                    "Task_10ms",
                    [{"field": "runnables"}, {"member": {"swc": "ExampleSWC"}}],
                    {"swc": "ExampleSWC"},
                ),
            ),
            project,
        )
        assert outcome.raised is None, outcome.describe()
        assert not outcome.committed
        assert outcome.error == "schema-violation", outcome.describe()

    @pytest.mark.parametrize(
        ("removed", "created"),
        [
            pytest.param(dict(_MAPPED_PAIR), _three_keys(_MAPPED_PAIR), id="remove-two-keys-create-null"),
            pytest.param(_three_keys(_MAPPED_PAIR), dict(_MAPPED_PAIR), id="remove-null-create-two-keys"),
        ],
    )
    def test_the_replacement_ban_sees_one_identity_whichever_way_it_is_spelled(
        self, project: Path, removed: dict[str, Any], created: dict[str, Any]
    ) -> None:
        outcome = _dispatch(
            _change_set(
                project,
                _remove_in("tasks", "Task_10ms", [{"field": "runnables"}, {"member": removed}]),
                _create_in("tasks", "Task_10ms", [{"field": "runnables"}, {"member": created}], dict(_MAPPED_PAIR)),
            ),
            project,
        )
        assert outcome.raised is None, outcome.describe()
        assert outcome.error == "identity-conflict", outcome.describe()

    def test_a_two_key_scope_covers_the_three_key_delta(self, project: Path) -> None:
        scope = [
            {
                "operation": "create",
                "address": {
                    "layer": "tasks",
                    "element": "Task_10ms",
                    "path": [{"field": "runnables"}, {"member": dict(_SECOND_PAIR)}],
                },
            }
        ]
        outcome = _dispatch(
            _change_set(
                project,
                _create_in(
                    "tasks",
                    "Task_10ms",
                    [{"field": "runnables"}, {"member": _three_keys(_SECOND_PAIR)}],
                    _three_keys(_SECOND_PAIR),
                ),
            ),
            project,
            scope=scope,
        )
        assert outcome.committed, outcome.describe()

    @pytest.mark.parametrize(
        ("scoped", "changed"),
        [
            pytest.param(dict(_SECOND_PAIR), dict(_INSTANCE_PAIR), id="swc-scope-instance-change"),
            pytest.param(dict(_INSTANCE_PAIR), dict(_SECOND_PAIR), id="instance-scope-swc-change"),
        ],
    )
    def test_a_scope_on_one_selector_does_not_cover_a_change_on_the_other(
        self, project: Path, scoped: dict[str, Any], changed: dict[str, Any]
    ) -> None:
        """An absent selector is filled with ``null``, never resolved: ``swc`` and ``instance`` are two identities."""
        scope = [
            {
                "operation": "create",
                "address": {
                    "layer": "tasks",
                    "element": "Task_10ms",
                    "path": [{"field": "runnables"}, {"member": scoped}],
                },
            }
        ]
        outcome = _dispatch(
            _change_set(
                project, _create_in("tasks", "Task_10ms", [{"field": "runnables"}, {"member": changed}], dict(changed))
            ),
            project,
            scope=scope,
        )
        assert outcome.raised is None, outcome.describe()
        assert not outcome.committed
        assert outcome.error == "unauthorized-delta", outcome.describe()


#: The experiment gate's project directory: a name no report holds by accident.
EXPERIMENT_PROBE_DIRNAME = "no-experiment-report-may-name-this-directory"


class TestNoExperimentReportCarriesAStoragePath:
    """No ``swcraft experiment`` report file names where the project is stored.

    Allocation writes ``report.md`` and ``result.json``, trace adds ``proposals.json``. ``result.json``'s
    only paths are relative to the project directory (the configuration file, the report directory);
    ``report.md``'s links are relative to the report directory. Neither the project's absolute path nor
    a recorded ``tempfile.mkdtemp`` scratch copy may appear, natively or POSIX-spelled. The mixed-task
    fixture carries the validator's messages into the report.

    Does not reach a location spelled otherwise (a relative path climbing out, a ``~`` home) or the
    two lines the command prints, which name the written files by full path as ``generate`` does.
    """

    @pytest.mark.parametrize("fixture", ["three_cores", "mixed_task"])
    def test_neither_file_names_the_project_or_a_scratch_copy(
        self, tmp_path: Path, monkeypatch, capsys, fixture: str
    ) -> None:
        import carma_models
        from swcraft_tools import allocation
        from swcraft_tools.cli import main

        project_dir = tmp_path / EXPERIMENT_PROBE_DIRNAME
        manifest, file = carma_models.write_project(getattr(carma_models, fixture)(), project_dir, max_moves=2)
        scratch: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            scratch.append(made)
            return made

        monkeypatch.setattr(allocation.tempfile, "mkdtemp", recording)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "experiment", "allocation", "--project", str(manifest), "--config", str(file)]
        )
        main()
        capsys.readouterr()

        directory = project_dir / "dist" / "experiments" / "relieve"
        texts = {name: (directory / name).read_text(encoding="utf-8") for name in ("report.md", "result.json")}
        assert scratch, "no candidate was checked on a scratch copy, so the gate proves nothing about one"
        document = json.loads(texts["result.json"])
        if fixture == "mixed_task":
            assert document["dropped"], "the dropped set's messages are what this case is for"
        # The record's two paths are relative to the project directory, never a location.
        assert document["fingerprint"]["configuration"]["file"] == "experiments/relieve.yaml"
        assert document["fingerprint"]["parameters"]["output"] == "dist/experiments/relieve"
        assert "- Configuration: `experiments/relieve.yaml`, sha256:" in texts["report.md"]
        locations = {str(tmp_path), tmp_path.as_posix(), EXPERIMENT_PROBE_DIRNAME, tempfile.gettempdir()}
        locations |= {spelled for made in scratch for spelled in (made, Path(made).as_posix(), Path(made).name)}
        for name, text in texts.items():
            leaked = sorted(location for location in locations if location in text)
            assert leaked == [], f"{name} names a storage location: {leaked}"
        # result.json carries no link and no path to the model; report.md's link targets are relative only.
        link = re.compile(r"\[([^\]\[]+)\]\(([^)\s]+)\)")
        assert link.search(texts["result.json"]) is None
        assert "model/" not in texts["result.json"]
        assert texts["result.json"].count(".yaml") == 1, "a .yaml beside the configuration file's own name"
        targets = [target for _name, target in link.findall(texts["report.md"])]
        assert targets, "report.md links no name, so the link half of the gate proves nothing"
        model_root = (project_dir / "model").resolve()
        for target in targets:
            assert not is_absolute_anywhere(target) and "\\" not in target, target
            resolved = (directory / urllib.parse.unquote(target)).resolve()
            assert resolved.is_file() and resolved.is_relative_to(model_root), target

    def test_the_trace_kinds_three_files_name_no_location(self, tmp_path: Path) -> None:
        """``swcraft experiment trace``'s three files name no location, failing model's validator messages included."""
        import test_trace_search_run as trace_run
        import trace_models
        from swcraft_tools.project import require_project
        from swcraft_tools.trace_search import run_trace

        project_dir = tmp_path / EXPERIMENT_PROBE_DIRNAME
        manifest, file = trace_models.write_project(trace_run._model(), project_dir, candidates=3)
        run = run_trace(require_project(str(manifest)), file)
        directory = run.report_path.parent
        texts = {
            name: (directory / name).read_text(encoding="utf-8")
            for name in ("report.md", "result.json", "proposals.json")
        }
        document = json.loads(texts["result.json"])
        assert document["findings"]["trace"], "no validator message reached the report, so that half proves nothing"
        assert document["fingerprint"]["configuration"]["file"] == "experiments/trace.yaml"
        assert document["fingerprint"]["parameters"]["output"] == "dist/experiments/trace"
        locations = {str(tmp_path), tmp_path.as_posix(), EXPERIMENT_PROBE_DIRNAME, tempfile.gettempdir()}
        for name, text in texts.items():
            leaked = sorted(location for location in locations if location in text)
            assert leaked == [], f"{name} names a storage location: {leaked}"
        link = re.compile(r"\[([^\]\[]+)\]\(([^)\s]+)\)")
        for name in ("result.json", "proposals.json"):
            assert link.search(texts[name]) is None and "model/" not in texts[name], name
            assert texts[name].count(".yaml") == 1, f"{name}: a .yaml beside the configuration file's own name"
        targets = [target for _name, target in link.findall(texts["report.md"])]
        assert targets, "report.md links no name, so the link half of the gate proves nothing"
        model_root = (project_dir / "model").resolve()
        for target in targets:
            assert not is_absolute_anywhere(target) and "\\" not in target, target
            resolved = (directory / urllib.parse.unquote(target)).resolve()
            assert resolved.is_file() and resolved.is_relative_to(model_root), target
