"""Forward dependency closure for component-scoped ARXML exchange.

Given a ``model_root`` and one SWC name, :func:`compute_closure` resolves the **component-owned**
content set: the SWC, the interfaces its ports reference, the transitive data-type closure over
those interfaces, the mapping sets the SWC's ``data_type_mappings`` names (each narrowed to the
maps this closure reaches), and every unit, compu method and data constraint a data type of the
closure names, plus the unit a named ``linear`` names. A dangling reference is an error, never a
silent drop.

Composition membership, connectors, task allocation, sequences and requirements are
integrator-owned and are **never read or included**. Duplicate names are checked only within the
closure's reference set, so a duplicate elsewhere does not block an unrelated SWC's export.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swcraft_tools.data_description import DataDescription, read_data_description
from swcraft_tools.reader import read_interfaces, read_mapping_sets, read_swc_model
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    CSArgument,
    DataDescriptionElement,
    DataElement,
    DataType,
    EnumDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    Linear,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    SWComponent,
    ValueDataType,
    Variable,
)
from swcraft_tools.validator import data_type_layer


class ComponentExchangeError(Exception):
    """Base class for every typed component-exchange error."""


# `ComponentNotFoundError` and `AmbiguousComponentError` are defined at the end of this module.


class AmbiguousReferenceError(ComponentExchangeError):
    """A name the closure references is declared by two or more YAML files.

    Exporting a silently picked one of them would be non-deterministic.
    """


class MissingReferenceError(ComponentExchangeError):
    """The closure references a name declared nowhere in ``model_root``; see :attr:`missing`."""

    def __init__(self, message: str, missing: set[str] | frozenset[str]) -> None:
        super().__init__(message)
        self.missing: frozenset[str] = frozenset(missing)


@dataclass
class ComponentClosure:
    """The component-owned content set for one SWC.

    Carries **no** connector, task, sequence or requirement content. ``interfaces``, ``data_types``
    and ``mapping_sets`` are sorted by name; ``elements`` (units, compu methods, data constraints)
    by name and then layer. ``mapping_sets`` are narrowed: see :func:`compute_closure`.
    """

    swc: SWComponent
    interfaces: list[Interface] = field(default_factory=list)
    data_types: list[DataType] = field(default_factory=list)
    mapping_sets: list[MappingSet] = field(default_factory=list)
    elements: list[DataDescriptionElement] = field(default_factory=list)


def _prototype_type_key(prototype: DataElement | CSArgument | Variable) -> tuple[str, str]:
    """``(layer, name)`` of the type a data element, an argument or a variable states.

    Raises:
        RuntimeError: *prototype* states neither type, which only a schema bypass builds.
    """
    if prototype.application_type is not None:
        return (_APPLICATION_TYPES, prototype.application_type)
    if prototype.implementation_type is None:
        raise RuntimeError(f"'{prototype.name}' states no type: the schema requires exactly one of the two")
    return (_IMPLEMENTATION_TYPES, prototype.implementation_type)


def _interface_type_refs(iface: Interface) -> set[tuple[str, str]]:
    """``(layer, name)`` of every data type an interface references directly.

    An external stub declares no member, so it references none; ``export_component`` refuses a
    closure that reaches one.
    """
    if isinstance(iface, SRInterface):
        return {_prototype_type_key(de) for de in iface.data_elements}
    if isinstance(iface, ExternalInterface):
        return set()
    return {_prototype_type_key(arg) for op in iface.operations for arg in op.arguments}


def _data_type_refs(dt: DataType) -> set[str]:
    """Data-type names one data type references directly.

    Both ``base`` edges are followed, so a reference chain is collected down to its base type or the
    implementation composite it aliases; a pointer cycle ends where the walk finds a name it holds.
    The same edges as ``validator.data_type_references``, spelled here on purpose: the field oracle
    witnesses a read only in the ``component-exchange`` consumer's own modules.
    """
    if isinstance(dt, (ValueDataType, ReferenceDataType)):
        return {dt.base}
    if isinstance(dt, RecordDataType):
        return {f.type for f in dt.fields}
    if isinstance(dt, ImplementationRecordDataType):
        return {e.type for e in dt.elements}
    if isinstance(dt, (ArrayDataType, ImplementationArrayDataType)):
        return {dt.element_type}
    if isinstance(dt, PointerDataType):
        return {dt.target}
    return set()


@dataclass
class _TypeWalk:
    """The data types a closure reaches, by ``(layer, name)``: those the walk requires and those it finds undeclared."""

    dt_groups: dict[tuple[str, str], list[DataType]]
    required: set[tuple[str, str]] = field(default_factory=set)
    missing_types: set[tuple[str, str]] = field(default_factory=set)

    def visit(self, key: tuple[str, str]) -> None:
        """The walk recording ``key`` as required, or missing when undeclared, then the types it references."""
        if key in self.required or key in self.missing_types:
            return
        group = self.dt_groups.get(key)
        if not group:
            self.missing_types.add(key)
            return
        self.required.add(key)
        target_layer = (
            _BASE_TYPES
            if isinstance(group[0], ValueDataType)
            else _APPLICATION_TYPES
            if key[0] == _APPLICATION_TYPES
            else _IMPLEMENTATION_TYPES
        )
        for ref in _data_type_refs(group[0]):
            self.visit((target_layer, ref))


@dataclass
class _ElementWalk:
    """The units, compu methods and data constraints a closure carries, and those it finds undeclared."""

    element_groups: dict[tuple[str, str], list[DataDescriptionElement]]
    carried: set[tuple[str, str]] = field(default_factory=set)
    missing_elements: set[tuple[str, str]] = field(default_factory=set)

    def visit(self, key: tuple[str, str]) -> None:
        """The walk recording ``key`` as carried, or missing when undeclared, then the elements it references."""
        if key in self.carried or key in self.missing_elements:
            return
        group = self.element_groups.get(key)
        if not group:
            self.missing_elements.add(key)
            return
        self.carried.add(key)
        for named in sorted(_element_refs(group[0])):
            self.visit(named)


def _target_swc(model_root: str, swc_name: str) -> SWComponent:
    """The one component declaring *swc_name*; none or several raise."""
    swc_matches = [s for s in read_swc_model(model_root) if s.name == swc_name]
    if not swc_matches:
        raise ComponentNotFoundError(swc_name, model_root)
    if len(swc_matches) > 1:
        raise AmbiguousComponentError(swc_name, model_root, count=len(swc_matches))
    swc = swc_matches[0]
    return swc


def _port_interfaces(model_root: str, swc: SWComponent, swc_name: str) -> list[Interface]:
    """The interfaces the component's ports name, in name order; an ambiguous or undeclared one raises."""
    iface_groups: dict[str, list[Interface]] = {}
    for iface in read_interfaces(model_root):
        iface_groups.setdefault(iface.name, []).append(iface)

    # A port without an interface carries nothing (the export refuses it later), and a `None`
    # here would make the sort and the join below raise.
    port_iface_names = sorted({p.interface for p in swc.ports if p.interface is not None})
    ambiguous_ifaces = [n for n in port_iface_names if len(iface_groups.get(n, [])) > 1]
    if ambiguous_ifaces:
        raise AmbiguousReferenceError(
            f"interface name(s) referenced by SWC '{swc_name}' are declared by multiple "
            f"YAML files: {', '.join(ambiguous_ifaces)}"
        )
    missing_ifaces = {n for n in port_iface_names if n not in iface_groups}
    if missing_ifaces:
        raise MissingReferenceError(
            f"SWC '{swc_name}' references undeclared interface(s): {', '.join(sorted(missing_ifaces))}",
            missing_ifaces,
        )
    interfaces: list[Interface] = [iface_groups[n][0] for n in port_iface_names]
    return interfaces


