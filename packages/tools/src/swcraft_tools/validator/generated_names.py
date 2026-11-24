"""The names the ARXML writer generates, which a model's own names must not take.

Holds the generated suffixes and prefixes and the derivations that build them, restated
here rather than imported because ``writer`` imports ``validator``: the generated
``_Unit``/``_Compu``/``_Range`` forms of data types, the ``{Interface}_{Element}_Init``
constant, and a runnable's access point names. The checks that report collisions and
lengths (``check_name_uniqueness``, ``check_identifier_conformance``) read these.
"""

from __future__ import annotations

from collections.abc import Iterable

from swcraft_tools.schema import ApplicationDataType, DataType, EnumDataType, Runnable, ValueDataType


def access_entries(runnable: Runnable) -> Iterable[tuple[str, str, str, str]]:
    """``(list, port, member field, member)`` for every entry of *runnable*, list by list.

    The lists are read as named attributes, never through ``getattr``, so that
    the coverage oracle can bind each field read to its attribute.
    """
    for read in runnable.reads:
        yield "reads", read.port, "element", read.element
    for write in runnable.writes:
        yield "writes", write.port, "element", write.element
    for call in runnable.calls:
        yield "calls", call.port, "operation", call.operation


# ---------------------------------------------------------------------------
# check_data_type_references
# ---------------------------------------------------------------------------

#: The three ``SHORT-NAME`` suffixes derived from an application type's own name —
#: the ``UNIT``, the ``COMPU-METHOD`` and the ``DATA-CONSTR`` emitted beside it.
#: Each is reserved **only when the field it is emitted for is declared**.
RESERVED_UNIT_SUFFIX = "_Unit"


RESERVED_COMPU_SUFFIX = "_Compu"


RESERVED_RANGE_SUFFIX = "_Range"


def generates_compu_method(dt: object) -> bool:
    """Whether *dt* generates its ``{name}_CompuMethod``: an enumeration that states ``literals``.

    Only then is the name reserved and charged against the ``Identifier`` limit;
    one naming a ``texttable`` compu method by ``compu_method_ref`` generates none.
    """
    return isinstance(dt, EnumDataType) and dt.literals is not None


#: The init constant's suffix: ``{Interface}_{Element}_Init`` leaves the interface
#: and data element names 122 characters *between them*. The constant is a
#: **sibling** of the interfaces in ``PortInterfaces``, so it can collide with a
#: declared interface name and with another constant.
GENERATED_INIT_CONSTANT_SUFFIX = "_Init"


#: The read, write and call access point prefixes, each joining a port name and
#: a data element or operation name. None is reserved: an access point is nested
#: in its runnable, so only two entries of one runnable can collide.
GENERATED_READ_ACCESS_PREFIX = "R_"


GENERATED_WRITE_ACCESS_PREFIX = "W_"


GENERATED_CALL_POINT_PREFIX = "Call_"


#: The two variable access forms: ``RV_{variable}`` for ``reads_variables`` and
#: ``WV_{variable}`` for ``writes_variables``, the writer's ``READ-LOCAL-VARIABLES``
#: and ``WRITTEN-LOCAL-VARIABLES``; the prefix leaves the variable name 125
#: characters. They share the runnable's access point name space with the three
#: forms above, and cannot equal one (second character ``V``, not ``_``).
GENERATED_READ_VARIABLE_PREFIX = "RV_"


GENERATED_WRITE_VARIABLE_PREFIX = "WV_"


#: Which of a runnable's three access lists each form is derived from.
GENERATED_ACCESS_PREFIXES: dict[str, str] = {
    "reads": GENERATED_READ_ACCESS_PREFIX,
    "writes": GENERATED_WRITE_ACCESS_PREFIX,
    "calls": GENERATED_CALL_POINT_PREFIX,
}


#: Which of a runnable's two variable access lists each variable form is derived
#: from.
GENERATED_VARIABLE_ACCESS_PREFIXES: dict[str, str] = {
    "reads_variables": GENERATED_READ_VARIABLE_PREFIX,
    "writes_variables": GENERATED_WRITE_VARIABLE_PREFIX,
}


def variable_access_entries(runnable: Runnable) -> list[tuple[str, str, str | None]]:
    """``(list, variable, stated name or None)`` for every variable access of *runnable*.

    ``reads_variables`` first, then ``writes_variables``, each in its own order.
    """
    return [
        *(("reads_variables", read.variable, read.name) for read in runnable.reads_variables),
        *(("writes_variables", write.variable, write.name) for write in runnable.writes_variables),
    ]


def generated_access_point(prefix: str, port_name: str, member_name: str) -> str:
    """The ``SHORT-NAME`` the writer derives for one runnable access entry.

    The derivation is **not injective**: ``a_b`` + ``c`` and ``a`` + ``b_c``
    both give ``R_a_b_c``.
    """
    return f"{prefix}{port_name}_{member_name}"


def stated_access_point_names(runnable: Runnable) -> list[str | None]:
    """Each access entry's stated ``name``, or ``None``, in :func:`access_entries` order."""
    return [
        *(read.name for read in runnable.reads),
        *(write.name for write in runnable.writes),
        *(call.name for call in runnable.calls),
    ]


def generated_init_constant(interface_name: str, element_name: str) -> str:
    """The ``SHORT-NAME`` the writer derives for one data element's init constant.

    The derivation is **not injective**: ``A_B`` + ``C`` and ``A`` + ``B_C``
    both give ``A_B_C_Init`` (``interface.init-constant-name-collision``).
    """
    return f"{interface_name}_{element_name}{GENERATED_INIT_CONSTANT_SUFFIX}"


def _generated_application_forms(application_type: ApplicationDataType) -> frozenset[str]:
    """Which of the three suffixes *application_type*'s name generates: the application half.

    Called by :func:`generated_data_type_forms` alone: a direct caller would see no value
    type's ``_Range``. The writer emits a ``UNIT`` only for a declared ``unit``, a ``COMPU-METHOD``
    only for a declared ``conversion`` and a ``DATA-CONSTR`` only for a declared
    ``range``. A blank ``unit`` is no unit.
    """
    forms: set[str] = set()
    if application_type.unit.strip():
        forms.add(RESERVED_UNIT_SUFFIX)
    if application_type.conversion is not None:
        forms.add(RESERVED_COMPU_SUFFIX)
    if application_type.range is not None:
        forms.add(RESERVED_RANGE_SUFFIX)
    return frozenset(forms)


def generated_data_type_forms(dt: DataType) -> frozenset[str]:
    """Which of the three suffixes *dt*'s name actually generates, whatever its kind.

    Restates the writer's ``arxml_generation.generated_data_type_forms``: an application
    type's forms, and a value type's ``_Range``, the ``DATA-CONSTR`` of ``INTERNAL-CONSTRS``
    emitted exactly when a ``value_min`` or a ``value_max`` is declared. Every other kind
    generates none.
    """
    if isinstance(dt, ApplicationDataType):
        return _generated_application_forms(dt)
    if isinstance(dt, ValueDataType) and (dt.value_min is not None or dt.value_max is not None):
        return frozenset({RESERVED_RANGE_SUFFIX})
    return frozenset()
