"""ARXML path -> SWCraft emitted-name registry.

Implements the type-name normalization policy: app/impl twin collapse (rule 1),
deterministic ``_app`` (/``_app_{package}``/``_{k}_app``) suffix renaming on a
bare-name collision (rule 2), and ``NameCollisionError`` for a duplicate path
(rule 3). One SHORT-NAME may recur in any number of AUTOSAR packages, so a
bare-name collision is routine and always has a resolution.

**Names are reserved per layer**: an application type, an implementation type
and a base type are elements of three layers, each with its own name space, so
one ``SHORT-NAME`` declared in two of them is two elements under one name and no
rename. Rule 2's ``_app`` suffix answers a bare-name collision inside one layer
only.

A base of the package's dependency graph: it imports no other ``arxml_import``
submodule, which is why :func:`fit_identifier`, the length cap every
name-minting site shares, lives here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple

from swcraft_tools.arxml_reader import com_spec_block
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH

if TYPE_CHECKING:
    from swcraft_tools.arxml_reader import (
        DataElementComSpecInfo,
        PortComSpecInitInfo,
        SkipCategory,
        SkipEntry,
        SkipLog,
        SkipReason,
        StandardRule,
        SwcPortInfo,
    )
    from swcraft_tools.schema import InitValue

#: The token every name this package mints to resolve a collision carries, always
#: as the final token (``_reserve()``'s bare-name renames and ``reconcile.py``'s
#: reserved-name renames). Lowercase, while every suffix the writer reserves
#: carries a capital, so nothing minted here is a name the writer reserves.
RENAME_SUFFIX = "_app"

#: The implementation-type layer (``merge.LAYER_IMPLEMENTATION_TYPES``'s value,
#: spelled here because this module imports no sibling): the layer of the two
#: registrations no caller states one for, an external type stub and the
#: collapsed application path of a bare same-name pair.
_IMPLEMENTATION_TYPES_LAYER = "implementation_types"


class NameCollisionError(ValueError):
    """Raised when the type-name normalization policy cannot resolve a collision (rule 3).

    That is a path re-registered under an already-claimed identical full path --
    a malformed export, reported as an import error. A bare-name collision between
    distinct paths is always resolved (:meth:`PathRegistry._collision_candidates`).
    ``arxml_import.reconcile`` raises it too, for its own unresolvable cases (the
    rename fixed point not settling, a rename leaving two connectors sharing a name).
    """


def snake_case(path: str) -> str:
    """Snake-case the immediate parent package segment of an AUTOSAR path.

    ``/Acme/Chassis/ApplicationTypes/DoorStateEnum`` -> ``application_types``, for
    rule 2's fallback suffix ``{bare_name}_app_{snake_cased_parent}``.

    The result is sanitized to ``[a-z0-9_]``: no layer checks a package's
    ``SHORT-NAME``, and this is the one place a package's characters reach an
    emitted name (``/Acme/My Pkg-2/Foo`` would otherwise mint a name holding a space).
    """
    parts = path.strip("/").split("/")
    parent = parts[-2] if len(parts) >= 2 else parts[-1]
    return re.sub(r"[^a-z0-9_]", "_", re.sub(r"(?<!^)(?=[A-Z])", "_", parent).lower())


def fit_identifier(stem: str, suffix: str = "") -> str:
    """``f"{stem}{suffix}"``, shortened to the AUTOSAR ``Identifier`` limit.

    The stem gives up characters; the suffix is shortened only when it does not fit
    the whole budget on its own, so a minted name keeps reading as "``stem``,
    disambiguated by ``suffix``". A counter goes into the suffix
    (``f"_{k}{RENAME_SUFFIX}"``), so the rename token stays the final one.

    Every name this package mints routes through here
    (:meth:`PathRegistry._collision_candidates`, ``reconcile.free_name()``,
    :func:`stub_name`, and ``reconcile._free_package``'s sub-package name). What this
    does not reach: a declared ``SHORT-NAME`` is never capped on its own (a cap would
    hide the offender; :func:`stub_name` keeps an over-length leaf uncapped), so an
    over-length one reaches ``reconcile.identifier_violations()`` and ``run_import()``
    refuses it (an ``IDENTIFIER--SIMPLE`` at source never exceeds the limit);
    ``component_exchange/import_.py``'s guards mint nothing; and ``import component``
    refuses any rename this registry reports.

    Args:
        stem: the part that gives up characters first.
        suffix: the part kept whole whenever it fits inside the budget.

    Returns:
        A string of at most :data:`~swcraft_tools.identifier.MAX_IDENTIFIER_LENGTH`
        characters. A suffix longer than the whole budget (reachable through
        ``_app_{snake_case(package)}``) truncates both parts: the stem to at most half
        the budget, the suffix to the rest, so the result still begins with the stem's
        leading characters. Two different suffixes are not promised two different
        results, since a truncated stem can absorb the difference; distinctness is the
        caller's, checked against the claimed set.
    """
    budget = MAX_IDENTIFIER_LENGTH - len(suffix)
    if budget < 1:
        head = min(len(stem), MAX_IDENTIFIER_LENGTH // 2)
        return f"{stem[:head]}{suffix}"[:MAX_IDENTIFIER_LENGTH]
    return f"{stem[:budget]}{suffix}"


def stub_name(leaf: str, rank: int, claimed: Collection[str]) -> tuple[str, int]:
    """The external stub name ``<leaf>_ext<X>`` and its X: the first free X from *rank* up.

    *rank* is the stub's index among the paths sharing its leaf, by sorted path
    (:func:`stub_names`). A leaf over the ``Identifier`` limit keeps it uncapped,
    ``leaf + "_ext<X>"``, so ``reconcile.identifier_violations()`` refuses the
    over-length source name; any other leaf goes through :func:`fit_identifier`,
    which shortens the stem only. Two leaves sharing a shortened stem mint one name,
    so a candidate already *claimed* moves X past it, and X is then a free rank
    rather than the namesake index.
    """

    def candidate(x: int) -> str:
        suffix = f"_ext{x}"
        return f"{leaf}{suffix}" if len(leaf) > MAX_IDENTIFIER_LENGTH else fit_identifier(leaf, suffix)

    x = rank
    while candidate(x) in claimed:
        x += 1
    return candidate(x), x


@dataclass(frozen=True)
class InitRadixNormalization:
    """One radix-normalization report line, and the interface whose commitment made it true.

    One record rather than two parallel lists, so a line cannot outlive its
    interface: the report never carries a line about an element in no written file.
    `element_path` is the data element's ARXML path, for
    ``orchestrator.without_inits_through_a_stub``, which drops an ``init`` and
    keeps its interface.
    """

    interface: str
    element_path: str
    line: str


def init_radix_line(subject: str, spellings: list[str] | tuple[str, ...]) -> str:
    """One radix line: *subject*'s init value was read from *spellings* and is written in decimal.

    *subject* is a data element's path or, for a port's own value, ``{port}/{block}``.
    """
    return f"{subject}: init read from {', '.join(spellings)}, written decimal"


class PortInitKey(NamedTuple):
    """The key of :attr:`PathRegistry.port_inits`: one com-spec of one port.

    The block is the one the com-spec sits in (``arxml_reader.com_spec_block``); the
    reference is the raw ``DATA-ELEMENT-REF`` text (``""`` when absent); *position*
    is the com-spec's index among every child of its block, and *port_position* the
    port's index among every child of its owner's ``PORTS``.

    The positions bind an outcome to one com-spec of one port: two com-specs of one
    kind for one element in one block, or two ports of one owner sharing a
    ``SHORT-NAME`` (a ``PR-PORT-PROTOTYPE`` among them), never share a key, so the
    component mapper finds the outcome of the com-spec it passed and no other.
    """

    port_path: str
    block: str
    com_spec_tag: str
    data_element_ref: str
    position: int
    port_position: int

    @classmethod
    def of(
        cls,
        *,
        port_path: str,
        block_tag: str | None,
        com_spec_tag: str,
        data_element_ref: str | None,
        position: int,
        port_position: int,
    ) -> PortInitKey:
        """The key of one com-spec, its block defaulted by ``arxml_reader.com_spec_block``."""
        return cls(
            port_path=port_path,
            block=com_spec_block(block_tag, com_spec_tag),
            com_spec_tag=com_spec_tag,
            data_element_ref=data_element_ref or "",
            position=position,
            port_position=port_position,
        )

    @classmethod
    def of_init(cls, com_spec: PortComSpecInitInfo) -> PortInitKey:
        """The key of the com-spec one reader init record was read from (``map_interfaces``)."""
        return cls.of(
            port_path=com_spec.port_path,
            block_tag=com_spec.block_tag,
            com_spec_tag=com_spec.com_spec_tag,
            data_element_ref=com_spec.data_element_ref,
            position=com_spec.position,
            port_position=com_spec.port_position,
        )

    @classmethod
    def of_com_spec(cls, port: SwcPortInfo, spec: DataElementComSpecInfo, block: str) -> PortInitKey:
        """The key of one com-spec shape of *port* in *block* (the component mapper's gate)."""
        return cls.of(
            port_path=port.path,
            block_tag=block,
            com_spec_tag=spec.com_spec_tag,
            data_element_ref=spec.data_element_ref,
            position=spec.position,
            port_position=port.position,
        )


class CarriedValueLine(NamedTuple):
    """One line a carried init value takes, with its category and rule.

    ``type_mapping.carried_value_lines`` decides it, and each reporter of a carried
    value records it whole. The descriptive lines (the numerical form under a
    collapsed twin, a unit that is not the type's, a member's number read through
    its type's table) are ``NOT_SUPPORTED`` with no rule; a prototype's own value
    given as a number and read through its type's table is ``EXPORT_DEFECT`` with
    the rule it breaks (``[constr_1221]`` or ``[constr_1225]``), while the value is
    carried.
    """

    text: str
    category: SkipCategory
    rule: StandardRule | None = None


@dataclass(frozen=True)
class PortInitOutcome:
    """A port com-spec's own init value, as ``map_interfaces`` decided it.

    Asked only of a com-spec whose ``INIT-VALUE`` is not the reference to its data
    element's own constant. Exactly one of three answers:

    * ``value`` -- the value shaped against the element's type, differing from the
      element's own ``init`` (or the element has none): the port's ``com_specs``
      entry carries it;
    * ``reason`` -- why it is not carried (unreadable, a constant not readable here,
      or not of the element's type's shape), with its category and rule;
    * neither -- the value equals the element's own, so the writer's reference to the
      element's constant states it.

    ``lost_labels`` counts the source value's ``SHORT-LABEL``s the writer does not
    write back; ``in_place`` says the source wrote the value in place rather than
    through a constant. ``value_lines`` are the descriptive lines of a carried value
    (``type_mapping.carried_value_lines``), ``radix_spellings`` its radix spellings.
    ``type_target`` is the element type's path when the export declares it nowhere,
    and ``once_stubbed`` the reason's re-wording should that path become an external
    type stub (``UnresolvedReferences.chain_skip``). The component mapper decides
    what of this becomes a model value or a line.
    """

    value: object | None = None
    reason: SkipReason | None = None
    lost_labels: int = 0
    in_place: bool = False
    radix_spellings: tuple[str, ...] = ()
    value_lines: tuple[CarriedValueLine, ...] = ()
    type_target: str | None = None
    once_stubbed: Callable[[str], str] | None = None


@dataclass(frozen=True)
class VariableTypeOutcome:
    """An inter-runnable variable's type and initial value, as ``type_mapping.map_variable_types`` decided them.

    The component mapper records every line of a variable it keeps and none of one
    it does not. Either ``type_name`` is the name kept at the ``TYPE-TREF``'s path,
    on the side ``application`` says, or ``skip`` says why not. A skip whose path the
    file declares nowhere and whose ``DEST`` is no application kind carries that path
    as ``keep_target``: the unresolved-reference policy may keep it, and the external
    type stub minted for it is then the variable's ``implementation_type``; an
    application type declared nowhere has no stub and stays a skip. ``init`` is the
    value shaped against the type, or ``init_reason`` says why the variable is kept
    without it. ``sw_calibration_access`` is the model's literal, ``None`` when the
    source states none the model carries. ``lines`` are the ``(path, tag, line)``
    triples of a carried value, ``radix_spellings`` its radix spellings.
    """

    type_name: str | None = None
    application: bool = False
    skip: SkipReason | None = None
    keep_target: str | None = None
    init: InitValue | None = None
    init_reason: SkipReason | None = None
    sw_calibration_access: Literal["not_accessible", "read_only", "read_write"] | None = None
    lines: tuple[tuple[str, str, CarriedValueLine], ...] = ()
    radix_spellings: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnreadPhysics:
    """One report line about a physical property an application type declared and this import did not carry.

    ``source_path`` is the application primitive's ARXML path, not its emitted name,
    so the lines can be narrowed to the types the import writes even after
    ``reconcile.py`` renamed one.
    """

    source_path: str
    line: str


@dataclass(frozen=True)
class TwinReduction:
    """One app/impl twin collapse line, and the pair of paths it is about.

    ``app_path`` is what :meth:`PathRegistry.carried_reductions` resolves, so the
    line follows a later rename. The pair lets ``type_mapping.map_mapping_sets`` tell
    the ``DATA-TYPE-MAP`` row that caused this collapse from any other self-map row (a
    file may hold ``App/X -> App/X`` beside ``App/X -> Impl/X``): the first is folded
    into the collapse, every other self-map keeps its abort.
    """

    app_path: str
    impl_path: str
    line: str


@dataclass(frozen=True)
class RenameRecord:
    """One rename line, and what decides whether its element is written.

    The other fields are the key a ``--packages`` narrowing asks:

    * ``source_path`` -- a data type registered under a replacement name;
      :meth:`PathRegistry.resolve` follows every later rename of it.
    * ``renamed_from`` -- an element that carried that name and carries ``name``
      since: a later record renaming ``name`` again continues the chain.
    * neither -- an element that never carried the old name (an external stub named
      past a claimed one), so no chain runs through its old half.
    """

    line: str
    layer: str
    name: str
    source_path: str | None = None
    renamed_from: str | None = None


@dataclass
class PathRegistry:
    """ARXML path -> SWCraft emitted name.

    All data-type reference resolution goes through ``resolve()``: refs are full
    paths, so lookups are unambiguous whatever bare names collide. Every
    registration and claim names its element's layer (``application_types``,
    ``implementation_types`` or ``base_types``), and a name is taken in that layer
    alone; :meth:`layer_of` tells which of two layers sharing a name a reference
    means.
    """

    _path_to_name: dict[str, str] = field(default_factory=dict)
    _path_layer: dict[str, str] = field(default_factory=dict)
    _used_names: dict[str, set[str]] = field(default_factory=dict)
    # (layer, emitted name) -> the declared bare name, for every type not emitted under it.
    _source_names: dict[tuple[str, str], str] = field(default_factory=dict)
    # One record per twin collapse and per suffix rename, structured so a
    # `--packages` selection can narrow them.
    reduction_records: list[TwinReduction] = field(default_factory=list)
    rename_records: list[RenameRecord] = field(default_factory=list)
    # One line per data element whose `init` was read from a hexadecimal spelling
    # and is written in decimal: the loss is recorded rather than hidden.
    init_radix_normalizations: list[InitRadixNormalization] = field(default_factory=list)
    # One line per physical property an APPLICATION-PRIMITIVE-DATA-TYPE declared
    # and this import did not carry: the type is imported, so none of it is a skip.
    unread_physics: list[UnreadPhysics] = field(default_factory=list)
    # Values carried in another spelling (`record_represented`), as an ordered set.
    represented: dict[Represented, None] = field(default_factory=dict)
    # Emitted interface name -> the ARXML path `map_interfaces` committed under it.
    # The whole-export importer writes the layer in this path order, so which of two
    # interfaces differing only in case gets the plain file name is decided by the
    # export and not by the platform. A CONFLICTING group's name is in it, at the
    # first member met, because the group claims the name without importing a member.
    interface_source_paths: dict[str, str] = field(default_factory=dict)
    # Every port com-spec init `map_interfaces` decided: the decision needs the
    # element's type and init, which only that loop has, and its use needs the kept
    # ports, which only `map_swcs` and `map_compositions` have.
    port_inits: dict[PortInitKey, PortInitOutcome] = field(default_factory=dict)
    # Every inter-runnable variable's type and init by its ARXML path, decided by
    # `type_mapping.map_variable_types` and spent by `map_swcs`, for the same reason.
    variable_types: dict[str, VariableTypeOutcome] = field(default_factory=dict)
    # The `(path, tag)` keys of reader lines the import carries or words anew (an
    # implementation type's COMPU-METHOD-REF, a carried constraint's CONSTR-LEVEL),
    # which each importer leaves out of its report (:meth:`uncarried`).
    carried_reader_lines: set[tuple[str, str]] = field(default_factory=set)
    # Every non-primary member path of a MERGED group -> the member path mapped in
    # its place, read by `resolve()` alone. Empty for `import component`, which
    # passes no verdict.
    merge_aliases: dict[str, str] = field(default_factory=dict)
    # The name `claim()` handed out per CONFLICTING group, so a later member of the
    # group reuses it rather than minting a second one.
    _claimed_groups: dict[object, str] = field(default_factory=dict)

    def _names(self, layer: str) -> set[str]:
        """The names taken in *layer*, created empty on first use."""
        return self._used_names.setdefault(layer, set())

    @property
    def reductions(self) -> list[str]:
        """The app/impl twin collapse lines, in the order they were recorded."""
        return [record.line for record in self.reduction_records]

    @property
    def renames(self) -> list[str]:
        """The rename lines, in the order they were recorded -- every namespace's."""
        return [record.line for record in self.rename_records]

    def carried_reductions(self, data_type_keys: Collection[tuple[str, str]]) -> list[str]:
        """The collapse lines whose collapsed type is among *data_type_keys*, each ``(layer, name)``.

        The application path is asked, not the printed name, because a later pass can
        rename the element and :meth:`resolve` follows it.
        """
        return [
            record.line
            for record in self.reduction_records
            if (self.layer_of(record.app_path), self.resolve(record.app_path)) in data_type_keys
        ]

    def uncarried(self, skip_log: SkipLog) -> Iterator[SkipEntry]:
        """The reader's entries of *skip_log* but those :attr:`carried_reader_lines` names, in order."""
        return (entry for entry in skip_log if (entry.path, entry.tag) not in self.carried_reader_lines)

    def carried_renames(self, written: Mapping[str, Collection[str]]) -> list[str]:
        """The rename lines about an element the project is written with.

        *written* maps a layer to the names it is written with -- ``swc`` holding the
        compositions too. A data type's line asks its ARXML path; every other line
        follows its name through later renames of the same element.
        What this does not reach: a line whose layer *written* does not name is dropped.
        """
        onward: dict[tuple[str, str], str] = {
            (record.layer, record.renamed_from): record.name
            for record in self.rename_records
            if record.renamed_from is not None
        }
        carried: list[str] = []
        for record in self.rename_records:
            names = written.get(record.layer, ())
            if record.source_path is not None:
                if self.resolve(record.source_path) in names:
                    carried.append(record.line)
                continue
            name, seen = record.name, {record.name}
            while (record.layer, name) in onward and onward[(record.layer, name)] not in seen:
                name = onward[(record.layer, name)]
                seen.add(name)
            if name in names:
                carried.append(record.line)
        return carried

    def _record_type_rename(self, path: str, emitted: str, layer: str) -> None:
        """Report that the data type declared at *path* is written as *emitted*, in *layer*."""
        self.rename_records.append(
            RenameRecord(line=f"{path} -> {emitted}", layer=layer, name=emitted, source_path=path)
        )

    def record_init_radix_normalization(self, interface_name: str, element_path: str, spellings: list[str]) -> None:
        """Record that `element_path`'s init was read in another radix and is written in decimal.

        The spellings are quoted verbatim. The caller records only a committed
        interface's elements, and only a leaf written as a number. `interface_name`
        lets a ``--packages`` narrowing drop the line with its interface
        (:func:`~swcraft_tools.arxml_import.pipeline._narrow_records`).
        """
        self.init_radix_normalizations.append(
            InitRadixNormalization(
                interface=interface_name,
                element_path=element_path,
                line=init_radix_line(element_path, spellings),
            )
        )

    def record_unread_physics(self, source_path: str, reason: str) -> None:
        """Record that *source_path*'s application type was imported without a declared physical property.

        The line names the export's AUTOSAR paths only, never where the project is stored.
        """
        self.unread_physics.append(UnreadPhysics(source_path=source_path, line=f"{source_path}: {reason}"))

    def resolve(self, path: str) -> str | None:
        """The emitted SWCraft name for `path`, or None if it was never registered (not yet processed, or skipped).

        A path :attr:`merge_aliases` names answers the name of the declaration its
        group was merged into, so a reference at either path finds the one emitted
        name. An alias points at a primary, and a primary is never an alias.
        """
        name = self._path_to_name.get(path)
        if name is None:
            target = self.merge_aliases.get(path)
            if target is not None:
                return self._path_to_name.get(target)
        return name

    def layer_of(self, path: str) -> str | None:
        """The layer of the element *path* resolves to, or None where :meth:`resolve` answers None.

        A merged member answers its primary's layer, and the collapsed application
        path of a bare same-name pair answers the implementation layer.
        """
        layer = self._path_layer.get(path)
        if layer is None:
            target = self.merge_aliases.get(path)
            if target is not None:
                return self._path_layer.get(target)
        return layer

    def claim(self, bare_name: str, layer: str, path: str = "", group: object | None = None) -> str:
        """Claim, in *layer*, the name a declaration this import **refused** would have taken.

        Called for a group decided **conflicting**. Nothing is registered, but the
        name stays out of circulation in its layer, so an unrelated declaration of
        that name is renamed exactly as it would have been had the group imported, and
        repairing the defect in the source renames no other element. A name already
        taken claims the rule-2 replacement the group would have been given. No rename
        is reported and no source name recorded: no element is written.

        Args:
            bare_name: the ``SHORT-NAME`` the refused declarations declared.
            layer: the layer the group's members would have been written to --
                its ``merge.GroupKey.layer``.
            path: one member's ARXML path, for the package segment of the second
                candidate -- only read when the bare name is already taken.
            group: the key the members of one refusal share, so that they take one
                claim. `None` claims unconditionally.
        """
        if group is not None and group in self._claimed_groups:
            return self._claimed_groups[group]
        names = self._names(layer)
        if bare_name not in names:
            names.add(bare_name)
            if group is not None:
                self._claimed_groups[group] = bare_name
            return bare_name
        emitted = self._first_free_candidate(bare_name, path, layer)
        names.add(emitted)
        if group is not None:
            self._claimed_groups[group] = emitted
        return emitted

    def register(self, path: str, bare_name: str, layer: str) -> str:
        """Register `path` under `bare_name` in *layer*, applying rule 2 on a collision inside that layer.

        Returns:
            The emitted name.

        Raises:
            NameCollisionError: `path` was already registered (rule 3, a malformed
                export). A bare-name collision between distinct paths is always resolved.
        """
        if path in self._path_to_name:
            raise NameCollisionError(
                f"Duplicate declaration at path '{path}' (already registered "
                f"as '{self._path_to_name[path]}') -- malformed export"
            )
        final = self._reserve(bare_name, path, layer)
        self._path_to_name[path] = final
        self._path_layer[path] = layer
        return final

    def register_twin(self, app_path: str, impl_path: str, bare_name: str) -> str:
        """App/impl twin collapse (rule 1): both paths resolve to ONE emitted name, recorded as a reduction.

        The name is whichever `impl_path` already claimed (implementation types are
        processed first), or a freshly reserved one as a fallback. It is read through
        :meth:`resolve`, not the raw map, because `impl_path` may be a merged group's
        non-primary member, which carries no name of its own; nothing is ever
        registered under an alias.
        """
        final = self.resolve(impl_path)
        if final is None:
            final = self.register(self.merge_aliases.get(impl_path, impl_path), bare_name, _IMPLEMENTATION_TYPES_LAYER)
        if app_path in self._path_to_name:
            raise NameCollisionError(
                f"Duplicate declaration at path '{app_path}' (already "
                f"registered as '{self._path_to_name[app_path]}') -- "
                "malformed export"
            )
        self._path_to_name[app_path] = final
        # The application path resolves to the implementation type it collapsed
        # onto, so it answers that type's layer; nothing is written, and nothing
        # is claimed, in the application layer.
        self._path_layer[app_path] = self.layer_of(impl_path) or _IMPLEMENTATION_TYPES_LAYER
        self.reduction_records.append(
            TwinReduction(
                app_path=app_path,
                impl_path=impl_path,
                line=f"{app_path} + {impl_path} -> {final} (app/impl twin collapse)",
            )
        )
        return final

    def register_external(self, path: str, name: str) -> None:
        """Register the external type stub minted for the absent *path* under *name*.

        A stub is written to the ``implementation_types`` layer, so it takes its place
        in the path map (write order, ``--packages`` narrowing, :attr:`source_paths`)
        and in :meth:`claimed_names`. :func:`stub_name` minted the name, and the path
        is one the export declares nowhere, so it is never already mapped.
        """
        self._path_to_name[path] = name
        self._path_layer[path] = _IMPLEMENTATION_TYPES_LAYER
        self._names(_IMPLEMENTATION_TYPES_LAYER).add(name)

    def claimed_names(self, layer: str) -> frozenset[str]:
        """Every name ``_reserve()`` or :meth:`claim` has handed out in *layer*, as a read-only snapshot.

        A superset of what the import finally writes: a name claimed by a type a
        ``--packages`` selection later dropped is still avoided, which keeps one
        import's names identical with or without a selection.
        """
        return frozenset(self._used_names.get(layer, ()))

    @property
    def source_names(self) -> dict[tuple[str, str], str]:
        """``(layer, emitted data-type name)`` -> the ``SHORT-NAME`` the export declared it under.

        Only the moved types: an empty map means this import renamed no data type.
        Both rename mechanisms maintain it (rule 2's suffixing in :meth:`_reserve` and
        :meth:`rename`, which chains onto it), so a name moved twice still points at
        the export. A refusal quotes it, so the message names a string the reader can
        search the export for.

        Returns:
            A copy, so a caller cannot edit the registry's own map.
        """
        return dict(self._source_names)

    @property
    def source_paths(self) -> dict[tuple[str, str], str]:
        """``(layer, emitted data-type name)`` -> the ARXML path it was **first** registered from.

        The path map is many-to-one: a twin collapse maps the application path onto
        the implementation path's name, and the implementation path is kept. A name
        moved by :meth:`rename` keeps its path. The whole-export importer writes the
        types in this path order, so which of two types differing only in case gets
        the plain file name depends on the export, not the filesystem.

        Returns:
            A copy, in registration order.
        """
        paths: dict[tuple[str, str], str] = {}
        for path, name in self._path_to_name.items():
            paths.setdefault((self._path_layer[path], name), path)
        return paths

    def record_rename(self, old: str, new: str, reason: str, *, layer: str, moved: bool = True) -> None:
        """Report a rename this registry's own namespace was not part of.

        For component renames and external-stub names, which this registry does not
        hand out. It only reports: :meth:`rename` would also re-point a data type
        sharing the component's name (a data type and an SWC may share one).

        Args:
            old: the name being replaced.
            new: its replacement.
            reason: why the writer reserves `old`, rendered in the report.
            layer: the layer the renamed element is written to -- ``swc`` for a
                composition too, which shares the component types' namespace.
            moved: whether the element carried `old` before this rename. False for an
                external stub named past a name another component holds, so that
                :meth:`carried_renames` never reads that component as renamed.
        """
        self.rename_records.append(
            RenameRecord(
                line=f"{old} -> {new} ({reason})",
                layer=layer,
                name=new,
                renamed_from=old if moved else None,
            )
        )

    def rename(self, old: str, new: str, reason: str, *, layer: str) -> None:
        """Re-point every path of *layer* emitted as `old` at `new`, and report it.

        The reserved-name write-back for the data-type namespace: a name colliding
        with one the writer reserves (``Mode_CompuMethod`` beside enum ``Mode``) is
        only knowable after mapping (:mod:`swcraft_tools.arxml_import.reconcile`).
        `old` is freed and `new` taken, so no later mint hands `old` out twice. A
        caller renaming in another namespace wants :meth:`record_rename`.

        Args:
            old: the currently emitted name.
            new: its replacement, already checked free by the caller.
            reason: why the writer reserves `old`, rendered in the report.
            layer: the layer of the renamed type; a type of another layer under
                `old` is another element and keeps it.
        """
        for path, name in self._path_to_name.items():
            if name == old and self._path_layer[path] == layer:
                self._path_to_name[path] = new
        names = self._names(layer)
        names.discard(old)
        names.add(new)
        self._source_names[(layer, new)] = self._source_names.pop((layer, old), old)
        self.record_rename(old, new, reason, layer=layer)

    def _reserve(self, bare_name: str, path: str, layer: str) -> str:
        """The rule-2 bare-name collision policy, with every *minted* candidate capped.

        The declared name first, then :meth:`_collision_candidates` until one is free
        in *layer*. The first candidate is the **uncapped** declared name, so an
        over-length declared name reaches ``reconcile.identifier_violations()`` and
        aborts the import; every later candidate is capped by :func:`fit_identifier`.
        A candidate is accepted by membership in ``_used_names``, never by its shape,
        because two suffixes can truncate to one string. Reserved-name reconciliation
        does not happen here: it needs each type's kind and the final set.
        """
        names = self._names(layer)
        if bare_name not in names:
            names.add(bare_name)
            return bare_name

        emitted = self._first_free_candidate(bare_name, path, layer)
        self._claim_renamed(path, bare_name, emitted, layer)
        return emitted

    def _first_free_candidate(self, bare_name: str, path: str, layer: str) -> str:
        """The first rule-2 candidate for *bare_name* that is free in *layer*.

        It claims nothing: :meth:`_reserve` renames into the answer and :meth:`claim`
        merely reserves it, each adding it to the layer's names itself.
        """
        names = self._names(layer)
        candidates = self._collision_candidates(bare_name, path)
        emitted = next(candidates)
        while emitted in names:
            emitted = next(candidates)
        return emitted

    def _collision_candidates(self, bare_name: str, path: str) -> Iterator[str]:
        """Rule 2's replacement names for `bare_name`, in order, without end.

        ``{bare}_app``, then ``{bare}_app_{package}``, then ``{bare}_2_app``,
        ``{bare}_3_app``, … -- each capped by :func:`fit_identifier`, so each is at
        most 128 characters and a legal ``Identifier`` whenever `bare_name` is. The
        two named candidates come first because they say why the name exists.
        :func:`~swcraft_tools.arxml_import.reconcile.free_name` uses the same counter
        form, so a name moved by both phases stays readable.

        The loop over them terminates. The counter candidates are pairwise distinct
        for every ``k < 10**122`` (while ``len(f"_{k}_app") <= 127``): each is
        ``stem[:123 - d] + "_" + str(k) + "_app"`` with ``d`` the digit count, so with
        equal ``d`` the numerals differ, and with ``d(j) < d(k)`` the ``_`` opening
        ``j``'s numeral lands on a digit of ``k``'s. Past that bound
        :func:`fit_identifier` truncates the suffix too and candidates may coincide,
        but :meth:`_first_free_candidate` stops after at most ``len(_used_names) + 1``
        candidates, far below it. The generator claims nothing and does not read
        ``_used_names``.

        Args:
            bare_name: the declared ``SHORT-NAME`` that was already taken.
            path: its ARXML path, for the package segment of the second candidate.
        """
        yield fit_identifier(bare_name, RENAME_SUFFIX)
        yield fit_identifier(bare_name, f"{RENAME_SUFFIX}_{snake_case(path)}")
        counter = 2
        while True:
            yield fit_identifier(bare_name, f"_{counter}{RENAME_SUFFIX}")
            counter += 1

    def _claim_renamed(self, path: str, bare_name: str, emitted: str, layer: str) -> None:
        """Claim `emitted` for `path` in *layer*, report the rename, and remember the origin.

        The origin half is what lets a later refusal quote the string the export
        declared rather than this policy's own mint — see :attr:`source_names`.
        """
        self._names(layer).add(emitted)
        self._source_names[(layer, emitted)] = bare_name
        self._record_type_rename(path, emitted, layer)

    def record_represented(self, source_path: str, reasons: Iterable[str]) -> None:
        """Record that *source_path*'s element carries a value the export spells in another form, once per reason.

        An infinite ``PHYS-CONSTRS`` limit is carried as the absent bound
        (``type_mapping.infinite_limit_lines``): neither a loss nor an unsupported
        feature, so neither a skip entry nor an :attr:`unread_physics` line.
        *source_path* is an application type's or a standalone ``DATA-CONSTR``'s path.
        """
        for reason in reasons:
            # A dict as an ordered set: each entry once, at its first recording's position, in constant time.
            entry = Represented(source_path=source_path, line=f"{source_path}: {reason}")
            self.represented.setdefault(entry, None)


@dataclass(frozen=True)
class StubName:
    """One stub's minted name and the facts its rename line states (:func:`stub_names`)."""

    name: str
    #: The stub's index among the paths sharing its leaf, by sorted path.
    index: int
    #: How many paths share the leaf.
    namesakes: int

    @property
    def reason(self) -> str:
        """The rename line's reason, ``imported stub, namesake <index> of <n>``."""
        return f"imported stub, namesake {self.index} of {self.namesakes}"


def stub_names(leaf_by_path: Mapping[str, str], claimed: Collection[str]) -> dict[str, StubName]:
    """Every stub of one layer named ``<leaf>_ext<X>``, X from 0 by sorted source path.

    For interface, type and component stubs alike: *leaf_by_path* maps each stub's
    source path to its leaf, and namesakes are grouped by the exact, case-sensitive
    leaf. X counts each group's paths in sorted order and groups are taken in sorted
    leaf order, so names depend on no reading order and a re-import of SWCraft's own
    output mints the same names; where X is a free rank rather than the namesake index
    (two leaves whose shortened stems coincide, or a kept element already named
    ``<leaf>_ext0``), the sorted order resolves it the same way every time. *claimed*
    is never mutated.

    What this does **not** reach: it ranks the paths it is given. A caller ranks all
    of the file's stubs before any drop, so a stub a later pass drops leaves a gap in
    its group's numbering, which a re-import of that output closes.
    """
    groups: dict[str, list[str]] = {}
    for path in sorted(leaf_by_path):
        groups.setdefault(leaf_by_path[path], []).append(path)
    taken = set(claimed)
    named: dict[str, StubName] = {}
    for leaf in sorted(groups):
        paths = groups[leaf]
        x = 0
        for index, path in enumerate(paths):
            name, x = stub_name(leaf, max(index, x), taken)
            taken.add(name)
            named[path] = StubName(name=name, index=index, namesakes=len(paths))
            x += 1
    return named


@dataclass(frozen=True)
class Represented:
    """One *Represented in another form* line (:meth:`PathRegistry.record_represented`) and the path it is keyed by.

    Not :class:`UnreadPhysics`: the value is carried. Frozen, so hashable as a key of
    the registry's ordered dict.
    """

    source_path: str
    line: str