def _walk_data_types(
    model_root: str, swc: SWComponent, interfaces: list[Interface]
) -> tuple[_TypeWalk, DataDescription]:
    """The data types the interfaces and the variables reach, transitively, and the description they were read from."""
    # Keyed by `(layer, name)`: a name may exist in several layers.
    description = read_data_description(model_root)
    dt_groups: dict[tuple[str, str], list[DataType]] = {}
    for dt in description.data_types:
        dt_groups.setdefault((data_type_layer(dt), dt.name), []).append(dt)

    types = _TypeWalk(dt_groups)

    for iface in interfaces:
        for key in sorted(_interface_type_refs(iface), key=lambda key: (key[1], key[0])):
            types.visit(key)
    # Before the narrowing snapshot, so a variable's application type keeps its map.
    for variable in swc.variables:
        types.visit(_prototype_type_key(variable))
    return types, description


def _narrowed_mapping_sets(model_root: str, swc: SWComponent, swc_name: str, types: _TypeWalk) -> list[MappingSet]:
    """The sets the component names, each narrowed to the maps whose application side the walk reached.

    The implementation side of every kept map joins the walk. An ambiguous or undeclared set raises.
    """
    # A snapshot taken before any implementation side is walked: the loop below mutates
    # `types.required`, and a store pulled in for one map must never make another map reachable.
    reached = frozenset(name for layer, name in types.required if layer == _APPLICATION_TYPES)
    set_groups: dict[str, list[MappingSet]] = {}
    for mapping_set in read_mapping_sets(model_root):
        set_groups.setdefault(mapping_set.name, []).append(mapping_set)

    named_set_names = sorted(set(swc.data_type_mappings))
    ambiguous_sets = [n for n in named_set_names if len(set_groups.get(n, [])) > 1]
    if ambiguous_sets:
        raise AmbiguousReferenceError(
            f"mapping set name(s) referenced by SWC '{swc_name}' are declared by multiple "
            f"YAML files: {', '.join(ambiguous_sets)}"
        )
    missing_sets = {n for n in named_set_names if n not in set_groups}
    if missing_sets:
        raise MissingReferenceError(
            f"SWC '{swc_name}' references undeclared mapping set(s): {', '.join(sorted(missing_sets))}",
            missing_sets,
        )

    mapping_sets: list[MappingSet] = []
    for name in named_set_names:
        declared = set_groups[name][0]
        kept = [entry for entry in declared.maps if entry.application in reached]
        mapping_sets.append(declared.model_copy(update={"maps": kept}))
        for entry in kept:
            types.visit((_IMPLEMENTATION_TYPES, entry.implementation))
    return mapping_sets


