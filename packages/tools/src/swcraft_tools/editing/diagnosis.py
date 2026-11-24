"""Pass D's *code* and *where*: the protocol's answer to an invalid staged result.

A translation of the validator's findings, not a second implementation: the code
comes from :data:`RULE_CODES` keyed by rule id and the addresses from the
subjects; a message is never parsed. A rule :data:`RULE_CODES` does not name
raises :class:`UndiagnosedInconsistency`.

The three codes: ``broken-reference`` (a reference that does not resolve,
addressed at the referrers); ``invalid-identifier`` (an emitted ``SHORT-NAME``
that is not a legal AUTOSAR ``Identifier``); ``consistency-violation`` (an
inter-element rule over the assembled result fails).

:data:`RULE_CODES` names every error rule of :data:`~swcraft_tools.validator.CONSISTENCY_RULES`
and no warning rule, which pass D never sees. The identifier rule is scoped by the writer's own
``RESERVED_DATA_TYPE_RULES`` and ``RESERVED_DATA_TYPE_FORM_RULES``, so the engine never refuses a
name ``check-model`` accepts (a value type emits no ``{name}_CompuMethod``). Do not restore the alias arm:
the emitted forms are ``RESERVED_DATA_TYPE_RULES``' to say, never a kind description's. Nothing is waived:
a change set is accepted only when the whole staged result is valid, so it may repair a pre-existing violation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple, get_args

from pydantic import BaseModel

from swcraft_tools import diagnostics
from swcraft_tools.diagnostics import Subject
from swcraft_tools.editing.address import Address, FieldStep, KeyStep, MemberStep, PathStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.protocol import Violation
from swcraft_tools.editing.resolve import Role, append_sentence
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, check_identifier

# The kernel, for which layers are ARXML-bound and which element kind a
# `schema.py` class realizes: the published `LayerDescriptor` carries no
# `arxmlBound`, and adding one would change what `discover` publishes.
from swcraft_tools.model_registry import (
    LayerFacts,
    ModelRegistry,
    ModelRegistryError,
    current_registry,
    production_registry,
)

# `validator` for its structured findings and the writer algorithms it restates,
# never for its prose. `unique_names` is reused because it is pinned against the
# writer's event-name counter; a third copy would drift.
from swcraft_tools.validator import (
    EVENT_KIND_WORDS,
    GENERATED_BUDGET_SUFFIX,
    GENERATED_DATA_EVENT_PREFIX,
    GENERATED_INIT_EVENT_PREFIX,
    GENERATED_INTERNAL_BEHAVIOR_SUFFIX,
    GENERATED_OPERATION_EVENT_PREFIX,
    GENERATED_RETURNS_EVENT_PREFIX,
    GENERATED_TIMING_EVENT_PREFIX,
    RESERVED_IMPLEMENTATION_SUFFIX,
    STUB_MEMBER_LISTS,
    ConsistencyIssue,
    ConsistencyResult,
    derived_result_point_name,
)
from swcraft_tools.validator.events import unique_names

# The data-type forms come from the writer's own tables, the only place that
# carries their kind scope. `writer/` imports nothing from `editing/`, so the
# dependency still points one way.
from swcraft_tools.writer.arxml_generation import (
    ACCESS_LIST_PREFIXES,
    INIT_CONSTANT_ADDED_LENGTH,
    RESERVED_DATA_TYPE_FORM_RULES,
    RESERVED_DATA_TYPE_RULES,
    VARIABLE_ACCESS_LIST_PREFIXES,
    access_point_added_length,
    access_point_name,
    init_constant_name,
)

__all__ = [
    "BROKEN_REFERENCE",
    "CONSISTENCY_VIOLATION",
    "INVALID_IDENTIFIER",
    "RULE_CODES",
    "UndiagnosedInconsistency",
    "diagnose",
    "emitted_identifier_violations",
    "subject_address",
]

#: The three codes this module produces; ``schema-violation`` has no rule here.
BROKEN_REFERENCE = "broken-reference"
INVALID_IDENTIFIER = "invalid-identifier"
CONSISTENCY_VIOLATION = "consistency-violation"


class _Mapping(NamedTuple):
    """One validator rule's protocol answer: which code, and what it groups with.

    Findings of one ``group`` naming the same ``target`` become one violation
    carrying every referrer's address, because one repair fixes them all. A blank
    group is filled with the rule's own id (:func:`_own_groups`), so unrelated rules
    never merge by accident.
    """

    code: str
    group: str = ""


#: Every error rule of :data:`~swcraft_tools.validator.CONSISTENCY_RULES`, mapped
#: onto the protocol code that answers it; a missing rule is a failing test, not a
#: run-time :class:`UndiagnosedInconsistency`. The declaration order is the report
#: order, so the first rule reached is the primary: the identifier rules, then the
#: reference rules, then the rest.
RULE_CODES: dict[str, _Mapping] = {
    # --- invalid-identifier ------------------------------------------------
    "identifier.declared": _Mapping(INVALID_IDENTIFIER),
    "identifier.generated": _Mapping(INVALID_IDENTIFIER),
    # --- broken-reference --------------------------------------------------
    "port.undeclared-interface": _Mapping(BROKEN_REFERENCE, "interface"),
    # The same group as the component port's: declaring the interface fixes
    # every referrer of it at once.
    "composition.port-undeclared-interface": _Mapping(BROKEN_REFERENCE, "interface"),
    # A task names a partition; declaring it is a repair of its own.
    "hardware.undeclared-partition": _Mapping(BROKEN_REFERENCE, "partition"),
    # A partition's `core` and a memory's `accessible` both name a core, so one
    # repair (declaring the core) fixes every referrer.
    "hardware.partition-undeclared-core": _Mapping(BROKEN_REFERENCE, "core"),
    "hardware.accessible-undeclared-core": _Mapping(BROKEN_REFERENCE, "core"),
    # A `satisfies` member naming no declared requirement; every site naming the
    # id groups under `requirement`.
    "trace.link-unknown-requirement": _Mapping(BROKEN_REFERENCE, "requirement"),
    "connector.endpoint-unknown-swc": _Mapping(BROKEN_REFERENCE, "connector-swc"),
    # An endpoint names a component or a composition: separate rules and groups
    # because the repairs differ; `connector-port` groups both kinds' port failures.
    "connector.endpoint-unknown-composition": _Mapping(BROKEN_REFERENCE, "connector-composition"),
    # A connector's own undeclared composition: the same repair, so the same group.
    "connector.unknown-composition": _Mapping(BROKEN_REFERENCE, "connector-composition"),
    "connector.endpoint-unknown-port": _Mapping(BROKEN_REFERENCE, "connector-port"),
    # Four groups: a missing component, composition, member and port are four
    # different repairs.
    "composition.member-unknown-type": _Mapping(BROKEN_REFERENCE, "composition-member-type"),
    "composition.member-unknown-composition": _Mapping(BROKEN_REFERENCE, "composition-member-composition"),
    "composition.delegation-unknown-member": _Mapping(BROKEN_REFERENCE, "composition-delegation-member"),
    "composition.delegation-unknown-port": _Mapping(BROKEN_REFERENCE, "composition-delegation-port"),
    "interface.data-element-undeclared-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    "interface.argument-undeclared-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    # Declaring the type is the repair, whichever referrer names it.
    "swc.variable.undeclared-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    # A value type's and a reference type's `base`: the same repair, the same group.
    "data-type.value-type-undeclared-base": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.reference-type-undeclared-base": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.record-field-undeclared-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.array-undeclared-element-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.implementation-element-undeclared-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.implementation-array-undeclared-element-type": _Mapping(BROKEN_REFERENCE, "data-type"),
    "data-type.pointer-undeclared-target": _Mapping(BROKEN_REFERENCE, "data-type"),
    # A mapping set's two sides, also when the name resolves to a type of the
    # wrong kind: the second repair is to repoint the referrer.
    "mapping-set.unknown-application": _Mapping(BROKEN_REFERENCE, "data-type"),
    "mapping-set.unknown-implementation": _Mapping(BROKEN_REFERENCE, "data-type"),
    "task.mapping-unknown-swc": _Mapping(BROKEN_REFERENCE, "task-mapping-swc"),
    "task.mapping-unknown-runnable": _Mapping(BROKEN_REFERENCE, "task-mapping-runnable"),
    "sequence.unknown-task": _Mapping(BROKEN_REFERENCE, "sequence-task"),
    "sequence.step-not-mapped": _Mapping(BROKEN_REFERENCE, "sequence-step-mapping"),
    "sequence.step-unknown-swc": _Mapping(BROKEN_REFERENCE, "sequence-step-swc"),
    "sequence.step-unknown-runnable": _Mapping(BROKEN_REFERENCE, "sequence-step-runnable"),
    "hardware.undeclared-memory": _Mapping(BROKEN_REFERENCE, "memory"),
    # Three groups: a missing port, element and operation are three repairs.
    "swc.access.unknown-port": _Mapping(BROKEN_REFERENCE, "access-port"),
    "swc.access.unknown-element": _Mapping(BROKEN_REFERENCE, "access-element"),
    "swc.access.unknown-operation": _Mapping(BROKEN_REFERENCE, "access-operation"),
    # A variable access naming no variable of its own component (`[constr_2026]`).
    "swc.variable-access.unknown-variable": _Mapping(BROKEN_REFERENCE, "access-variable"),
    # A `can_enter` or `runs_inside` entry naming no exclusive area of its component.
    "swc.exclusive-area.unknown-area": _Mapping(BROKEN_REFERENCE, "access-area"),
    # Event reference rules group apart from access findings: an event is not an
    # access entry.
    "swc.operation-event.unknown-port": _Mapping(BROKEN_REFERENCE, "operation-event-port"),
    "swc.operation-event.unknown-operation": _Mapping(BROKEN_REFERENCE, "operation-event-operation"),
    "swc.data-event.unknown-port": _Mapping(BROKEN_REFERENCE, "data-event-port"),
    "swc.data-event.unknown-element": _Mapping(BROKEN_REFERENCE, "data-event-element"),
    # No repair pool suggests a call: asynchronous calls are nested, filtered,
    # pair-identified entries no `REFERENCE_POOLS` row expresses.
    "swc.call-returned-event.unknown-call": _Mapping(BROKEN_REFERENCE, "call-returned-event-call"),
    # A mapping set the model does not declare; a whole-value list member, so
    # nothing resolved it earlier.
    "swc.unknown-mapping-set": _Mapping(BROKEN_REFERENCE, "mapping-set"),
    # A `derived_from` member naming no requirement and no element; not
    # `requirement`'s group, since the target may be an element.
    "trace.derived-from-unknown-target": _Mapping(BROKEN_REFERENCE),
    # --- consistency-violation ---------------------------------------------
    # Every element and reference resolves; what fails is an inter-element rule:
    # uniqueness over emitted names, compatibility over resolved endpoints, budget
    # or ordering arithmetic. A warning rule has no code: pass D never sees one.
    "connector.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "connector.duplicate-link": _Mapping(CONSISTENCY_VIOLATION),
    "connector.from-not-provide": _Mapping(CONSISTENCY_VIOLATION),
    "connector.to-not-require": _Mapping(CONSISTENCY_VIOLATION),
    "connector.interface-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "connector.pointer-between-application-components": _Mapping(CONSISTENCY_VIOLATION),
    # Rules over the assembled hierarchy: one root, unique names and placements,
    # acyclicity, direction agreement. A type-form error is refused at load as a
    # `schema-violation` and never reaches pass D.
    "composition.no-root": _Mapping(CONSISTENCY_VIOLATION),
    "composition.multiple-roots": _Mapping(CONSISTENCY_VIOLATION),
    "composition.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "composition.duplicate-port": _Mapping(CONSISTENCY_VIOLATION),
    "composition.duplicate-member": _Mapping(CONSISTENCY_VIOLATION),
    "composition.duplicate-delegation": _Mapping(CONSISTENCY_VIOLATION),
    "composition.duplicate-delegation-link": _Mapping(CONSISTENCY_VIOLATION),
    "composition.cycle": _Mapping(CONSISTENCY_VIOLATION),
    "composition.member-type-repeated": _Mapping(CONSISTENCY_VIOLATION),
    "composition.delegation-port-direction-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "composition.delegation-interface-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "composition.connector-endpoint-not-a-member": _Mapping(CONSISTENCY_VIOLATION),
    "composition.delegation-connector-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    "swc.external-with-ports": _Mapping(CONSISTENCY_VIOLATION),
    # Not `broken-reference`: `external_ref` points outside the model by
    # construction, so there is nothing in the project to declare.
    "swc.external-incomplete-reference": _Mapping(CONSISTENCY_VIOLATION),
    "swc.external-reference-without-stub": _Mapping(CONSISTENCY_VIOLATION),
    "swc.external-reference-inside-generated-package": _Mapping(CONSISTENCY_VIOLATION),
    # The two stub kinds' reference pairs, for the same reason.
    "interface.external-incomplete-reference": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.external-incomplete-reference": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.compu-method-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    # The unit, computation method and constraint an application type's name
    # generates, colliding with a declared name; the repair is a rename.
    "data-type.unit-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.compu-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.range-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    # An application type a port speaks in that no named mapping set pairs with an
    # implementation type; the repair is to add the pair or name the set.
    "data-type.application-type-unmapped": _Mapping(CONSISTENCY_VIOLATION),
    # A uniqueness rule over the `mapping_sets` layer's emitted names.
    "mapping-set.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    # Both sides resolve; what fails is a rule over the pair's resolved shapes.
    "mapping-set.incompatible-composite-map": _Mapping(CONSISTENCY_VIOLATION),
    "mapping-set.pointer-mapped": _Mapping(CONSISTENCY_VIOLATION),
    # `[constr_1004]`: the named sets pair one application type with two
    # implementation types; the repair drops a map or a set, never declares.
    "data-type.application-type-double-mapped": _Mapping(CONSISTENCY_VIOLATION),
    # No wrong-kind reference rules: each role resolves in a layer holding only
    # the kinds it admits, so a name of another layer is `broken-reference`.
    "data-type.implementation-record-duplicate-element-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.record-duplicate-field-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.enum-duplicate-literal-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.enum-duplicate-literal-value": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.cycle": _Mapping(CONSISTENCY_VIOLATION),
    # `check_init_conformance`'s rules, after the `data-type.*` run (groups are
    # contiguous by emitting check, then by prefix). It abstains wherever the
    # declared type does not resolve, so every reference has resolved by the time
    # one fires. The last two fire when the exporter has no spelling for a value;
    # the repair is an edit to the element.
    "interface.init-shape-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-unknown-enum-literal": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-record-field-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-array-length-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-out-of-range": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-string-scalar": _Mapping(CONSISTENCY_VIOLATION),
    "interface.init-unwritable-integer": _Mapping(CONSISTENCY_VIOLATION),
    # An init on a data element typed by a type stub, whose shape is outside the project.
    "interface.init-on-external-type": _Mapping(CONSISTENCY_VIOLATION),
    "task.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "task.runnable-period-not-multiple": _Mapping(CONSISTENCY_VIOLATION),
    "task.runnable-mapped-to-multiple-tasks": _Mapping(CONSISTENCY_VIOLATION),
    # A count over the tree: one placed instance run by tasks of two partitions.
    "task.instance-in-multiple-partitions": _Mapping(CONSISTENCY_VIOLATION),
    # `instance` is a path, not a declared reference, so these are not
    # `broken-reference`: what fails is the walk over the tree.
    "task.resolved-duplicate-runnable": _Mapping(CONSISTENCY_VIOLATION),
    "task.instance-ambiguous": _Mapping(CONSISTENCY_VIOLATION),
    "task.instance-unknown": _Mapping(CONSISTENCY_VIOLATION),
    "task.instance-not-a-component": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.step-order": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.resolved-duplicate-step": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.step-instance-ambiguous": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.step-instance-unknown": _Mapping(CONSISTENCY_VIOLATION),
    "sequence.step-instance-not-a-component": _Mapping(CONSISTENCY_VIOLATION),
    # The hardware topology rules whose reference resolves: what fails is the
    # *kind* the reference vocabulary cannot state, or a rule over the assembled
    # layer. Exactly the shape `connector.from-not-provide` has.
    "hardware.layer-absent": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.no-processor": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.multiple-processors": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.partition-not-a-partition": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.partition-core-not-a-core": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.memory-not-a-memory": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.accessible-not-a-core": _Mapping(CONSISTENCY_VIOLATION),
    # Every reference resolves (the task's partition and core, the SWC, the
    # memory); what fails is a design rule over them.
    "hardware.unreachable-memory": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.missing-budget": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.threshold-breach": _Mapping(CONSISTENCY_VIOLATION),
    "hardware.rate-monotonic-violation": _Mapping(CONSISTENCY_VIOLATION),
    # A task's runnables' declared costs above its own budget: arithmetic, like
    # the budget rules above.
    "task.runnable-cost-exceeds-task": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-swc": _Mapping(CONSISTENCY_VIOLATION),
    # A requirement `id` two files declare: a uniqueness rule over the layer.
    "name.duplicate-requirement": _Mapping(CONSISTENCY_VIOLATION),
    "name.swc-implementation-collision": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-port": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-runnable": _Mapping(CONSISTENCY_VIOLATION),
    # A runnable and an event share one name space.
    "name.runnable-event-collision": _Mapping(CONSISTENCY_VIOLATION),
    # Two events written with one SHORT-NAME.
    "name.duplicate-event": _Mapping(CONSISTENCY_VIOLATION),
    # Two names sharing one name space, ignoring case.
    "name.name-space-collision": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-interface": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-data-element": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-operation": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-argument": _Mapping(CONSISTENCY_VIOLATION),
    # Emitted by `check_name_uniqueness`, so they sit with its group, not with
    # the `composition.*` run.
    "name.duplicate-component-type": _Mapping(CONSISTENCY_VIOLATION),
    "name.composition-implementation-collision": _Mapping(CONSISTENCY_VIOLATION),
    # The `_Timing` twins of the two rules above.
    "name.swc-timing-collision": _Mapping(CONSISTENCY_VIOLATION),
    "name.composition-timing-collision": _Mapping(CONSISTENCY_VIOLATION),
    # Emitted by `check_name_uniqueness`, so it sits with that group. A group of
    # its own: the repair renames one of two declared names.
    "interface.init-constant-name-collision": _Mapping(CONSISTENCY_VIOLATION),
    # Emitted by `check_name_uniqueness`: the three access-point forms are
    # SHORT-NAME siblings under one runnable. A group of its own, since the repair
    # renames one of two names in two different files.
    "swc.access.name-collision": _Mapping(CONSISTENCY_VIOLATION),
    # The port and its interface resolve; what fails is the port's direction or
    # interface kind for the list the entry sits in.
    "swc.access.direction-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "swc.access.interface-kind-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    # The operation event's rules over resolved references: direction, kind, a
    # pair bound twice, or incompatible operations in one runnable.
    "swc.operation-event.direction-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "swc.operation-event.interface-kind-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "swc.operation-event.duplicate-binding": _Mapping(CONSISTENCY_VIOLATION),
    "swc.operation-event.incompatible-signatures": _Mapping(CONSISTENCY_VIOLATION),
    # The data-received event's direction and interface-kind rules.
    "swc.data-event.direction-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "swc.data-event.interface-kind-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    # The implicit read's rules, the asynchronous call's synchrony, timeout and
    # result-count rules, the ambiguous call-returned event and `[constr_1259]`.
    "swc.access.queued-implicit-read": _Mapping(CONSISTENCY_VIOLATION),
    "swc.access.implicit-read-enable-update": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call.mixed-synchrony": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call.timeout-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call.async-without-result": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call.several-results": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call-returned-event.ambiguous-call": _Mapping(CONSISTENCY_VIOLATION),
    "swc.call.init-runnable-result": _Mapping(CONSISTENCY_VIOLATION),
    # Every name resolves; what fails is one declared level below another. A
    # group of its own: the repair is this component's alone.
    "safety.component-below-requirements": _Mapping(CONSISTENCY_VIOLATION),
    # The safety rules each have a group of their own: every name resolves and
    # each repair belongs to one component or task. The `measure-*` rules judge
    # declarations that all resolved, in a file that loads.
    "safety.provider-below-element": _Mapping(CONSISTENCY_VIOLATION),
    "safety.consumer-above-element": _Mapping(CONSISTENCY_VIOLATION),
    "safety.measure-unknown-element": _Mapping(CONSISTENCY_VIOLATION),
    "safety.measure-on-provide-port": _Mapping(CONSISTENCY_VIOLATION),
    "safety.task-mixed-asil": _Mapping(CONSISTENCY_VIOLATION),
    # A blank measure text, beside its siblings: an update of the port's
    # `measures` repairs it, unlike `model.unknown-key`.
    "safety.measure-blank": _Mapping(CONSISTENCY_VIOLATION),
    # Placement rules: a level compared with a level over resolved names, each
    # repair one partition's, component's or hardware element's alone.
    "safety.partition-on-unqualified-core": _Mapping(CONSISTENCY_VIOLATION),
    "safety.memory-below-component": _Mapping(CONSISTENCY_VIOLATION),
    # Per-type rules; above and below are two repairs (no argument closes the
    # first), so they are not merged.
    "safety.component-above-partition": _Mapping(CONSISTENCY_VIOLATION),
    "safety.component-below-partition": _Mapping(CONSISTENCY_VIOLATION),
    "safety.partition-memory-below-level": _Mapping(CONSISTENCY_VIOLATION),
    # Trust rules: below and unrated are two repairs (raise the level, declare one).
    "safety.trusted-partition-below-processor-level": _Mapping(CONSISTENCY_VIOLATION),
    "safety.trusted-partition-unrated": _Mapping(CONSISTENCY_VIOLATION),
    # Two file names differing only in case. The protocol cannot rename a file,
    # so on a case-sensitive host every change set is refused until one is
    # renamed by hand.
    "storage.filename-case-collision": _Mapping(CONSISTENCY_VIOLATION),
    # A stored file name Windows reserves for a device. The protocol never writes
    # one (`segment_refusal`), so the repair is a rename by hand.
    "storage.reserved-device-name": _Mapping(CONSISTENCY_VIOLATION),
    # Nothing it names is undeclared; a value the element must carry is missing.
    "data-type.base-type-native-declaration-missing": _Mapping(CONSISTENCY_VIOLATION),
    # One violation per offending component, so one change set can repair every
    # one of them at once.
    "swc.undeclared-termination-and-restart": _Mapping(CONSISTENCY_VIOLATION),
    # The com-spec and disabled-mode rules judge whether a value has a place, like
    # `safety.measure-unknown-element`; the rest are rules over resolved declarations.
    "swc.port.com-spec-unknown-element": _Mapping(CONSISTENCY_VIOLATION),
    "swc.port.com-spec-inapplicable": _Mapping(CONSISTENCY_VIOLATION),
    # An attribute a stub port's com-spec can never carry.
    "swc.port.com-spec-on-stub": _Mapping(CONSISTENCY_VIOLATION),
    "swc.port.queued-receiver-without-queue-length": _Mapping(CONSISTENCY_VIOLATION),
    "swc.timing_event.disabled-mode-port": _Mapping(CONSISTENCY_VIOLATION),
    "swc.access.queued-read-by-value": _Mapping(CONSISTENCY_VIOLATION),
    "composition.port-service-interface": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.conversion-without-unit": _Mapping(CONSISTENCY_VIOLATION),
    # A symbol that is not a C identifier, or two runnables written with one symbol.
    "swc.runnable.symbol-not-c-identifier": _Mapping(CONSISTENCY_VIOLATION),
    "name.duplicate-runnable-symbol": _Mapping(CONSISTENCY_VIOLATION),
    # A character no XML 1.0 document can carry: inconsistent with every artifact
    # the model would be written into.
    "text.xml-unrepresentable-character": _Mapping(CONSISTENCY_VIOLATION),
    # The data description elements: name uniqueness within each layer, role
    # rules over resolved references, and unknown-target rules whose `target`
    # still merges every referrer of one missing element.
    "unit.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "compu-method.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-constr.duplicate-name": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.unknown-unit": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.unknown-compu-method": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.unknown-data-constr": _Mapping(CONSISTENCY_VIOLATION),
    "compu-method.unknown-unit": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.compu-method-wrong-category": _Mapping(CONSISTENCY_VIOLATION),
    "data-type.compu-method-on-unresolved-reference-type": _Mapping(CONSISTENCY_VIOLATION),
    "compu-method.table-entry-repeated": _Mapping(CONSISTENCY_VIOLATION),
    # A derivation cycle, or a bracket letter that is not the parent's level
    # before decomposition. The trace warning rules have no code.
    "trace.derived-from-cycle": _Mapping(CONSISTENCY_VIOLATION),
    "trace.decomposition-context-mismatch": _Mapping(CONSISTENCY_VIOLATION),
    # One `satisfies` list naming one requirement twice; removing the extra
    # member repairs it.
    "trace.duplicate-link": _Mapping(CONSISTENCY_VIOLATION),
    # A variable typed by a pointer (`[constr_1296]`); `swc.variable.read-without-init`
    # is a warning and has no code.
    "swc.variable.pointer-type": _Mapping(CONSISTENCY_VIOLATION),
    # A file key no field declares, which no address can name: repaired by hand.
    # Last on purpose: the first violation is the primary, and a stray key in an
    # untouched file must never stand in front of the client's own error.
    "model.unknown-key": _Mapping(CONSISTENCY_VIOLATION),
}


def _own_groups(table: dict[str, _Mapping]) -> dict[str, _Mapping]:
    """*table* with every blank ``group`` filled in by the rule's own id.

    A shared blank group would merge unrelated rules that name the same target.
    """
    return {rule: mapping if mapping.group else _Mapping(mapping.code, rule) for rule, mapping in table.items()}


RULE_CODES = _own_groups(RULE_CODES)

#: One sentence per code, appended to the validator's wording to say what kind of
#: failure it is. Prose, not contract.
_CODE_EXPLANATIONS: dict[str, str] = {
    BROKEN_REFERENCE: (
        "The addresses named here are the referrers, not the target that is missing: the staged result is "
        "validated as a whole, so repoint or remove every referrer — or supply the target — in the same change set."
    ),
    # Worded to be true of an external stub, which is emitted as no SHORT-NAME.
    INVALID_IDENTIFIER: (
        "The AUTOSAR Identifier rule is checked on every SHORT-NAME the exporter emits, in the form it is "
        "emitted in rather than only as declared; an external stub is emitted as no SHORT-NAME, and its own "
        "name is held to the same rule."
    ),
    CONSISTENCY_VIOLATION: (
        "Every element and every reference here is individually admissible; what the assembled staged result "
        "breaks is a deterministic inter-element rule of the model."
    ),
}


class UndiagnosedInconsistency(RuntimeError):
    """The validator and the engine disagree about the staged result.

    An engine defect, raised rather than answered: a protocol code would tell a
    client to fix a correct request. Raised when the validator refuses with a rule
    :data:`RULE_CODES` does not name, or with no record at all. The message carries
    the validator's issues as a diagnostic, never contract.
    """


def diagnose(
    consistency: ConsistencyResult, suggest: Callable[[str, str, tuple[Role, ...]], str] | None = None
) -> tuple[Violation, ...]:
    """The violations one :class:`~swcraft_tools.validator.ConsistencyResult` translates into.

    Args:
        consistency: the validator's answer over the materialized staged result.
            Only its error ``records`` are read, for ``rule``, ``subjects`` and
            ``target``; a ``message`` is carried, never branched on.
        suggest: ``(group, target, roles) -> sentence`` naming the nearest names for
            a ``broken-reference`` target, *roles* the ``(rule, field, element)`` of
            each finding. ``None`` appends nothing.

    Returns:
        The violations in :data:`RULE_CODES` order, one per (group, target) and one
        per ungrouped finding. Empty when no record is a rule this table names,
        which ``pass_d`` turns into an :class:`UndiagnosedInconsistency`.
    """
    order = {rule: position for position, rule in enumerate(RULE_CODES)}
    buckets: dict[Any, list[ConsistencyIssue]] = {}
    for position, record in enumerate(consistency.records):
        mapping = RULE_CODES.get(record.rule)
        if mapping is None:
            continue  # a disagreement; `pass_d` raises rather than guessing a code
        # A target is what makes two findings one repair. Without one, each
        # finding stands alone — hence the position, which is unique per record.
        key = (mapping.group, record.target) if record.target is not None else (record.rule, position)
        buckets.setdefault(key, []).append(record)

    ordered = sorted(buckets.values(), key=lambda records: order[records[0].rule])
    return tuple(_violation(records, suggest) for records in ordered)


def _role(record: ConsistencyIssue) -> Role:
    """*record*'s rule, its first subject's last field step (naming the target), and that subject's element."""
    path = record.subjects[0].path if record.subjects else ()
    fields = [step.value for step in path if step.kind == diagnostics.FIELD]
    return (record.rule, str(fields[-1]) if fields else None, record.subjects[0].element if record.subjects else None)


