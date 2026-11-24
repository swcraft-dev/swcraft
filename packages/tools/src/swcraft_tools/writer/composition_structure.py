"""The composition hierarchy's structure, and the two structural refusals over it.

``compositions_by_name`` maps a declared composition name to its composition, first
declaration winning; ``root_composition`` resolves the one composition that
declares ``root: true``; ``member_type_ref`` names what a composition member
instantiates, as the ``(field, name)`` pair; ``check_composition_hierarchy``
refuses a member graph that is not acyclic; and ``check_external_stubs``
refuses the external-stub shapes ``check-model`` reports. The module depends
only on ``schema`` and ``validator``.
"""

from __future__ import annotations

from swcraft_tools.schema import (
    Composition,
    CompositionMember,
    DataType,
    ExternalDataType,
    ExternalInterface,
    Interface,
    SWComponent,
)
from swcraft_tools.validator import EXTERNAL_DATA_TYPE_DESTS, EXTERNAL_INTERFACE_DESTS
from swcraft_tools.validator.compositions import DEST_TOKEN
from swcraft_tools.validator.data_types import REF_PATH, external_pair_problems, own_package_problem


def compositions_by_name(compositions: list[Composition]) -> dict[str, Composition]:
    """Map declared composition name -> composition, first declaration winning.

    A duplicate name across two files is ``check_composition_consistency``'s to report.
    """
    index: dict[str, Composition] = {}
    for composition in compositions:
        index.setdefault(composition.name, composition)
    return index


def check_composition_hierarchy(composition_index: dict[str, Composition]) -> None:
    """Refuse a ``compositions/`` layer whose member graph is not acyclic.

    A member whose reference does not resolve contributes no edge. **What this does not reach**: a composition that is
    *unreachable* from the root. That is legal and is emitted.

    Args:
        composition_index: the deduplicated ``compositions`` layer by name.

    Raises:
        ValueError: naming the cycle, in the order it was walked.
            ``check-model`` reports the same as ``rule:composition.cycle``.
    """
    colour: dict[str, str] = {}

    def walk(start: str, path: list[str]) -> None:
        colour[start] = "gray"
        for member in composition_index[start].members:
            neighbour = member.composition
            if neighbour is None or neighbour not in composition_index:
                continue
            if colour.get(neighbour) == "gray":
                cycle = path[path.index(neighbour) :] + [neighbour]
                raise ValueError(
                    f"Composition cycle in compositions/: {' -> '.join(cycle)}; "
                    "a composition cannot contain itself, directly or through another composition."
                )
            if colour.get(neighbour, "white") == "white":
                path.append(neighbour)
                walk(neighbour, path)
                path.pop()
        colour[start] = "black"

    for name in sorted(composition_index):
        if colour.get(name, "white") == "white":
            walk(name, [name])