def _settled_data_types(swc_name: str, types: _TypeWalk) -> list[DataType]:
    """The reached data types in name order, once none is ambiguous or undeclared."""
    # Ambiguity first: the walk followed the first definition of each name, which is only THE
    # definition once no name is declared twice.
    ambiguous_types = sorted(
        {
            name
            for layer, name in types.required | types.missing_types
            if len(types.dt_groups.get((layer, name), [])) > 1
        }
    )
    if ambiguous_types:
        raise AmbiguousReferenceError(
            f"data type name(s) in SWC '{swc_name}''s closure are declared by multiple "
            f"YAML files: {', '.join(ambiguous_types)}"
        )

    if types.missing_types:
        missing_names = {name for _, name in types.missing_types}
        raise MissingReferenceError(
            f"SWC '{swc_name}''s closure references undeclared data type(s): {', '.join(sorted(missing_names))}",
            missing_names,
        )

    data_types: list[DataType] = [
        types.dt_groups[key][0] for key in sorted(types.required, key=lambda key: (key[1], key[0]))
    ]
    return data_types


def _carried_elements(
    description: DataDescription, data_types: list[DataType], swc_name: str
) -> list[DataDescriptionElement]:
    """The units, compu methods and data constraints the data types name, in name order; a clash or gap raises."""
    # After the data types are settled, so each element comes from THE definition of each type.
    element_groups: dict[tuple[str, str], list[DataDescriptionElement]] = {}
    for element in description.elements:
        element_groups.setdefault((data_type_layer(element), element.name), []).append(element)
    walk = _ElementWalk(element_groups)

    for dt in data_types:
        for named in sorted(_element_refs(dt)):
            walk.visit(named)
    ambiguous_elements = sorted(
        {name for layer, name in walk.carried | walk.missing_elements if len(element_groups.get((layer, name), [])) > 1}
    )
    if ambiguous_elements:
        raise AmbiguousReferenceError(
            f"unit, compu method or data constraint name(s) in SWC '{swc_name}''s closure are declared by "
            f"multiple YAML files: {', '.join(ambiguous_elements)}"
        )
    if walk.missing_elements:
        missing_element_names = {name for _, name in walk.missing_elements}
        raise MissingReferenceError(
            f"SWC '{swc_name}''s closure references undeclared unit, compu method or data constraint element(s): "
            f"{', '.join(sorted(missing_element_names))}",
            missing_element_names,
        )
    elements = [element_groups[key][0] for key in sorted(walk.carried, key=lambda key: (key[1], key[0]))]
    return elements