def _violation(
    records: Sequence[ConsistencyIssue],
    suggest: Callable[[str, str, tuple[Role, ...]], str] | None = None,
) -> Violation:
    """One violation from the findings that share a repair."""
    mapping = RULE_CODES[records[0].rule]
    nearest = ""
    if suggest is not None and mapping.code == BROKEN_REFERENCE and records[0].target is not None:
        # A rule with no group of its own is its own pool key (`resolve.REFERENCE_POOLS`).
        # The roles narrow the pool where one group spans namespaces (`resolve.ROLE_LAYERS`).
        roles = tuple(dict.fromkeys(_role(record) for record in records))
        nearest = suggest(mapping.group or records[0].rule, str(records[0].target), roles)
    return Violation(
        error=mapping.code,
        message=_violation_message(records, mapping.code, nearest),
        addresses=_addresses(records),
    )


def _violation_message(records: Sequence[ConsistencyIssue], code: str, nearest: str = "") -> str:
    """The rejection's prose: the validator's own wording, plus what the code means.

    The only function here that touches a message, and it only concatenates: no
    code or address is ever decided by looking at text.
    """
    message = f"{' '.join(record.message for record in records)} {_CODE_EXPLANATIONS[code]}"
    return append_sentence(message, nearest)


def _addresses(records: Sequence[ConsistencyIssue]) -> tuple[Address, ...]:
    """Every subject of *records* as a protocol address, in order, without repeats."""
    unique: dict[Address, None] = {}
    for record in records:
        for subject in record.subjects:
            unique.setdefault(subject_address(subject), None)
    return tuple(unique)


