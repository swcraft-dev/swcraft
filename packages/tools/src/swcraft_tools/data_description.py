"""The data description of a model, resolved by name: :class:`DataDescription`.

One view over the layers that describe data, built once per read. A consumer
reads a **name** off an element or a prototype and asks the view for the
element that name names in one layer: :meth:`DataDescription.application`,
:meth:`DataDescription.implementation`, :meth:`DataDescription.base`,
:meth:`DataDescription.unit`, :meth:`DataDescription.compu_method` or
:meth:`DataDescription.data_constr`. The view answers that element, or ``None``.

**It resolves elements and reads no field of theirs** but ``name``, and it holds
no table of which kinds a role admits: those stay with the consumer that judges
them, so a field read is witnessed in the module that makes it. Which layer an
element belongs to is its class's, never a field of it.

**The model holds six such layers**: three of types (``application_types``,
``implementation_types``, ``base_types``) and three of the forms a type names by
reference (``units``, ``compu_methods``, ``data_constraints``). One name may be
declared once in each layer, and each accessor answers the element of its own
layer alone. :attr:`DataDescription.data_types` is the three type layers'
elements, so a consumer that walks every type meets no unit. A name declared
twice **inside one layer** resolves to its **first** declaration in the reader's
order: ``data-type.duplicate-name`` owns the repeat, and a check must judge one
element rather than whichever file was read last.

What this does **not** do: it is not the kernel's registry, it is no
editing-protocol surface, and it judges nothing -- an absent name is ``None``,
never a finding.
"""

from __future__ import annotations

from collections.abc import Iterable
from heapq import merge
from typing import Generic, Protocol, TypeVar

from swcraft_tools.reader import read_data_types, read_layer
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CompuMethod,
    DataConstr,
    DataDescriptionElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Linear,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    TextTable,
    Unit,
    ValueDataType,
)

#: The classes of the ``application_types`` layer's four top-level kinds.
APPLICATION_DATA_TYPE_CLASSES: tuple[type, ...] = (EnumDataType, RecordDataType, ArrayDataType, ApplicationDataType)

#: The ``implementation_types`` layer's six kinds, so an element of no data type class is refused, not filed there.
_IMPLEMENTATION_DATA_TYPE_CLASSES: tuple[type, ...] = (
    ValueDataType,
    ReferenceDataType,
    ImplementationRecordDataType,
    ImplementationArrayDataType,
    PointerDataType,
    ExternalDataType,
)

_Element = DataType | DataDescriptionElement


class _Named(Protocol):
    @property
    def name(self) -> str: ...


_E = TypeVar("_E", bound=_Named)


class _Layer(Generic[_E]):
    """One layer's elements in the order given, and each name resolved to its first or its last declaration."""

    __slots__ = ("_last_wins", "by_name", "in_order")

    def __init__(self, *, last_wins: bool) -> None:
        self._last_wins = last_wins
        self.in_order: list[_E] = []
        self.by_name: dict[str, _E] = {}

    def file(self, element: _E) -> None:
        self.in_order.append(element)
        if self._last_wins:
            self.by_name[element.name] = element
        else:
            self.by_name.setdefault(element.name, element)

    def get(self, name: str | None) -> _E | None:
        return None if name is None else self.by_name.get(name)


