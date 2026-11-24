"""The closed rule inventory of the validator.

Holds :data:`CONSISTENCY_RULES`, every rule identifier any check of the package
can report with a one-line statement of what it means, grouped by the check that
mints it, and :data:`EXTERNAL_INTERFACE_DESTS`, the ``DEST`` values an external
interface stub may carry. A rule's severity is not here: :data:`WARNING_RULES`
in :mod:`.results` names the warnings.
"""

from __future__ import annotations

from swcraft_tools.model_registry import DEVICE_NAME_SPELLINGS

# ---------------------------------------------------------------------------
# The closed rule inventory
# ---------------------------------------------------------------------------

#: Every rule any check of the package can report, with what it means. A check that adds a rule adds it here; every
#: error rule maps onto an editing-protocol error code.
CONSISTENCY_RULES: dict[str, str] = {
    # check_cross_layer_consistency's own check; every other rule belongs to a composed check below.
    "trace.link-unknown-requirement": (
        "A `satisfies` member naming no declared requirement, or carrying a version that is not one: a "
        "member is `<id>@<version>`, the version a positive integer, or the bare `<id>`."
    ),
    # check_requirement_traceability
    "trace.requirement-unallocated": (
        "A requirement no `satisfies` member of any element names; a `derived_from` naming it is not an allocation."
    ),
    "trace.link-version-not-stated": (
        "A `satisfies` or `derived_from` member naming a requirement by its bare `<id>`, with no `@<version>`."
    ),
    "trace.link-needs-confirmation": (
        "A `satisfies` or `derived_from` member whose `@<version>` is not the named requirement's current `version`."
    ),
    "trace.derived-from-unknown-target": (
        "A `derived_from` member naming no declared requirement and no top-level element of a linkable layer, or "
        "carrying a version that is not one."
    ),
    "trace.derived-from-cycle": "Requirements deriving from each other, through `derived_from`, in a cycle.",
    "trace.decomposition-context-mismatch": (
        "A decomposed requirement `X(Y)` whose `Y` is not the level before decomposition of a levelled requirement "
        "it derives from: that parent's own `(Y)` when it is decomposed, its level otherwise."
    ),
    "trace.requirement-stub": "A requirement that is a stub: named as a parent and never imported.",
    "trace.duplicate-link": (
        "One `satisfies` or `derived_from` list naming one target twice: a requirement, at the same version or "
        "at two, or one element; each list names a target once."
    ),
    "trace.link-unpublishable-id": (
        "A `satisfies` or `derived_from` member naming a requirement whose id holds `.`, which Sphinx-Needs reads "
        "as an id and a part: `generate needs` cannot publish the link."
    ),
    "trace.requirement-blank-text": (
        "An architecture requirement whose `title` or `description` is empty or whitespace only: it is published, "
        "and the requirements import refuses it."
    ),
    # check_connector_consistency
    "connector.duplicate-name": "Two connectors declaring the same name.",
    "connector.duplicate-link": "Two connectors declaring the same endpoint pair.",
    "connector.endpoint-unknown-swc": "A connector endpoint naming no declared SWC.",
    "connector.endpoint-unknown-composition": "A connector endpoint naming no declared composition.",
    "connector.endpoint-unknown-port": "A connector endpoint naming a port that component does not declare.",
    "connector.from-not-provide": "A connector's `from` endpoint is not a provide port.",
    "connector.to-not-require": "A connector's `to` endpoint is not a require port.",
    "connector.interface-mismatch": (
        "A connector whose required port's interface is not satisfied by the provided port's: a kind that "
        "differs (an external interface stub's kind is the one its `external_dest` names), a data element or "
        "operation without a counterpart of the same name, or a counterpart whose resolved type or signature "
        "differs, a data element queued on one side only, or an `is_service` that differs. Not judged: an "
        "external interface stub beyond its kind, or whose `external_dest` is abstract; invalidValue, a value "
        "type's own unit and range, interface mappings."
    ),
    # [constr_1295], judged only between declared application components (the one component kind the writer emits) and
    # only its `typed by` half; its `mapped to` half is `mapping-set.pointer-mapped`.
    "connector.pointer-between-application-components": (
        "A connector whose two ends are application components, followed through delegations across nested "
        "compositions, and whose interface carries a data element or an argument typed by a pointer, "
        "directly or through reference types: a pointer crosses a port only where one end is a service or an "
        "ECU-abstraction component. Not judged: an end that is an external component stub or that no "
        "delegation resolves to a component, and a pointer inside an implementation record or array."
    ),
    # Also check_connector_consistency.
    "connector.unknown-composition": "A connector naming no declared composition.",
    # check_composition_consistency, with the three `swc` rules about an external stub component.
    "composition.no-root": "No composition declares `root: true`; exactly one is required.",
    "composition.multiple-roots": "More than one composition declares `root: true`; exactly one is allowed.",
    "composition.duplicate-name": "The same composition name declared in more than one file.",
    "composition.duplicate-port": "One composition declaring a boundary port name twice.",
    "composition.duplicate-member": "One composition declaring a member name twice.",
    "composition.duplicate-delegation": "One composition declaring a delegation name twice.",
    "composition.duplicate-delegation-link": (
        "Two delegations of one composition joining the same boundary port to the same member port."
    ),
    "composition.cycle": "A dependency cycle in the composition hierarchy.",
    "composition.member-unknown-type": "A composition member naming no declared SWC.",
    "composition.member-unknown-composition": "A composition member naming no declared composition.",
    "composition.member-type-repeated": "One composition placing the same type in more than one member.",
    "composition.delegation-unknown-member": "A delegation naming no member of its composition.",
    "composition.delegation-unknown-port": (
        "A delegation naming a port its composition or its member's type does not declare."
    ),
    "composition.delegation-port-direction-mismatch": "A delegation whose two ports declare different directions.",
    "composition.delegation-interface-mismatch": (
        "A delegation whose inner port's interface is not compatible with its outer port's: a kind that "
        "differs (an external interface stub's kind is the one its `external_dest` names); for a require "
        "port, a data element or operation of the inner interface without a compatible counterpart of the "
        "same name in the outer; for a provide port, not one such member. A counterpart is compatible when "
        "its resolved type or signature agrees and it is queued exactly when the member is, and the two "
        "interfaces' `is_service` must agree. Not judged: an external interface stub beyond its kind, or whose "
        "`external_dest` is abstract; invalidValue, a value type's own unit and range, interface mappings."
    ),
    "composition.connector-endpoint-not-a-member": (
        "A connector endpoint naming a type that is not a member of the connector's composition."
    ),
    "composition.delegation-connector-name-collision": (
        "A delegation with the name of a connector drawn inside the same composition."
    ),
    "swc.external-with-ports": (
        "An external stub component declaring a port with an interface, measures or com-specs, runnables, variables, "
        "exclusive areas, a memory budget, data type mappings, a termination and restart handling, a package or a "
        "behaviour name."
    ),
    "swc.external-incomplete-reference": ("An external stub with a blank or malformed `external_ref`/`external_dest`."),
    "swc.external-reference-without-stub": (
        "A component declaring `external_ref` or `external_dest` without `external: true`."
    ),
    # A rule of its own: widening a shipped rule's condition is a major contract change, a new identifier a minor.
    "swc.external-reference-inside-generated-package": (
        "An external stub whose `external_ref` lies inside a package the generated ARXML declares the "
        "project's own elements in."
    ),
    # The two stub kinds' reference pairs. An interface stub admits the eight port interface `DEST` values of the
    # AUTOSAR schema, a type stub `IMPLEMENTATION-DATA-TYPE` alone.
    "interface.external-incomplete-reference": (
        "An external interface stub with a blank or malformed `external_ref` or one inside a package the "
        "generated ARXML declares the project's own elements in, or an `external_dest` that is not a port "
        "interface `DEST`."
    ),
    "data-type.external-incomplete-reference": (
        "An external data type stub with a blank or malformed `external_ref` or one inside a package the "
        "generated ARXML declares the project's own elements in, or an `external_dest` other than "
        "`IMPLEMENTATION-DATA-TYPE`."
    ),
    # check_port_interface_references
    "port.undeclared-interface": (
        "A port of a component that is not an external stub naming no declared interface, or none at all (an "
        "external interface stub counts as declared)."
    ),
    "composition.port-undeclared-interface": (
        "A composition boundary port naming no declared interface (an external interface stub counts as declared)."
    ),
    # check_runnable_access
    "swc.access.unknown-port": "A runnable access naming a port its component does not declare.",
    "swc.access.unknown-element": (
        "A runnable read or write naming no data element of the port's sender/receiver interface, or, through a "
        "port on an external interface stub, no name the stub lists in its `data_elements`."
    ),
    "swc.access.unknown-operation": (
        "A runnable call naming no operation of the port's client/server interface, or, through a port on an "
        "external interface stub, no name the stub lists in its `operations`."
    ),
    "swc.access.direction-mismatch": "A read or call through a provide port, or a write through a require port.",
    "swc.access.interface-kind-mismatch": (
        "A read or write through a client/server port, or a call through a sender/receiver port."
    ),
    # check_operation_events, including the partial `[constr_2000]` over one runnable's operations. Mixed event kinds
    # are not judged.
    "swc.operation-event.unknown-port": "An operation event naming a port its component does not declare.",
    "swc.operation-event.direction-mismatch": "An operation event through a require port.",
    "swc.operation-event.interface-kind-mismatch": (
        "An operation event through a port whose interface is sender/receiver."
    ),
    "swc.operation-event.unknown-operation": (
        "An operation event naming no operation of the port's client/server interface, or, through a port on an "
        "external interface stub, no name the stub lists in its `operations`."
    ),
    "swc.operation-event.duplicate-binding": (
        "A `(port, operation)` pair named by more than one operation event of one component, on two runnables "
        "or twice on one."
    ),
    "swc.operation-event.incompatible-signatures": (
        "A runnable started by operation events whose operations differ in argument count, in an argument's "
        "direction, or in the structure of an argument's resolved implementation type, position by position. "
        "Not judged: a position whose type does not resolve completely, or whose application type or "
        "enumeration the component's mapping sets do not map to exactly one implementation type; possible "
        "errors, the return value, errorHandling, serverArgumentImplPolicy and PortDefinedArgumentValues."
    ),
    # check_data_type_references
    "data-type.duplicate-name": "The same data type name declared in more than one file of one layer.",
    "data-type.compu-method-name-collision": (
        "A declared name colliding with the `_CompuMethod` generated for an enum that states `literals`, in the "
        "enum's package."
    ),
    "interface.data-element-undeclared-type": (
        "A sender/receiver data element whose `application_type` names no declared application type, or whose "
        "`implementation_type` names no declared implementation type (an external data type stub counts as declared)."
    ),
    "interface.argument-undeclared-type": (
        "A client/server argument whose `application_type` names no declared application type, or whose "
        "`implementation_type` names no declared implementation type (an external data type stub counts as declared)."
    ),
    # A reference type's `base` must be a value type or another reference type. A value type's `base` resolves in
    # `base_types` alone; a base type's name in any other role is an undeclared name of the layer that role resolves in.
    "data-type.value-type-undeclared-base": "A value type whose `base` names no declared base type.",
    "data-type.reference-type-undeclared-base": (
        "A reference type whose `base` names no declared type (an external data type stub counts as declared)."
    ),
    # Every implementation role resolves in `implementation_types` and every application role in `application_types`, so
    # a name of the wrong kind is an undeclared name of the role's layer.
    "data-type.implementation-element-undeclared-type": (
        "An implementation record element whose `type` names no declared type."
    ),
    "data-type.implementation-record-duplicate-element-name": (
        "An implementation record declaring one element name twice."
    ),
    "data-type.implementation-array-undeclared-element-type": (
        "An implementation array whose `element_type` names no declared type."
    ),
    "data-type.pointer-undeclared-target": "A pointer whose `target` names no declared type.",
    # The three `SHORT-NAME`s an application type's name generates, each reserved only when the field it is emitted for
    # is declared.
    "data-type.unit-name-collision": (
        "A declared name colliding with an application type's generated `_Unit` in the type's package."
    ),
    "data-type.compu-name-collision": (
        "A declared name colliding with an application type's generated `_Compu` in the type's package."
    ),
    "data-type.range-name-collision": (
        "A declared name colliding with an application type's or a value type's generated `_Range` in the type's "
        "package."
    ),
    # check_init_conformance
    # Each also judges a port's own `init` in `com_specs`, against the type of the data element the entry names.
    "interface.init-shape-mismatch": (
        "An init value — a data element's own, a port's own in `com_specs`, or a component's variable's — whose shape "
        "the declared type cannot hold."
    ),
    "interface.init-unknown-enum-literal": (
        "An init value — a data element's own, a port's own in `com_specs`, or a component's variable's — naming no "
        "literal of its declared enum type, or no entry of the table its declared value type or reference type "
        "references."
    ),
    "interface.init-record-field-mismatch": (
        "An init map — a data element's own, a port's own in `com_specs`, or a component's variable's — whose keys are "
        "not exactly its record type's fields or its implementation record's elements."
    ),
    "interface.init-array-length-mismatch": (
        "An init list — a data element's own, a port's own in `com_specs`, or a component's variable's — whose length "
        "is not its array or implementation array type's declared length."
    ),
    "interface.init-out-of-range": (
        "An init value — a data element's own, a port's own in `com_specs`, or a component's variable's — outside the "
        "range its value type declares."
    ),
    # A text names an enumeration's literal or a table's entry, so a type that states no table carries a number. The
    # emitter writes an integer as `str(value)`, bounded by `sys.get_int_max_str_digits()`. The writer refuses both.
    "interface.init-string-scalar": (
        "An init value — a data element's own, a port's own in `com_specs`, or a component's variable's — written as "
        "text where its declared type carries a number."
    ),
    "interface.init-unwritable-integer": (
        "An init integer — a data element's own, a port's own in `com_specs`, or a component's variable's — too wide "
        "to be written out in full decimal digits."
    ),
    # The writer cannot shape an init by a type outside the project.
    "interface.init-on-external-type": (
        "An init value on a data element, or a port's own in `com_specs` for one, or on a component's variable, whose "
        "type is an external data type stub, named directly, through a chain of reference types, or as a member of an "
        "implementation record or array."
    ),
    "data-type.record-field-undeclared-type": "A record field naming no declared application type.",
    "data-type.record-duplicate-field-name": "A record declaring one field name twice.",
    "data-type.array-undeclared-element-type": "An array whose `element_type` names no declared application type.",
    "data-type.enum-duplicate-literal-name": "An enum declaring one literal name twice.",
    "data-type.enum-duplicate-literal-value": "An enum declaring one literal value twice.",
    # check_data_type_cycles
    "data-type.cycle": "A dependency cycle in the data-type graph.",
    # check_task_consistency
    "task.duplicate-name": "The same task name declared in more than one file.",
    "task.mapping-unknown-swc": "A task mapping naming no declared SWC.",
    "task.mapping-unknown-runnable": "A task mapping naming a runnable that component does not declare.",
    "task.runnable-period-not-multiple": "A runnable timing period that is not a positive multiple of its task's.",
    "task.runnable-mapped-to-multiple-tasks": (
        "One runnable of one resolved component mapped by more than one task, whichever of `swc` and "
        "`instance` each entry names it by."
    ),
    "task.instance-in-multiple-partitions": (
        "A component instance whose runnables are run by tasks of two or more partitions, judged per resolved "
        "instance: two occurrences of one type are two instances, and a type placed nowhere below the root "
        "composition is judged as one. Not judged without exactly one root composition or on a composition "
        "cycle."
    ),
    "task.resolved-duplicate-runnable": (
        "A task mapping one runnable of one resolved component twice, through two entries written differently."
    ),
    "task.instance-ambiguous": (
        "A task mapping naming by `swc` a component type placed more than once below the root composition, so "
        "it names no one instance; name the occurrence by `instance`."
    ),
    "task.instance-unknown": (
        "A task mapping whose `instance` path does not walk from the root composition to a member: a segment "
        "names no member, or descends into a component or an undeclared composition."
    ),
    "task.instance-not-a-component": (
        "A task mapping whose `instance` path ends on a member that instantiates a composition."
    ),
    # check_sequence_consistency
    "sequence.duplicate-name": "The same sequence name declared in more than one file.",
    "sequence.unknown-task": "A sequence naming no declared task.",
    "sequence.step-not-mapped": "A sequence step its own task does not map.",
    "sequence.step-unknown-swc": "A sequence step naming no declared SWC.",
    "sequence.step-unknown-runnable": "A sequence step naming a runnable that component does not declare.",
    "sequence.step-order": "A sequence whose step order is not a subsequence of its task's runnable order.",
    "sequence.resolved-duplicate-step": (
        "A sequence showing one runnable of one resolved component twice, through two steps written differently."
    ),
    "sequence.step-instance-ambiguous": (
        "A sequence step naming by `swc` a component type placed more than once below the root composition, "
        "so it names no one instance; name the occurrence by `instance`."
    ),
    "sequence.step-instance-unknown": (
        "A sequence step whose `instance` path does not walk from the root composition to a member: a segment "
        "names no member, or descends into a component or an undeclared composition."
    ),
    "sequence.step-instance-not-a-component": (
        "A sequence step whose `instance` path ends on a member that instantiates a composition."
    ),
    # check_hardware_topology
    "hardware.layer-absent": "The mandatory `hardware/` layer is absent from the project.",
    "hardware.no-processor": "No `processor` element is declared; exactly one is required.",
    "hardware.multiple-processors": "More than one `processor` element is declared; exactly one is allowed.",
    "hardware.duplicate-name": "The same hardware element name declared in more than one file.",
    "hardware.undeclared-partition": "A task naming a partition no hardware element declares.",
    "hardware.partition-not-a-partition": "A task's `partition` naming a hardware element that is not a `partition`.",
    "hardware.partition-undeclared-core": "A partition naming a core no hardware element declares.",
    "hardware.partition-core-not-a-core": "A partition's `core` naming a hardware element that is not a `core`.",
    "hardware.undeclared-memory": "An SWC memory budget for a memory no hardware element declares.",
    "hardware.memory-not-a-memory": "An SWC memory budget keyed by a hardware element that is not a `memory`.",
    "hardware.accessible-undeclared-core": "A memory's `accessible` naming a core no hardware element declares.",
    "hardware.accessible-not-a-core": "A memory's `accessible` naming a hardware element that is not a `core`.",
    "hardware.unreachable-memory": (
        "A task whose partition's core is not permitted to use a memory its SWC gives a non-zero budget."
    ),
    # check_resource_budgets (drained from resource_analysis's finding codes)
    "hardware.missing-budget": "A consumer of a declared core or memory that declares no budget for it.",
    "hardware.threshold-breach": "A core or memory whose declared consumption exceeds its threshold.",
    "hardware.rate-monotonic-violation": "Two tasks on one core whose priorities invert their periods.",
    # check_runnable_costs
    "task.runnable-cost-exceeds-task": (
        "The declared budgets of a task's runnables sum to more than the task's own budget; judged only when the "
        "task declares a budget and at least one of its runnables does."
    ),
    # check_name_uniqueness
    "name.duplicate-swc": "The same SWC name declared in more than one file.",
    "name.swc-implementation-collision": "An SWC name colliding with another's generated `_Implementation`.",
    "name.duplicate-port": "One component declaring a port name twice.",
    "name.duplicate-runnable": "One component declaring a runnable name twice.",
    # A runnable and a generated event are siblings in one `SWC-INTERNAL-BEHAVIOR` ([TPS_GST_00018], [constr_2508]).
    "name.runnable-event-collision": (
        "A runnable whose name, compared case-insensitively, an event of its component is also written with — "
        "the event's stated `name`, or the name the writer derives where it states none."
    ),
    # The exact repeat inside a behaviour's `EVENTS` that `name.name-space-collision` leaves to a per-list rule.
    "name.duplicate-event": (
        "Two events of one component written with one SHORT-NAME — each event's stated `name`, or the name the "
        "writer derives where it states none — compared exactly."
    ),
    "name.duplicate-interface": "The same interface name declared in more than one file.",
    "name.duplicate-data-element": "One interface declaring a data element name twice.",
    "name.duplicate-operation": "One interface declaring an operation name twice.",
    "name.duplicate-argument": "One operation declaring an argument name twice.",
    # The `swc` and `compositions` layers share one `/ComponentTypes` `SHORT-NAME` namespace.
    "name.duplicate-component-type": "A composition name also declared as an SWC.",
    "name.duplicate-requirement": "Two requirement files declaring one `id`, which no link can name unambiguously.",
    "name.composition-implementation-collision": (
        "A composition name colliding with a component's generated `_Implementation`."
    ),
    # A component's generated `_Timing` is a third sibling in `/ComponentTypes`, reserved only where it is emitted.
    "name.swc-timing-collision": (
        "An SWC name colliding with the generated `_Timing` of a component one of whose runnables declares a `wcet`."
    ),
    "name.composition-timing-collision": (
        "A composition name colliding with the generated `_Timing` of a component one of whose runnables "
        "declares a `wcet`."
    ),
    # Minted by `check_name_uniqueness`: the generated `_Init` constant is a sibling of the interfaces.
    "interface.init-constant-name-collision": (
        "A generated `_Init` constant name claimed by two data elements, or by a data element and an interface."
    ),
    # Minted by `check_name_uniqueness`, not `check_runnable_access`: the generated access point names are siblings
    # under one runnable.
    "swc.access.name-collision": (
        "Two access entries of one runnable — a port's or a variable's — written with one access point name — each "
        "entry's stated `name`, or the name the writer derives where it states none — compared exactly."
    ),
    # check_identifier_conformance
    "identifier.declared": "A declared name that is not a legal AUTOSAR Identifier.",
    "identifier.generated": (
        "A legal declared name whose derived SHORT-NAME is not legal, where no name is stated in the derived one's "
        "place."
    ),
    # check_component_asil: `[constr_6201]`'s direction; only a component built below its allocated requirements is a
    # finding.
    "safety.component-below-requirements": "An SWC declaring an ASIL below the requirements allocated to it.",
    # check_safety_coexistence. The two `measure-*` rules report a declared argument nothing can read, `measure-blank`
    # one that says nothing.
    "safety.provider-below-element": "An SWC providing a data element rated above its own effective ASIL.",
    "safety.consumer-above-element": (
        "An SWC requiring a data element rated below its own effective ASIL, with no measure declared for it."
    ),
    "safety.measure-unknown-element": (
        "A measures key naming no data element of the port's own interface, or, on a port typed by an external "
        "interface stub, no name the stub lists in its `data_elements`."
    ),
    "safety.measure-on-provide-port": "A measures map on a provide port, where no rule reads it.",
    "safety.task-mixed-asil": (
        "A task whose runnables belong to components of two or more effective ASILs, with no coexistence argument."
    ),
    "safety.measure-blank": "A measures entry whose text is empty or whitespace only, which names no mechanism.",
    # check_hardware_capability. Declaration checks in the sense `hardware.unreachable-memory` is; a budget of `0`
    # declares no use.
    "safety.partition-on-unqualified-core": (
        "A rated partition on a core whose declared ASIL capability is below the partition's level, "
        "whether or not any task runs in it."
    ),
    "safety.memory-below-component": (
        "A component charging a non-zero budget to a memory whose declared ASIL capability is below "
        "the component's effective ASIL."
    ),
    # The three per-type rules of the same check.
    "safety.component-above-partition": (
        "A component run by a partition's tasks whose effective ASIL is above the level the partition "
        "declares; nothing closes it."
    ),
    "safety.component-below-partition": (
        "A component run by a partition's tasks rated below the level the partition declares, QM included, "
        "where the partition declares no coexistence argument."
    ),
    "safety.partition-memory-below-level": (
        "A rated partition whose tasks run a component charging a non-zero budget to a memory whose declared "
        "ASIL capability is below the partition's level, one finding per partition and memory. A budget belongs "
        "to the component type, so two instances of one type in different memories cannot be expressed."
    ),
    # The two trust rules of the same check. Declaration checks: the class is the project's statement, never verified.
    "safety.trusted-partition-below-processor-level": (
        "A trusted partition declaring a level below the highest level any partition of the processor declares: "
        "unrestricted for memory, it may write into that partition's memory."
    ),
    "safety.trusted-partition-unrated": (
        "A trusted partition declaring no level while the highest level any partition of the processor declares "
        "is above QM: unknown assurance, unrestricted for memory, beside an assured partition."
    ),
    # check_storage_portability: rules about files rather than what they declare.
    "storage.filename-case-collision": (
        "Two files of one layer directory whose names differ only in case, which a case-insensitive "
        "filesystem stores as one."
    ),
    # Also check_storage_portability.
    "storage.reserved-device-name": (
        "A file of a layer directory whose name before the first '.' is a device name Windows reserves "
        f"({', '.join(DEVICE_NAME_SPELLINGS)}, in any case), which no Windows filesystem can hold."
    ),
    # check_unknown_keys: read off the file's own document, because the loaded element no longer holds the key.
    "model.unknown-key": (
        "A key of an element file that no field of its kind declares, at any depth the contract declares a kind "
        "for; such a key is dropped when the model is read, and the value it was meant to carry is lost."
    ),
    # check_base_type_native_declarations. A finding rather than a load failure, so the read-only commands still read
    # the project; the writer refuses the same model.
    "data-type.base-type-native-declaration-missing": (
        "A base type that declares no native declaration, or a blank one."
    ),
    # check_mapping_set_references. Each id covers both an undeclared name and a name resolving to a data type of the
    # wrong kind.
    "mapping-set.unknown-application": (
        "A mapping set's `application` naming no declared application type, enumeration, record or array."
    ),
    "mapping-set.unknown-implementation": (
        "A mapping set's `implementation` naming no declared data type, or naming one that is neither a "
        "value type, a reference type, an implementation record, an implementation array nor a pointer."
    ),
    # The same check. A composite map judged as PARTIAL coverage of [TPS_SWCT_01191] (mapped types shall be compatible)
    # through a positional subset SWCraft chooses, and [constr_1066]'s forbidden mapping to a DATA_REFERENCE type, read
    # with R22-11's extension into sub-elements.
    "mapping-set.incompatible-composite-map": (
        "A map whose two sides differ in category, one side a record, an array, an implementation record or "
        "an implementation array (through reference types): a record pairs with an implementation record of "
        "as many elements, each member compatible with the element in the same position, and an array with "
        "an implementation array of the same length and a compatible element; at a scalar member an "
        "application type or enumeration pairs with a type ending at a value type. Not judged: member names, "
        "whether a scalar's range or conversion is representable, two members of the same shape swapped in "
        "position, and a member whose type does not resolve or is an external data type stub."
    ),
    "mapping-set.pointer-mapped": (
        "A map whose implementation side is a pointer, directly, at the end of a chain of reference types, "
        "or inside the implementation record or array it names: an application type is never mapped to a "
        "pointer."
    ),
    "swc.unknown-mapping-set": "A component's `data_type_mappings` naming no declared mapping set.",
    # A set lies in its layer's own package, so a set and a data type are not siblings.
    "mapping-set.duplicate-name": "The same mapping set name declared in more than one file.",
    # check_application_type_mapping (`[constr_1004]`): one finding per `(component, type)`.
    "data-type.application-type-unmapped": (
        "A component whose port, or whose variable, is typed by an application type or an enumeration that none of the "
        "mapping sets it names pairs with an implementation type."
    ),
    # check_application_type_double_mapping: `[constr_1004]`'s other half, one implementation type per application type
    # within one atomic component.
    "data-type.application-type-double-mapped": (
        "A component whose mapping sets pair one application type or enumeration with more than one "
        "implementation type between them."
    ),
    # check_name_space_collisions. `[constr_2508]`: a SHORT-NAME is unique ignoring case within the name space of its
    # nearest `Identifiable` (`[TPS_GST_00018]`), so the XML containers between are one name space.
    "name.name-space-collision": (
        "Two names in one AUTOSAR name space of the generated ARXML — a root package (for a data type, a generated "
        "form or a mapping set the package of its layer below `/DataTypes`) or a sub-package with the elements and "
        "generated forms placed in it and the names of its sub-packages, a component's ports and its behaviour, a "
        "behaviour's runnables, events, variables and exclusive areas, a runnable's access points, an interface's data "
        "elements or operations, an operation's arguments, a record's fields or elements, a composition's ports, "
        "members and connectors — that are equal ignoring case and either spelled differently or declared in two "
        "different lists."
    ),
    # check_termination_and_restart. `handleTerminationAndRestart` has multiplicity 1 at R4.2.2 and R4.3.0, the only
    # versions SWCraft writes.
    "swc.undeclared-termination-and-restart": (
        "A component whose behaviour is emitted and that sets no `handle_termination_and_restart`, which "
        "AUTOSAR 4.2.2 and 4.3.0 require on every behaviour."
    ),
    # Each predicate is what the ARXML writer's up-front guard asks; the two com-spec rules share
    # `check_port_com_specs`.
    "swc.port.com-spec-unknown-element": (
        "A port's `com_specs` entry naming no data element of the port's sender/receiver interface or, on a port "
        "typed by an external interface stub, no name the stub lists in its `data_elements`, or any entry on a "
        "port whose interface is client/server or undeclared."
    ),
    "swc.port.com-spec-inapplicable": (
        "A port's `com_specs` entry stating an attribute with no place on the port's side or the element's "
        "policy: a receiver-only attribute on a provide port, `queue_length` on an element that is not queued, "
        "a non-queued receiver attribute on a queued element, or an `init` on a queued element."
    ),
    # Minted by `port_com_spec_records`.
    "swc.port.com-spec-on-stub": (
        "A port's `com_specs` entry for a data element an external interface stub lists that states an `init`, "
        "which the stub holds no type to shape, or, on a require port, a `queue_length`, since the com-spec of a "
        "stub's member is never queued."
    ),
    "swc.port.queued-receiver-without-queue-length": (
        "A component's require port whose interface declares a queued data element and whose `com_specs` give "
        "that element no `queue_length`, which a queued receiver com-spec carries and nothing defaults."
    ),
    # Also judged on the data-received event's `disabled_modes`; the id keeps
    # its name, because a rename is a major contract change.
    "swc.timing_event.disabled-mode-port": (
        "An event's disabled mode whose `port` is not a require port of the component typed by an "
        "external interface stub with `external_dest` MODE-SWITCH-INTERFACE, or whose `mode_group_ref` does "
        "not lie directly under that stub's `external_ref`."
    ),
    "swc.access.queued-read-by-value": "A runnable reading a queued data element `by: value`.",
    # The inter-runnable variables: an access naming no variable of its own component (`[constr_2026]`), a variable
    # typed by a pointer (`[constr_1296]`), and the one warning, a read with no `init` (`[TPS_SWCT_01522]`).
    "swc.variable.undeclared-type": (
        "A component's variable whose `application_type` names no declared application type, or whose "
        "`implementation_type` names no declared implementation type (an external data type stub counts as declared)."
    ),
    "swc.variable-access.unknown-variable": "A runnable's variable access naming no variable of its own component.",
    "swc.variable.pointer-type": (
        "A component's variable typed by a pointer — named directly, at the end of a chain of reference types, or, for "
        "an application type, through the implementation type the component's mapping sets pair it with. A variable "
        "whose application type no set or more than one set pairs, or whose type is an external data type stub, is not "
        "judged."
    ),
    "swc.variable.read-without-init": (
        "A variable that a runnable of its component reads and that states no `init`: AUTOSAR leaves the value "
        "undefined when the read comes before every write, and the model states no order of execution, so no write can "
        "be shown to come first."
    ),
    # The InternalBehavior's `exclusiveArea` is local to the component, so there is no cross-component form.
    "swc.exclusive-area.unknown-area": (
        "A runnable's `can_enter` or `runs_inside` entry naming no exclusive area of its own component."
    ),
    # The data-received event: the port is one of the event's own component (`[constr_2530]`), it faces `require` (the
    # XSD's `CONTEXT-R-PORT-REF`), and the element is that interface's (the GST's `AtpInstanceRef` table). The queued
    # split of `[SWS_Rte_07403]` is not checked.
    "swc.data-event.unknown-port": "A data-received event naming a port its component does not declare.",
    "swc.data-event.direction-mismatch": "A data-received event through a provide port.",
    "swc.data-event.interface-kind-mismatch": (
        "A data-received event through a port whose interface is client/server, or an external interface stub "
        "whose `external_dest` is not SENDER-RECEIVER-INTERFACE."
    ),
    "swc.data-event.unknown-element": (
        "A data-received event naming no data element of the port's sender/receiver interface, or, through a port "
        "on an external interface stub, no name the stub lists in its `data_elements`."
    ),
    # The implicit read's two rules (`[constr_2020]`, `[constr_1103]`), the asynchronous call's rules (`[constr_2022]`,
    # `[constr_2023]`, `[constr_2006]` -- `async-without-result` judged at 4.2.2 only), the call-returned event's two
    # resolution rules and `[constr_1259]`. Every one is an error.
    "swc.access.queued-implicit-read": (
        "A runnable reading a queued data element `by: implicit`: an implicit read takes one value before the "
        "runnable starts, and a queued element has no one value to take."
    ),
    "swc.access.implicit-read-enable-update": (
        "A runnable reading `by: implicit` a data element its port's `com_specs` entry states `enable_update: "
        "true` for."
    ),
    "swc.call.mixed-synchrony": (
        "One operation of one port called both synchronously and asynchronously by the runnables of one component."
    ),
    "swc.call.timeout-mismatch": (
        "The asynchronous calls of one operation of one port, by the runnables of one component, stating "
        "different timeouts — an absent `timeout` counting as a value of its own."
    ),
    "swc.call.async-without-result": (
        "In a project generated at AUTOSAR 4.2.2, an asynchronous call that neither polls its result nor has a "
        "`call_returned` event naming its operation: 4.2.2 gives every asynchronous call point exactly one result"
        " point, where 4.3.0 allows none."
    ),
    "swc.call.several-results": (
        "An asynchronous call with two result points — its own poll and a `call_returned` event naming its "
        "operation, or two such events: a call point is referenced by at most one result point."
    ),
    "swc.call-returned-event.unknown-call": (
        "A `call_returned` event naming an operation of a port that no runnable of its component calls asynchronously."
    ),
    "swc.call-returned-event.ambiguous-call": (
        "A `call_returned` event naming an operation that two or more runnables of its component call "
        "asynchronously, so no one call point is the one its result point references."
    ),
    "swc.call.init-runnable-result": (
        "A runnable started by an `init` event that owns a result point — a call it polls or a `call_returned` "
        "event: a runnable an init event starts may call asynchronously but fetches no result."
    ),
    "composition.port-service-interface": (
        "A composition port typed by an interface that declares `is_service: true`."
    ),
    "data-type.conversion-without-unit": (
        "An application type declaring a `conversion` and neither a `unit` nor a `unit_ref`: the conversion is "
        "written as a LINEAR compu method, which must refer to a unit."
    ),
    # `runnable_symbol_records` mints both; the second is a `name.` rule as a uniqueness rule within one component.
    "swc.runnable.symbol-not-c-identifier": (
        "A runnable stating a `symbol` that is not a C identifier: a letter or an underscore first, then letters, "
        "digits or underscores only."
    ),
    "name.duplicate-runnable-symbol": (
        "Two runnables of one component written with one symbol — each runnable's `symbol`, or its name where it "
        "states none — compared exactly."
    ),
    "text.xml-unrepresentable-character": (
        "A free text the model holds -- a `description`, a composition's `title`, a base type's "
        "`native_declaration` or `encoding`, an application type's `unit`, a unit's `display_name` -- "
        "containing a character XML 1.0 cannot carry (a NUL, a control character other than tab, line feed and "
        "carriage return, a lone surrogate, U+FFFE or U+FFFF), which no written document can hold as the model "
        "holds it."
    ),
    # The data description elements, from one predicate, `data_description_element_records`.
    "unit.duplicate-name": "The same unit name declared in more than one file.",
    "compu-method.duplicate-name": "The same compu method name declared in more than one file.",
    "data-constr.duplicate-name": "The same data constraint name declared in more than one file.",
    "data-type.unknown-unit": "An application type whose `unit_ref` names no declared unit.",
    "data-type.unknown-compu-method": (
        "An application type, an enumeration, a value type or a reference type whose `compu_method_ref` names no "
        "declared compu method."
    ),
    "data-type.unknown-data-constr": "An application type whose `data_constr_ref` names no declared data constraint.",
    "compu-method.unknown-unit": "A linear compu method whose `unit` names no declared unit.",
    "data-type.compu-method-wrong-category": (
        "A `compu_method_ref` naming a compu method of a kind its type cannot take: a texttable from an application "
        "type, or a linear from an enumeration, a value type or a reference type (an implementation type's compu "
        "method is restricted to BITFIELD_TEXTTABLE or TEXTTABLE, [constr_1158]; SWCraft holds a texttable)."
    ),
    "data-type.compu-method-on-unresolved-reference-type": (
        "A reference type stating a `compu_method_ref` whose reference chain, resolved, does not end in a value "
        "type ([constr_1383]); a chain ending at an external data type stub is not judged."
    ),
    "compu-method.table-entry-repeated": (
        "A texttable compu method declaring one entry name, or one entry value, twice."
    ),
}


# ---------------------------------------------------------------------------
# EXTERNAL_INTERFACE_DESTS
# ---------------------------------------------------------------------------

#: The ``DEST`` values an ``external_interface`` stub may carry: the eight members of ``PORT-INTERFACE--SUBTYPES-ENUM``,
#: which ``AUTOSAR_4-2-2.xsd`` and ``AUTOSAR_4-3-0.xsd`` enumerate identically for a port's ``PROVIDED-INTERFACE-TREF``
#: / ``REQUIRED-INTERFACE-TREF``. Compared by membership, never by suffix, so ``FOREIGN-SENDER-RECEIVER-INTERFACE`` is
#: refused.
EXTERNAL_INTERFACE_DESTS: frozenset[str] = frozenset(
    {
        "CLIENT-SERVER-INTERFACE",
        "DATA-INTERFACE",
        "MODE-SWITCH-INTERFACE",
        "NV-DATA-INTERFACE",
        "PARAMETER-INTERFACE",
        "PORT-INTERFACE",
        "SENDER-RECEIVER-INTERFACE",
        "TRIGGER-INTERFACE",
    }
)
