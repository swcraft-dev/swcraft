"""The AUTOSAR schema versions SWCraft can emit, and their XSD filenames.

A leaf outside the ``writer`` package, importing nothing from ``swcraft_tools``, so the manifest loader can
validate ``arxml_version`` without importing the writer.

**Version -> filename is a table, never a format string.** AUTOSAR adopted the numbered
``AUTOSAR_000NN.xsd`` form from AP R17-03 (``AUTOSAR_00042.xsd``) onward; both versions supported here
predate that switch and use the dashed form, so a format string would produce a wrong filename the first time
a numbered-form version is added.

The XML namespace is ``r4.0`` across the whole 4.x line, so for the element subset SWCraft emits, 4.3.0
output differs from 4.2.2 output only in the ``xsi:schemaLocation`` filename token; the writer has no
version-specific branching.
"""

from __future__ import annotations

import os
from enum import StrEnum
from types import MappingProxyType

#: The AUTOSAR XML namespace, stable across the whole 4.x line. The single definition, so the writer emits
#: exactly the namespace the reader traverses.
NAMESPACE = "http://autosar.org/schema/r4.0"

#: The XML Schema instance namespace, carrier of the ``xsi:schemaLocation`` attribute.
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"

#: The ``SHORT-NAME``\ s of the root ``AR-PACKAGE``\ s every generated document declares the project's own
#: elements in, in creation (and emission) order. The validator reads it to refuse an external stub whose
#: ``external_ref`` lies inside one of them, since such a reference would resolve to a generated element
#: rather than to the source's; it lives here because the validator may not import the writer.
ROOT_PACKAGE_NAMES: tuple[str, str, str] = ("DataTypes", "PortInterfaces", "ComponentTypes")

#: The package of each data description layer under ``/DataTypes``: ``/DataTypes`` holds no element of its own,
#: each layer's elements are emitted in ``/DataTypes/{package}``, and an element's ``package`` is one further
#: sub-package below that. A model with no data type emits no layer package. A generated private form
#: (``{name}_Unit``, ``{name}_Compu``, ``{name}_Range``, ``{enum}_CompuMethod``) is emitted in the package of
#: the type it is generated for: ``Units``, ``CompuMethods`` and ``Constraints`` hold declared elements only.
DATA_DESCRIPTION_PACKAGE_NAMES: MappingProxyType[str, str] = MappingProxyType(
    {
        "application_types": "Application",
        "implementation_types": "Implementation",
        "base_types": "Base",
        "units": "Units",
        "compu_methods": "CompuMethods",
        "data_constraints": "Constraints",
        "mapping_sets": "MappingSets",
    }
)

#: ``SwcInternalBehavior.handleTerminationAndRestart``: the model's value of
#: ``field:swc.swc.handle_termination_and_restart`` -> the XML literal of
#: ``HANDLE-TERMINATION-AND-RESTART-ENUM``. The model spells the template's literal names (``noSupport``,
#: ``canBeTerminated``, ``canBeTerminatedAndRestarted``) in lower snake case; the XML spelling is the licensed
#: schemas' (``AUTOSAR_4-2-2.xsd:72821-72833``, ``AUTOSAR_4-3-0.xsd:84060-84072``), the same three at both
#: versions. Shared by the writer and the reader, which may not import the writer.
HANDLE_TERMINATION_AND_RESTART_LITERALS: MappingProxyType[str, str] = MappingProxyType(
    {
        "no_support": "NO-SUPPORT",
        "can_be_terminated": "CAN-BE-TERMINATED",
        "can_be_terminated_and_restarted": "CAN-BE-TERMINATED-AND-RESTARTED",
    }
)


class ArxmlVersion(StrEnum):
    """An AUTOSAR schema version SWCraft can generate ARXML for."""

    V4_2_2 = "4.2.2"
    V4_3_0 = "4.3.0"

    @property
    def schema_filename(self) -> str:
        """The XSD filename this version's ``xsi:schemaLocation`` names."""
        return SCHEMA_FILENAMES[self]

    @property
    def namespace(self) -> str:
        """The XML namespace for this version (``r4.0`` for every 4.x member)."""
        return NAMESPACE

    @property
    def schema_location(self) -> str:
        """The full ``xsi:schemaLocation`` attribute value: namespace, space, filename."""
        return f"{self.namespace} {self.schema_filename}"

    @classmethod
    def from_string(cls, value: str) -> ArxmlVersion:
        """Parse a dotted version string (``"4.2.2"``) into a member.

        Args:
            value: the version string, or an ``ArxmlVersion`` (returned as-is).

        Returns:
            The matching member.

        Raises:
            TypeError: if *value* is not a string.
            ValueError: if *value* is not a supported version; the message names the rejected value and every
                supported one.
        """
        if not isinstance(value, str):
            raise TypeError(f"AUTOSAR version must be a string, got {type(value).__name__}: {value!r}")
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"Unsupported AUTOSAR version {value!r}; supported versions are: {', '.join(SUPPORTED_VERSIONS)}"
            ) from None


#: Version -> XSD filename, exhaustive over ``ArxmlVersion``.
SCHEMA_FILENAMES: dict[ArxmlVersion, str] = {
    ArxmlVersion.V4_2_2: "AUTOSAR_4-2-2.xsd",
    ArxmlVersion.V4_3_0: "AUTOSAR_4-3-0.xsd",
}

#: The supported version strings, in declaration order.
SUPPORTED_VERSIONS: tuple[str, ...] = tuple(version.value for version in ArxmlVersion)

#: The default version; 4.3.0 is opt-in.
DEFAULT_ARXML_VERSION = ArxmlVersion.V4_2_2

#: Case-folded XSD filename -> version, the inverse of :data:`SCHEMA_FILENAMES`: a document declaring
#: ``autosar_4-2-2.xsd`` names the 4.2.2 schema as surely as ``AUTOSAR_4-2-2.xsd`` does.
_VERSION_BY_SCHEMA_FILENAME: MappingProxyType[str, ArxmlVersion] = MappingProxyType(
    {filename.casefold(): version for version, filename in SCHEMA_FILENAMES.items()}
)


def schema_filename_from_location(schema_location: str | None) -> str | None:
    """The XSD basename an ``xsi:schemaLocation`` value names, if any.

    The token paired with the AUTOSAR :data:`NAMESPACE`, or a sole token; ``None`` for an absent or empty
    value and for pairs none of which is the AUTOSAR namespace's.
    """
    if not schema_location:
        return None
    tokens = schema_location.split()
    for namespace, location in zip(tokens[::2], tokens[1::2], strict=False):
        if namespace == NAMESPACE:
            return os.path.basename(location)
    if len(tokens) == 1:
        return os.path.basename(tokens[0])
    return None


def release_of_schema_filename(schema_filename: str | None) -> ArxmlVersion | None:
    """The version whose :attr:`ArxmlVersion.schema_filename` is *schema_filename*, else ``None``.

    Compared case-insensitively. ``None`` means *no release SWCraft writes*: the
    file named another schema, or (a ``None`` argument) none at all.
    """
    if schema_filename is None:
        return None
    return _VERSION_BY_SCHEMA_FILENAME.get(schema_filename.casefold())