#: A subject step's kind -> the address step that expresses it. The two
#: vocabularies are the same three, so this is a rename.
_STEPS: dict[str, Any] = {
    diagnostics.FIELD: FieldStep,
    diagnostics.MEMBER: MemberStep,
    diagnostics.KEY: KeyStep,
}


def subject_address(subject: Subject) -> Address:
    """One model subject as the protocol address that names it.

    Its JSON equals ``diagnostics.Subject.to_json``, the spelling
    ``swcraft check-model --format json`` prints.
    """
    path: tuple[PathStep, ...] = tuple(_STEPS[step.kind](step.value) for step in subject.path)
    return Address(layer=subject.layer, element=subject.element, path=path)


# ---------------------------------------------------------------------------
# The identifier rule, over the *in-memory* staged tree
#
# `write_element` refuses an illegal name during materialization, before pass D,
# when there is no materialized tree to validate, so the engine asks this module
# about the in-memory staged tree. `check_identifier_conformance` is the
# authority whenever a tree exists. The layers the rule reaches are the
# contract's `layerDecl.arxmlBound`, the same fact the validator reads.
# ---------------------------------------------------------------------------


def _identifier_layers(registry: ModelRegistry | None = None) -> tuple[LayerFacts, ...]:
    """The ARXML-bound layers, in contract order — the whole scope of this rule.

    A layer declaring no ``arxmlBound`` is not diagnosed. *registry* ``None`` is
    the one in effect.
    """
    model_registry = current_registry() if registry is None else registry
    return tuple(layer for layer in model_registry.layers if layer.arxml_bound)