class DataDescription:
    """The data description layers of one model, each resolved by name.

    Each element is filed in the layer its class belongs to. Read-only.

    Raises:
        ValueError: for an element of no data description class, a defect of the caller.
    """

    __slots__ = (
        "_application",
        "_base",
        "_compu_methods",
        "_data_constraints",
        "_data_types",
        "_implementation",
        "_units",
    )

    def __init__(self, elements: Iterable[_Element], *, last_wins: bool = False) -> None:
        application: _Layer[DataType] = _Layer(last_wins=last_wins)
        implementation: _Layer[DataType] = _Layer(last_wins=last_wins)
        base: _Layer[DataType] = _Layer(last_wins=last_wins)
        units: _Layer[Unit] = _Layer(last_wins=last_wins)
        compu_methods: _Layer[CompuMethod] = _Layer(last_wins=last_wins)
        data_constraints: _Layer[DataConstr] = _Layer(last_wins=last_wins)
        in_order: list[DataType] = []
        for element in elements:
            if isinstance(element, Unit):
                units.file(element)
            elif isinstance(element, (TextTable, Linear)):
                compu_methods.file(element)
            elif isinstance(element, DataConstr):
                data_constraints.file(element)
            else:
                if isinstance(element, BaseDataType):
                    layer = base
                elif isinstance(element, APPLICATION_DATA_TYPE_CLASSES):
                    layer = application
                elif isinstance(element, _IMPLEMENTATION_DATA_TYPE_CLASSES):
                    layer = implementation
                else:
                    raise ValueError(f"{type(element)} is no data description class")
                layer.file(element)
                in_order.append(element)
        self._data_types: tuple[DataType, ...] = tuple(in_order)
        self._application = application
        self._implementation = implementation
        self._base = base
        self._units = units
        self._compu_methods = compu_methods
        self._data_constraints = data_constraints

    @classmethod
    def last_declared(cls, elements: Iterable[_Element]) -> DataDescription:
        """The view over *elements* in which a name declared twice in one layer resolves to its LAST declaration."""
        return cls(elements, last_wins=True)

    @property
    def data_types(self) -> tuple[DataType, ...]:
        """Every element of the three type layers, repeats included, in the order given.

        No unit, compu method or data constraint is among them.
        """
        return self._data_types

    @property
    def units(self) -> tuple[Unit, ...]:
        """Every element of the ``units`` layer, repeats included, in the order given."""
        return tuple(self._units.in_order)

    @property
    def compu_methods(self) -> tuple[CompuMethod, ...]:
        """Every element of the ``compu_methods`` layer, repeats included, in the order given."""
        return tuple(self._compu_methods.in_order)

    @property
    def data_constraints(self) -> tuple[DataConstr, ...]:
        """Every element of the ``data_constraints`` layer, repeats included, in the order given."""
        return tuple(self._data_constraints.in_order)

    @property
    def elements(self) -> tuple[DataDescriptionElement, ...]:
        """Every unit, compu method and data constraint, layer by layer in that order, repeats included."""
        return (*self.units, *self.compu_methods, *self.data_constraints)

    def application(self, name: str | None) -> DataType | None:
        """The application type *name* names in the ``application_types`` layer, or ``None``."""
        return self._application.get(name)

    def implementation(self, name: str | None) -> DataType | None:
        """The implementation type *name* names in the ``implementation_types`` layer, or ``None``.

        An external data type stub included; never an application type or a base type.
        """
        return self._implementation.get(name)

    def base(self, name: str | None) -> DataType | None:
        """The base type *name* names in the ``base_types`` layer, or ``None``."""
        return self._base.get(name)

    def unit(self, name: str | None) -> Unit | None:
        """The unit *name* names in the ``units`` layer, or ``None``."""
        return self._units.get(name)

    def compu_method(self, name: str | None) -> CompuMethod | None:
        """The compu method *name* names in the ``compu_methods`` layer, either kind, or ``None``."""
        return self._compu_methods.get(name)

    def data_constr(self, name: str | None) -> DataConstr | None:
        """The data constraint *name* names in the ``data_constraints`` layer, or ``None``."""
        return self._data_constraints.get(name)


def _by_name(data_type: DataType) -> str:
    return data_type.name


def read_data_description(model_root: str) -> DataDescription:
    """The data description of the model under *model_root*, read once.

    The three type layers are merged by name (:attr:`DataDescription.data_types`);
    the ``units``, ``compu_methods`` and ``data_constraints`` layers follow, each in
    its reader's order.

    Raises:
        ValueError: as the layer reader does, naming the file that fails to
            parse or to validate.
    """
    return DataDescription(
        [
            *merge(
                read_layer(model_root, "application_types"),
                read_data_types(model_root),
                read_layer(model_root, "base_types"),
                key=_by_name,
            ),
            *read_layer(model_root, "units"),
            *read_layer(model_root, "compu_methods"),
            *read_layer(model_root, "data_constraints"),
        ]
    )