def check_external_stubs(
    swcs: list[SWComponent],
    interfaces: list[Interface],
    data_types: list[DataType],
) -> None:
    """Refuse the external-stub shapes ``check-model`` reports.

    The shapes, judged by the validator's own predicates:

    * a component stub declaring content nothing emits: ``runnables``, ``variables``, ``exclusive_areas``, ``memory``,
      ``data_type_mappings``, ``handle_termination_and_restart``, ``package``, a stated ``behavior_name``, or a port
      carrying an ``interface``, ``measures`` or ``com_specs`` (a port by name and direction alone is legal);
    * a stub whose ``external_ref``/``external_dest`` pair is blank, not reference-shaped, or inside a root package
      this writer declares the project's own elements in;
    * a component that is **not** a stub carrying either half of the pair (it would be dropped);
    * an interface or data type stub whose pair is malformed or whose ``DEST`` is outside the set its layer admits
      (``validator.EXTERNAL_INTERFACE_DESTS``, ``validator.EXTERNAL_DATA_TYPE_DESTS``), even when nothing names it.

    Raises:
        ValueError: naming every offending stub, one sentence per layer. ``check-model`` reports the same
            shapes as ``rule:swc.external-with-ports``, ``rule:swc.external-incomplete-reference``,
            ``rule:swc.external-reference-inside-generated-package``, ``rule:swc.external-reference-without-stub``,
            ``rule:interface.external-incomplete-reference`` and ``rule:data-type.external-incomplete-reference``.
    """

    offenders: list[str] = []
    for swc in swcs:
        if not swc.external:
            stray = [
                name
                for name, value in (("external_ref", swc.external_ref), ("external_dest", swc.external_dest))
                if value.strip()
            ]
            if stray:
                offenders.append(
                    f"component '{swc.name}' declares {' and '.join(stray)} without 'external: true', "
                    "and the reference pair is only emitted for an external stub"
                )
            continue
        # A stub's port by name and direction is what a connector resolves by; an
        # `interface`, `measures` or `com_specs` on it declares content nothing emits.
        # The condition restates the validator's own.
        offending_ports = [port for port in swc.ports if port.interface is not None or port.measures or port.com_specs]
        declared = [
            name
            for name, members in (
                ("ports", offending_ports),
                ("runnables", swc.runnables),
                ("variables", swc.variables),
                ("exclusive_areas", swc.exclusive_areas),
                ("memory", swc.memory),
                ("data_type_mappings", swc.data_type_mappings),
                ("handle_termination_and_restart", swc.handle_termination_and_restart),
                ("package", swc.package),
                ("behavior_name", swc.behavior_name),
            )
            if members
        ]
        if declared:
            port_detail = "; ".join(
                f"port '{port.name}' declares "
                + " and ".join(
                    what
                    for what, present in (
                        ("an interface", port.interface is not None),
                        ("measures", port.measures),
                        ("com_specs", port.com_specs),
                    )
                    if present
                )
                for port in offending_ports
            )
            offenders.append(
                f"external stub '{swc.name}' declares {' and '.join(declared)}, and an external stub "
                "emits no component type, so nothing it declares would be emitted"
                + (
                    f" (a stub's port is a name and a direction, and its type is outside the project: {port_detail})"
                    if offending_ports
                    else ""
                )
            )
        missing = [
            name
            for name, value in (("external_ref", swc.external_ref), ("external_dest", swc.external_dest))
            if not value.strip()
        ]
        if missing:
            offenders.append(
                f"external stub '{swc.name}' leaves {' and '.join(missing)} blank, and a stub is emitted "
                "as its source's TYPE-TREF and DEST verbatim"
            )
            continue
        if not REF_PATH.fullmatch(swc.external_ref):
            offenders.append(
                f"external stub '{swc.name}' declares external_ref '{swc.external_ref}', which is not an "
                "AUTOSAR reference path"
            )
        elif (inside := own_package_problem(swc.external_ref)) is not None:
            offenders.append(f"external stub '{swc.name}' {inside}")
        if not DEST_TOKEN.fullmatch(swc.external_dest):
            offenders.append(
                f"external stub '{swc.name}' declares external_dest '{swc.external_dest}', which is not an "
                "AUTOSAR reference DEST"
            )
    sentences: list[str] = []
    if offenders:
        sentences.append("Invalid external stub declaration(s) in swc/: " + "; ".join(offenders) + ".")
    layer_stubs = (
        (
            "interfaces/",
            "external interface stub",
            [iface for iface in interfaces if isinstance(iface, ExternalInterface)],
            EXTERNAL_INTERFACE_DESTS,
            "a port interface DEST",
        ),
        (
            "data_types/implementation/",
            "external type stub",
            [dt for dt in data_types if isinstance(dt, ExternalDataType)],
            EXTERNAL_DATA_TYPE_DESTS,
            "an implementation data type DEST",
        ),
    )
    for layer, noun, stubs, admitted, what in layer_stubs:
        problems = [
            f"{noun} '{stub.name}' {problem}"
            for stub in stubs
            for problem, _fields in external_pair_problems(stub.external_ref, stub.external_dest, admitted, what)
        ]
        if problems:
            sentences.append(f"Invalid external stub declaration(s) in {layer}: " + "; ".join(problems) + ".")
    if sentences:
        raise ValueError(" ".join(sentences))


def root_composition(compositions: list[Composition]) -> Composition:
    """Return the one composition declaring ``root: true``, or raise.

    A connector naming no composition belongs to the root. The root is resolved here, where
    the model is in hand, never as a schema default: its name is a property of the model.

    Raises:
        ValueError: if no composition declares ``root: true`` or more than one
            does. ``check-model`` reports the same two shapes as
            ``rule:composition.no-root`` and ``rule:composition.multiple-roots``.
    """
    roots = [composition for composition in compositions if composition.root]
    if not roots:
        raise ValueError(
            "No composition in compositions/ declares 'root: true'; "
            "exactly one must, because it is the top of the emitted hierarchy and "
            "the composition a connector that names none belongs to."
        )
    if len(roots) > 1:
        raise ValueError(
            f"Composition(s) {sorted(composition.name for composition in roots)} in "
            f"compositions/ all declare 'root: true'; exactly one may."
        )
    return roots[0]


def member_type_ref(member: CompositionMember) -> tuple[str, str]:
    """Return ``(field, type name)`` of what *member* instantiates (``type`` or ``composition``, exactly one set).

    The field is carried because the name alone is ambiguous: an ``external: true`` component of the same name emits
    no element, so resolving it in ``swc`` first would emit the stub's foreign ``TYPE-TREF`` for a composition member.
    """
    if member.type is not None:
        return "type", member.type
    return "composition", str(member.composition)