def emitted_identifier_violations(
    staged: StagedModel, *, registry: ModelRegistry | None = None
) -> tuple[Violation, ...]:
    """Every emitted ``SHORT-NAME`` of the staged result that is not a legal ``Identifier``.

    The rule applies to the emitted name: the declared name and the generated forms
    that lengthen it (``_CompuMethod``, ``_Implementation``, ``_InternalBehavior``,
    ``_Budget``, the event prefixes, ``{Interface}_{Element}_Init``, the access-point
    and result-point forms, ``_Unit``, ``_Compu``, ``_Range``). A generated form is
    checked only when the names it derives from conform, so one bad name is
    reported once. The pre-materialization path only; a bound layer with no
    :data:`_MEMBERS_AND_GENERATED_FORMS` entry is diagnosed by its declared
    identity alone.

    Returns:
        One violation per offending emitted name, in model order, addressed at the
        element or at the member's ``name`` field.
    """
    violations: list[Violation] = []
    for layer in _identifier_layers(registry):
        diagnose_element = _MEMBERS_AND_GENERATED_FORMS.get(layer.name)
        for element in staged.elements.get(layer.name, ()):
            identity = element.get(layer.identity_field)
            violations.extend(_package_name(layer, identity, element))
            if diagnose_element is not None:
                violations.extend(diagnose_element(element))
                continue
            violations.extend(_declared_name(Address(layer.name, str(identity)), layer.token, identity))
    return tuple(violations)