def compute_closure(model_root: str, swc_name: str) -> ComponentClosure:
    """The forward dependency closure of one SWC.

    Returns:
        A :class:`ComponentClosure`, each list sorted by name.

    Raises:
        ComponentNotFoundError: no SWC declares ``swc_name``.
        AmbiguousComponentError: 2+ SWC files declare ``swc_name``.
        AmbiguousReferenceError: 2+ files declare a name this closure references.
        MissingReferenceError: the closure references an undeclared name.

    Each mapping set the SWC names is narrowed to the maps whose application side this closure
    reaches; the implementation side of a kept map joins the walk. Units, compu methods and data
    constraints a reached type names are carried once each. **Does not reach:** the exported set is a
    subset of a wider declared one, so a re-import compares a subset; a set the component does not
    name; whether a compu method's category fits its referrer.
    """
    swc = _target_swc(model_root, swc_name)
    interfaces = _port_interfaces(model_root, swc, swc_name)
    types, description = _walk_data_types(model_root, swc, interfaces)
    mapping_sets = _narrowed_mapping_sets(model_root, swc, swc_name, types)
    data_types = _settled_data_types(swc_name, types)
    elements = _carried_elements(description, data_types, swc_name)
    return ComponentClosure(
        swc=swc, interfaces=interfaces, data_types=data_types, mapping_sets=mapping_sets, elements=elements
    )


class ComponentNotFoundError(ComponentExchangeError):
    """No SWC YAML file in ``model_root`` declares the requested name.

    The MCP host already knows its project, so ``str()`` says "in the model"; the CLI passes
    ``full_path=True`` to :meth:`render`, naming the model root.
    """

    def __init__(self, swc_name: str, model_root: str) -> None:
        self.swc_name, self.model_root = swc_name, model_root
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal, "in the model", or naming the model root for a local terminal."""
        return f"SWC '{self.swc_name}' not found in {self.model_root if full_path else 'the model'}"


class AmbiguousComponentError(ComponentExchangeError):
    """Two or more SWC YAML files declare the same ``name`` field.

    Rendered as :class:`ComponentNotFoundError` is, and for the same reason.
    """

    def __init__(self, swc_name: str, model_root: str, *, count: int) -> None:
        self.swc_name, self.model_root, self.count = swc_name, model_root, count
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal, "in the model", or naming the model root for a local terminal."""
        where = self.model_root if full_path else "the model"
        return (
            f"SWC name '{self.swc_name}' is declared by {self.count} YAML files in "
            f"{where} — resolve the duplicate before exporting"
        )


#: The three data description layers the closure walks.
_IMPLEMENTATION_TYPES = "implementation_types"
_BASE_TYPES = "base_types"
_APPLICATION_TYPES = "application_types"


def _element_refs(element: DataType | DataDescriptionElement) -> set[tuple[str, str]]:
    """``(layer, name)`` of every unit, compu method and data constraint *element* names directly.

    The same edges as ``validator.data_description_references``, spelled here on purpose for the
    same reason as :func:`_data_type_refs`.
    """
    named: tuple[tuple[str, str | None], ...]
    if isinstance(element, ApplicationDataType):
        named = (
            ("units", element.unit_ref),
            ("compu_methods", element.compu_method_ref),
            ("data_constraints", element.data_constr_ref),
        )
    elif isinstance(element, (EnumDataType, ValueDataType, ReferenceDataType)):
        named = (("compu_methods", element.compu_method_ref),)
    elif isinstance(element, Linear):
        named = (("units", element.unit),)
    else:
        named = ()
    return {(layer, name) for layer, name in named if name is not None}