def _package_name(layer: LayerFacts, identity: Any, element: Mapping[str, Any]) -> tuple[Violation, ...]:
    """One element's declared ``package``, checked as the ``SHORT-NAME`` it is emitted as.

    Read off the element itself: an absent or ``null`` value is the root package,
    which emits no name.
    """
    package = element.get("package")
    if package is None:
        return ()
    address = Address(layer=layer.name, element=str(identity), path=(FieldStep("package"),))
    return _declared_name(address, f"{layer.token} '{identity}' package", package)


def _swc_identifiers(swc: Mapping[str, Any]) -> list[Violation]:
    """One component: its own name, its two generated forms, its members, its events, its stated names.

    A stated ``behavior_name``, event ``name`` or access entry ``name`` is emitted as
    it is, so it is judged as a declared name and the generated form it replaces is
    not judged.
    """
    name = swc["name"]
    element = Address("swc", name)
    external = swc.get("external") is True  # canonical: staged elements are re-published validated models
    stated_behaviour = swc.get("behavior_name")
    violations = list(_declared_name(element, "SWC", name, emitted=not external))
    # A stub emits neither generated form, so neither is charged; a stated
    # behaviour name replaces `_InternalBehavior`.
    if not violations and not external:
        suffixes = (RESERVED_IMPLEMENTATION_SUFFIX,) + (
            (GENERATED_INTERNAL_BEHAVIOR_SUFFIX,) if stated_behaviour is None else ()
        )
        for suffix in suffixes:
            violations.extend(_generated_name(element, f"SWC '{name}'", name, f"{name}{suffix}"))
    if stated_behaviour is not None:
        violations.extend(
            _declared_name(
                Address(layer=element.layer, element=element.element, path=(FieldStep("behavior_name"),)),
                f"SWC '{name}' internal behaviour",
                stated_behaviour,
                emitted=not external,
            )
        )

    # A stub's port names are checked too: each is the last segment of every
    # connector end on it. `can_enter`/`runs_inside` entries mint no name.
    for field, noun in (
        ("ports", "port"),
        ("runnables", "runnable"),
        ("variables", "variable"),
        ("exclusive_areas", "exclusive area"),
    ):
        for member in _members(swc, field):
            violations.extend(
                _declared_name(
                    _member_name_address(element, field, member),
                    f"SWC '{name}' {noun}",
                    member.get("name"),
                    reference_segment=external and field == "ports",
                )
            )
    # `{runnable}_Budget`, emitted for a runnable stating `wcet`, charged only
    # when the runnable's own name conforms.
    for member in () if external else _members(swc, "runnables"):
        runnable_name = member.get("name")
        if member.get("wcet") is None or not isinstance(runnable_name, str) or check_identifier(runnable_name):
            continue
        violations.extend(
            _generated_name(
                _member_name_address(element, "runnables", member),
                f"SWC '{name}' runnable '{runnable_name}'",
                runnable_name,
                f"{runnable_name}{GENERATED_BUDGET_SUFFIX}",
            )
        )

    # One `(runnable, event, base name)` per emitted event, in emission order:
    # `unique_names`' retroactive counter is defined over that order, so the
    # pairing and the counter come from one walk. A stated name keeps its slot and
    # is judged as a declared name.
    emitted_events = [
        (runnable, event, base)
        for runnable in _members(swc, "runnables")
        for event in _members(runnable, "events")
        if (base := _event_base(runnable, event)) is not None
    ]
    names = unique_names([base for _runnable, _event, base in emitted_events])
    for (runnable, event, _base), emitted in zip(emitted_events, names, strict=True):
        stated = event.get("name")
        if stated is not None:
            violations.extend(
                _declared_name(
                    _event_address(element, runnable, event),
                    f"SWC '{name}' runnable '{runnable.get('name')}' "
                    f"{EVENT_KIND_WORDS.get(str(event.get('type')), event.get('type'))} event",
                    stated,
                )
            )
            continue
        if event.get("type") == "operation":
            violations.extend(_operation_event_identifier(element, name, runnable, event, emitted))
            continue
        if event.get("type") == "data":
            violations.extend(_data_event_identifier(element, name, runnable, event, emitted))
            continue
        if event.get("type") == "call_returned":
            violations.extend(_call_returned_event_identifier(element, name, runnable, event, emitted))
            continue
        if check_identifier(str(runnable.get("name"))) is not None:
            continue  # already reported as a runnable name; one root cause, one issue
        violations.extend(
            _generated_name(
                _member_name_address(element, "runnables", runnable),
                f"SWC '{name}' runnable '{runnable.get('name')}'",
                str(runnable.get("name")),
                emitted,
            )
        )

    for runnable in _members(swc, "runnables"):
        violations.extend(_access_point_identifiers(element, name, runnable))
    return violations


def _event_address(element: Address, runnable: Mapping[str, Any], event: Mapping[str, Any]) -> Address:
    """One staged event, addressed as the whole value it is -- ``check-model``'s address for it."""
    return Address(
        layer=element.layer,
        element=element.element,
        path=(
            FieldStep("runnables"),
            MemberStep(runnable.get("name")),
            FieldStep("events"),
            MemberStep(_event_identity(event)),
        ),
    )


def _event_identity(event: Mapping[str, Any]) -> dict[str, Any]:
    """One staged event's whole-value identity: the dict-level twin of ``validator.event_identity``.

    The discriminator plus each kind's identifying fields and a stated ``name``; a
    field at its default is no part of it. An unknown ``type`` is pass A's to report
    and keeps the discriminator alone.
    """
    kind = event.get("type")
    identity: dict[str, Any] = {"type": kind}
    if kind == "timing":
        identity["period"] = event.get("period")
        modes = _members(event, "disabled_modes")
        if modes:
            identity["disabled_modes"] = _disabled_mode_identity(modes)
    elif kind == "operation":
        identity["port"] = event.get("port")
        identity["operation"] = event.get("operation")
    elif kind == "data":
        identity["port"] = event.get("port")
        identity["element"] = event.get("element")
        modes = _members(event, "disabled_modes")
        if modes:
            identity["disabled_modes"] = _disabled_mode_identity(modes)
    elif kind == "call_returned":
        identity["port"] = event.get("port")
        identity["operation"] = event.get("operation")
        if event.get("result") is not None:
            identity["result"] = event.get("result")
    if event.get("name") is not None:
        identity["name"] = event.get("name")
    return identity


def _disabled_mode_identity(modes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A staged event's ``disabled_modes`` as its identity carries them: each mode's three references, in order."""
    return [{key: mode.get(key) for key in ("port", "mode_group_ref", "mode_ref")} for mode in modes]


def _event_base(runnable: Mapping[str, Any], event: Mapping[str, Any]) -> str | None:
    """One staged event's ``SHORT-NAME`` before the collision counter, or ``None`` for no known kind.

    One arm per member of the ``events`` union; ``None`` is a schema violation, pass
    A's to report.
    """
    kind = event.get("type")
    if kind == "timing":
        return f"{GENERATED_TIMING_EVENT_PREFIX}{runnable.get('name')}"
    if kind == "init":
        return f"{GENERATED_INIT_EVENT_PREFIX}{runnable.get('name')}"
    if kind == "operation":
        return f"{GENERATED_OPERATION_EVENT_PREFIX}{event.get('port')}_{event.get('operation')}"
    if kind == "data":
        return f"{GENERATED_DATA_EVENT_PREFIX}{event.get('port')}_{event.get('element')}"
    if kind == "call_returned":
        return f"{GENERATED_RETURNS_EVENT_PREFIX}{event.get('port')}_{event.get('operation')}"
    return None


def _operation_event_identifier(
    element: Address, swc_name: str, runnable: Mapping[str, Any], event: Mapping[str, Any], emitted: str
) -> tuple[Violation, ...]:
    """``OIT_{port}_{operation}``, in the pair shape of the access forms.

    ``write_element`` refuses an over-budget pair during materialization, before
    pass D, and the engine re-asks this module. The address is the event, whose
    identity carries both names; the overhead is read off the final,
    collision-resolved name.
    """
    port_name, operation = event.get("port"), event.get("operation")
    if not isinstance(port_name, str) or not isinstance(operation, str):
        return ()  # a schema violation, and pass A's to report
    if check_identifier(port_name) is not None or check_identifier(operation) is not None:
        return ()  # one root cause, one issue — the declared name is the one to fix
    return _generated_pair_name(
        Address(
            layer=element.layer,
            element=element.element,
            path=(
                FieldStep("runnables"),
                MemberStep(runnable.get("name")),
                FieldStep("events"),
                MemberStep(_event_identity(event)),
            ),
        ),
        f"SWC '{swc_name}' runnable '{runnable.get('name')}' operation event '{port_name}'.'{operation}'",
        port_name,
        operation,
        emitted,
        len(emitted) - len(port_name) - len(operation),
    )


def _data_event_identifier(
    element: Address, swc_name: str, runnable: Mapping[str, Any], event: Mapping[str, Any], emitted: str
) -> tuple[Violation, ...]:
    """``DRT_{port}_{element}``, in :func:`_operation_event_identifier`'s pair shape, for its reason."""
    port_name, data_element = event.get("port"), event.get("element")
    if not isinstance(port_name, str) or not isinstance(data_element, str):
        return ()  # a schema violation, and pass A's to report
    if check_identifier(port_name) is not None or check_identifier(data_element) is not None:
        return ()  # one root cause, one issue — the declared name is the one to fix
    return _generated_pair_name(
        _event_address(element, runnable, event),
        f"SWC '{swc_name}' runnable '{runnable.get('name')}' data-received event '{port_name}'.'{data_element}'",
        port_name,
        data_element,
        emitted,
        len(emitted) - len(port_name) - len(data_element),
    )


def _call_returned_event_identifier(
    element: Address, swc_name: str, runnable: Mapping[str, Any], event: Mapping[str, Any], emitted: str
) -> tuple[Violation, ...]:
    """``ASRT_{port}_{operation}``, in :func:`_operation_event_identifier`'s pair shape, for its reason."""
    port_name, operation = event.get("port"), event.get("operation")
    if not isinstance(port_name, str) or not isinstance(operation, str):
        return ()  # a schema violation, and pass A's to report
    if check_identifier(port_name) is not None or check_identifier(operation) is not None:
        return ()  # one root cause, one issue — the declared name is the one to fix
    return _generated_pair_name(
        _event_address(element, runnable, event),
        f"SWC '{swc_name}' runnable '{runnable.get('name')}' call-returned event '{port_name}'.'{operation}'",
        port_name,
        operation,
        emitted,
        len(emitted) - len(port_name) - len(operation),
    )


#: Which member field carries the interface member each access list names, beside
#: the ``port`` every entry carries. The composite identity the registry
#: publishes for the collection is ``{port, element}`` / ``{port, operation}``.
_ACCESS_MEMBER_FIELDS = {"reads": "element", "writes": "element", "calls": "operation"}


def _access_point_identifiers(element: Address, swc_name: str, runnable: Mapping[str, Any]) -> list[Violation]:
    """One runnable's access lists, as declared names and generated forms.

    ``write_element``'s access guard refuses an over-budget pair during
    materialization, before pass D, and the engine re-asks this module; without the
    form here the writer's ``ValueError`` would escape a well-formed change set.
    Only the length half lives here: two entries deriving one access point is a
    ``ReservedNameError`` the engine already translates. The address is the entry,
    which carries both names. The variable lists follow
    (:func:`_variable_access_identifiers`), then the result points.
    """
    violations: list[Violation] = []
    for list_name, member_field in _ACCESS_MEMBER_FIELDS.items():
        for entry in _members(runnable, list_name):
            port_name, member = entry.get("port"), entry.get(member_field)
            if not isinstance(port_name, str) or not isinstance(member, str):
                continue  # a schema violation, and pass A's to report
            stated = entry.get("name")
            if stated is not None:
                # A stated name is emitted instead of the derived form.
                violations.extend(
                    _declared_name(
                        Address(
                            layer=element.layer,
                            element=element.element,
                            path=(
                                FieldStep("runnables"),
                                MemberStep(runnable.get("name")),
                                FieldStep(list_name),
                                MemberStep({"port": port_name, member_field: member}),
                                FieldStep("name"),
                            ),
                        ),
                        f"SWC '{swc_name}' runnable '{runnable.get('name')}' {list_name} entry "
                        f"'{port_name}'.'{member}'",
                        stated,
                    )
                )
                continue
            if check_identifier(port_name) is not None or check_identifier(member) is not None:
                continue  # one root cause, one issue — the declared name is the one to fix
            prefix = ACCESS_LIST_PREFIXES[list_name]
            violations.extend(
                _generated_pair_name(
                    Address(
                        layer=element.layer,
                        element=element.element,
                        path=(
                            FieldStep("runnables"),
                            MemberStep(runnable.get("name")),
                            FieldStep(list_name),
                            MemberStep({"port": port_name, member_field: member}),
                        ),
                    ),
                    f"SWC '{swc_name}' runnable '{runnable.get('name')}' {list_name} entry '{port_name}'.'{member}'",
                    port_name,
                    member,
                    access_point_name(prefix, port_name, member),
                    access_point_added_length(prefix),
                )
            )
    violations.extend(_variable_access_identifiers(element, swc_name, runnable))
    violations.extend(_result_point_identifiers(element, swc_name, runnable))
    return violations


def _variable_access_identifiers(element: Address, swc_name: str, runnable: Mapping[str, Any]) -> list[Violation]:
    """One runnable's two variable access lists: stated names and ``RV_``/``WV_`` forms.

    A stated name is judged at the entry's ``name``; an unstated one's derived form
    at the entry, only when the variable name conforms.
    """
    violations: list[Violation] = []
    for list_name, prefix in VARIABLE_ACCESS_LIST_PREFIXES.items():
        for entry in _members(runnable, list_name):
            variable = entry.get("variable")
            if not isinstance(variable, str):
                continue  # a schema violation, and pass A's to report
            address = Address(
                layer=element.layer,
                element=element.element,
                path=(
                    FieldStep("runnables"),
                    MemberStep(runnable.get("name")),
                    FieldStep(list_name),
                    MemberStep(variable),
                ),
            )
            subject = f"SWC '{swc_name}' runnable '{runnable.get('name')}' {list_name} entry '{variable}'"
            stated = entry.get("name")
            if stated is not None:
                violations.extend(
                    _declared_name(
                        Address(layer=address.layer, element=address.element, path=(*address.path, FieldStep("name"))),
                        subject,
                        stated,
                    )
                )
                continue
            if check_identifier(variable) is not None:
                continue  # one root cause, one issue — the variable's own name is the one to fix
            violations.extend(_generated_name(address, subject, variable, f"{prefix}{variable}"))
    return violations


def _interface_identifiers(interface: Mapping[str, Any]) -> list[Violation]:
    """One interface: its name, its members, and ``{Interface}_{Element}_Init``.

    The init constant's length is checked here because ``write_element`` refuses an
    over-budget pair during materialization, before pass D, and the engine re-asks
    this function. Its two collisions are whole-model questions the writer raises
    as ``ReservedNameError``.
    """
    name = interface["name"]
    element = Address("interfaces", name)
    if interface.get("kind") == "external_interface":
        stub_violations = list(_declared_name(element, "external interface stub", name, emitted=False))
        # Each listed member name is the last segment of every reference to it,
        # so it is held to the declared-name rule.
        for field, noun, _dest in STUB_MEMBER_LISTS.values():
            members = interface.get(field)
            for member in members if isinstance(members, list) else ():
                stub_violations.extend(
                    _declared_name(
                        Address(
                            layer=element.layer, element=element.element, path=(FieldStep(field), MemberStep(member))
                        ),
                        f"external interface stub '{name}' {noun}",
                        member,
                        reference_segment=True,
                    )
                )
        return stub_violations
    violations = list(_declared_name(element, "interface", name))
    interface_conforms = isinstance(name, str) and check_identifier(name) is None
    for member in _members(interface, "data_elements"):
        member_violations = _declared_name(
            _member_name_address(element, "data_elements", member),
            f"interface '{name}' data element",
            member.get("name"),
        )
        violations.extend(member_violations)
        member_name = member.get("name")
        if interface_conforms and not member_violations and isinstance(member_name, str):
            violations.extend(
                _generated_pair_name(
                    element,
                    f"interface '{name}' data element '{member_name}'",
                    name,
                    member_name,
                    init_constant_name(name, member_name),
                    INIT_CONSTANT_ADDED_LENGTH,
                )
            )
    for operation in _members(interface, "operations"):
        operation_address = _member_name_address(element, "operations", operation)
        violations.extend(_declared_name(operation_address, f"interface '{name}' operation", operation.get("name")))
        for argument in _members(operation, "arguments"):
            violations.extend(
                _declared_name(
                    Address(
                        layer="interfaces",
                        element=name,
                        path=(
                            FieldStep("operations"),
                            MemberStep(operation.get("name")),
                            FieldStep("arguments"),
                            MemberStep(argument.get("name")),
                            FieldStep("name"),
                        ),
                    ),
                    f"interface '{name}' operation '{operation.get('name')}' argument",
                    argument.get("name"),
                )
            )
    return violations


def _generated_pair_name(
    address: Address, subject: str, first: str, second: str, emitted: str, added_length: int
) -> tuple[Violation, ...]:
    """One generated form derived from two conforming declared names.

    The message states the budget the two names share, the number the writer's
    refusal and ``check-model`` state. *added_length* is what the writer adds to the
    pair, which differs per form.
    """
    reason = check_identifier(emitted)
    if reason is None:
        return ()
    budget = MAX_IDENTIFIER_LENGTH - added_length
    return (
        Violation(
            error=INVALID_IDENTIFIER,
            message=(
                f"{subject} declares two legal names, but the exporter emits '{emitted}' "
                f"({len(emitted)} characters) and that {reason}. '{first}' and '{second}' get "
                f"{budget} characters between them: the identifier rule is checked on what is "
                "emitted, not on what is declared"
            ),
            addresses=(address,),
        ),
    )


def _composition_identifiers(composition: Mapping[str, Any]) -> list[Violation]:
    """One composition: its own name and its ports', members' and delegations'.

    No generated form lengthens any of them. A member's ``type`` and a delegation's
    references name other elements, which are diagnosed where they are declared.
    """
    name = composition["name"]
    element = Address("compositions", name)
    violations = list(_declared_name(element, "composition", name))
    for field in ("ports", "members", "delegations"):
        for member in _members(composition, field):
            violations.extend(
                _declared_name(
                    _member_name_address(element, field, member),
                    f"composition '{name}' {field[:-1]}",
                    member.get("name"),
                )
            )
    return violations


def _data_type_identifiers(data_type: Mapping[str, Any], layer: str = "implementation_types") -> list[Violation]:
    """One data type: its name, the generated forms its kind emits, and its members.

    Kind-scoped forms come from the writer's ``RESERVED_DATA_TYPE_RULES`` and
    field-scoped ones from ``RESERVED_DATA_TYPE_FORM_RULES``
    (:func:`_generated_form_suffixes`), read rather than restated, so the engine,
    ``check-model`` and the writer agree. *layer* is ``implementation_types`` or
    ``application_types``.
    """
    name = data_type["name"]
    kind = data_type.get("kind")
    element = Address(layer, name)
    if kind == "external_type":
        return list(_declared_name(element, "external data type stub", name, emitted=False))
    violations = list(_declared_name(element, "data type", name))
    if not violations:
        for suffix in _generated_data_type_suffixes(data_type):
            violations.extend(_generated_name(element, f"data type '{name}'", name, f"{name}{suffix}"))
        for suffix in _generated_form_suffixes(data_type):
            violations.extend(_generated_name(element, f"data type '{name}'", name, f"{name}{suffix}"))

    field = (
        "literals"
        if kind == "enum"
        else "fields"
        if kind == "record"
        else "elements"
        if kind == "implementation_record"
        else None
    )
    if field is not None:
        for member in _members(data_type, field):
            violations.extend(
                _declared_name(
                    _member_name_address(element, field, member),
                    f"{kind} '{name}' {field[:-1]}",
                    member.get("name"),
                )
            )
    if kind == "implementation_array":
        # The array's one sub-element carries `element_name` as its SHORT-NAME.
        violations.extend(
            _declared_name(
                Address(layer=element.layer, element=element.element, path=(FieldStep("element_name"),)),
                f"{kind} '{name}' element",
                data_type.get("element_name"),
            )
        )
    return violations


def _application_type_identifiers(data_type: Mapping[str, Any]) -> list[Violation]:
    """One application type (contract ``8.0.0``): :func:`_data_type_identifiers` addressed in its own layer."""
    return _data_type_identifiers(data_type, layer="application_types")


def _compu_method_identifiers(compu_method: Mapping[str, Any]) -> list[Violation]:
    """One compu method (contract ``8.0.0``): its name, and a ``texttable``'s entry names.

    An entry's ``name`` is written as its scale's ``SHORT-LABEL`` and ``VT``,
    exactly as an enumeration literal's, so it is held to the rule a literal is.
    No generated form lengthens the method's own name.
    """
    name = compu_method["name"]
    element = Address("compu_methods", name)
    violations = list(_declared_name(element, "compu method", name))
    for member in _members(compu_method, "entries"):
        violations.extend(
            _declared_name(
                _member_name_address(element, "entries", member),
                f"texttable '{name}' entry",
                member.get("name"),
            )
        )
    return violations


#: The layers whose elements emit more than their declared identity: members with
#: names of their own, generated forms, or both. Not a scope statement: a bound
#: layer absent here is still diagnosed by its declared identity. The entries are
#: code because each subject is reported in prose no declaration could supply.
_MEMBERS_AND_GENERATED_FORMS: dict[str, Callable[[Mapping[str, Any]], list[Violation]]] = {
    "swc": _swc_identifiers,
    "compositions": _composition_identifiers,
    "interfaces": _interface_identifiers,
    "implementation_types": _data_type_identifiers,
    "application_types": _application_type_identifiers,
    "compu_methods": _compu_method_identifiers,
}


def _suffixes_by_kind() -> dict[str, tuple[str, ...]]:
    """``RESERVED_DATA_TYPE_RULES`` keyed by the published kind name.

    Built once, at import, over the shipped contract: the writer's table is keyed by
    shipped classes, and resolving the context here would bind whichever registry
    was in scope at first import. A class the registry does not name raises rather
    than leaving a generated form unchecked.
    """
    model_registry = production_registry()
    by_kind: dict[str, list[str]] = {}
    for suffix, _element, kinds in RESERVED_DATA_TYPE_RULES:
        for model_class in kinds:
            facts = model_registry.kind_of_model(model_class)
            if facts is None:  # pragma: no cover - a writer rule naming a class the contract does not bind
                raise ModelRegistryError(f"no element kind is bound to {model_class.__name__}")
            by_kind.setdefault(facts.kind, []).append(suffix)
    return {kind: tuple(suffixes) for kind, suffixes in by_kind.items()}


#: Published data-type kind -> the ``SHORT-NAME`` suffixes the exporter derives
#: from a name of that kind. ``record`` and ``array`` are absent: they get no
#: generated form at all, which is exactly why a kind-blind guard misfires.
_GENERATED_DATA_TYPE_SUFFIXES: dict[str, tuple[str, ...]] = _suffixes_by_kind()


def _generated_data_type_suffixes(data_type: Mapping[str, Any]) -> tuple[str, ...]:
    """The kind-scoped ``SHORT-NAME`` suffixes the exporter derives from *data_type*'s name.

    An enumeration naming a ``texttable`` instead of ``literals`` generates no
    ``_CompuMethod`` (:func:`~swcraft_tools.writer.arxml_generation.generates_kind_form`).
    """
    if data_type.get("kind") == "enum" and data_type.get("literals") is None:
        return ()
    return _GENERATED_DATA_TYPE_SUFFIXES.get(str(data_type.get("kind")), ())


def _form_kind(model_class: type[BaseModel]) -> str:
    """The published kind name of a class :data:`RESERVED_DATA_TYPE_FORM_RULES` names.

    Read off the class's own ``kind`` discriminator (a one-value ``Literal``), so
    nothing resolves a registry: the writer's table is keyed by shipped classes,
    and their discriminator is the kind name a staged document carries.
    """
    (kind,) = get_args(model_class.model_fields["kind"].annotation)
    return kind


def _declares(data_type: Mapping[str, Any], field: str) -> bool:
    """Whether the raw document declares *field* the way the writer's presence rule reads it.

    A ``unit`` counts only with non-blank text (a blank unit is no unit); every
    other declaring field counts whenever it is present and not ``null``.
    """
    if field == "unit":
        return bool(str(data_type.get("unit") or "").strip())
    return data_type.get(field) is not None


def _generated_form_suffixes(data_type: Mapping[str, Any]) -> tuple[str, ...]:
    """The field-scoped suffixes *data_type*'s name generates, whatever its kind.

    Each form is minted only where the type declares a field it is emitted for
    (``arxml_generation.generated_data_type_forms``), so two types of one kind can
    mint different sets. The presence rule is restated over the raw document, since
    the writer's predicate takes a model object; the (suffix, kind, fields) table is
    read from ``RESERVED_DATA_TYPE_FORM_RULES``.
    """
    kind = data_type.get("kind")
    return tuple(
        dict.fromkeys(
            suffix
            for suffix, _element, model_class, fields in RESERVED_DATA_TYPE_FORM_RULES
            if _form_kind(model_class) == kind and any(_declares(data_type, field) for field in fields)
        )
    )


def _declared_name(
    address: Address, subject: str, name: Any, *, emitted: bool = True, reference_segment: bool = False
) -> tuple[Violation, ...]:
    """One declared name, checked as it is emitted. Empty when it conforms.

    *emitted* is ``False`` for an external stub, emitted as no ``SHORT-NAME``;
    *reference_segment* is ``True`` for a stub's port, emitted only as the last
    segment of connector ends. Both change only the message's wording.
    """
    if not isinstance(name, str):  # pragma: no cover - every name field is a validated `str`
        return ()
    reason = check_identifier(name)
    if reason is None:
        return ()
    if reference_segment:
        held = "is emitted as the last segment of an AUTOSAR reference path"
    elif emitted:
        held = "is emitted as an AUTOSAR SHORT-NAME"
    else:
        held = "is held to the AUTOSAR Identifier rule, although a stub is emitted as no SHORT-NAME,"
    return (
        Violation(
            error=INVALID_IDENTIFIER,
            message=f"the {subject} name '{name}' {held} and {reason}",
            addresses=(address,),
        ),
    )


def _generated_name(address: Address, subject: str, name: str, emitted: str) -> tuple[Violation, ...]:
    """One *generated* form of a conforming declared name. Empty when it conforms."""
    reason = check_identifier(emitted)
    if reason is None:
        return ()
    return (
        Violation(
            error=INVALID_IDENTIFIER,
            message=(
                f"{subject} declares a legal name, but the exporter emits '{emitted}' "
                f"({len(emitted) - len(name)} characters more than '{name}') and that {reason}. "
                "The identifier rule is checked on what is emitted, not on what is declared"
            ),
            addresses=(address,),
        ),
    )


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


def _members(owner: Mapping[str, Any], field: str) -> Sequence[Mapping[str, Any]]:
    """One collection field's members, or nothing when the field is absent or not a list."""
    value = owner.get(field)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return [member for member in value if isinstance(member, Mapping)]


def _member_name_address(element: Address, field: str, member: Mapping[str, Any]) -> Address:
    """The address of one ``field``-identified member's ``name``, three steps deep."""
    return Address(
        layer=element.layer,
        element=element.element,
        path=(FieldStep(field), MemberStep(member.get("name")), FieldStep("name")),
    )


# ---------------------------------------------------------------------------
# The result points
# ---------------------------------------------------------------------------


def _result_point_identifiers(element: Address, swc_name: str, runnable: Mapping[str, Any]) -> list[Violation]:
    """One runnable's result points: a poll's on its call, a ``call_returned`` event's on the event.

    The dict-level twin of ``validator.result_point_sites``, here for the access
    forms' reason. A stated ``result`` is a declared name; an unstated one is
    ``Result_{port}_{operation}``, a joint budget over the pair, charged only when
    both names conform.
    """
    violations: list[Violation] = []
    at_runnable = (FieldStep("runnables"), MemberStep(runnable.get("name")))
    sites: list[tuple[str, Any, Any, Any, tuple[PathStep, ...]]] = []
    for call in _members(runnable, "calls"):
        if call.get("poll") is not True:
            continue
        entry = (
            *at_runnable,
            FieldStep("calls"),
            MemberStep({"port": call.get("port"), "operation": call.get("operation")}),
        )
        stated = call.get("result")
        sites.append(
            (
                f"SWC '{swc_name}' runnable '{runnable.get('name')}' calls entry "
                f"'{call.get('port')}'.'{call.get('operation')}' result point",
                call.get("port"),
                call.get("operation"),
                stated,
                (*entry, FieldStep("result")) if stated is not None else entry,
            )
        )
    for event in _members(runnable, "events"):
        if event.get("type") != "call_returned":
            continue
        sites.append(
            (
                f"SWC '{swc_name}' runnable '{runnable.get('name')}' call-returned event "
                f"'{event.get('port')}'.'{event.get('operation')}' result point",
                event.get("port"),
                event.get("operation"),
                event.get("result"),
                (*at_runnable, FieldStep("events"), MemberStep(_event_identity(event))),
            )
        )
    for subject, port_name, operation, stated, path in sites:
        address = Address(layer=element.layer, element=element.element, path=path)
        if stated is not None:
            violations.extend(_declared_name(address, f"{subject}", stated))
            continue
        if not isinstance(port_name, str) or not isinstance(operation, str):
            continue  # a schema violation, and pass A's to report
        if check_identifier(port_name) is not None or check_identifier(operation) is not None:
            continue  # one root cause, one issue — the declared name is the one to fix
        emitted = derived_result_point_name(port_name, operation)
        violations.extend(
            _generated_pair_name(
                address, subject, port_name, operation, emitted, len(emitted) - len(port_name) - len(operation)
            )
        )
    return violations
