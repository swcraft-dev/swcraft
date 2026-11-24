"""A small builder for synthetic ARXML exports carrying implementation composites and pointers.

Every name is invented. The shapes are the measured ones: depth-one ``TYPE_REFERENCE``
sub-elements read at their own ``SW-DATA-DEF-PROPS`` conditional, an array's
``ARRAY-SIZE`` and ``ARRAY-SIZE-SEMANTICS`` on its one sub-element, a pointer's
``SW-POINTER-TARGET-PROPS`` with ``TARGET-CATEGORY`` ``TYPE_REFERENCE`` and a nested
``IMPLEMENTATION-DATA-TYPE-REF``. Each function returns one XML fragment; a test
composes a document out of them and edits what its control needs.

What it does **not** reach: it writes no ``ADMIN-DATA``, no ``DESC`` and no
``SW-DATA-DEF-PROPS`` on a composite type itself, and the documents are not held to
either licensed schema -- the importer is the subject, and the reader tolerates
everything it does not read.
"""

from __future__ import annotations

from pathlib import Path

TYPES = "/Acme/Types"
APP = "/Acme/AppTypes"
MAPS = "/Acme/Maps"
INTERFACES = "/Acme/Interfaces"
CONSTANTS = "/Acme/Constants"
COMPONENTS = "/Acme/Components"

IMPL = "IMPLEMENTATION-DATA-TYPE"
PRIMITIVE = "APPLICATION-PRIMITIVE-DATA-TYPE"
RECORD = "APPLICATION-RECORD-DATA-TYPE"
ARRAY = "APPLICATION-ARRAY-DATA-TYPE"


def document(*packages: str) -> str:
    """A whole export: the packages under one ``Acme`` root package unless they carry their own."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd">\n'
        "<AR-PACKAGES>\n" + "".join(packages) + "</AR-PACKAGES>\n</AUTOSAR>\n"
    )


def package(name: str, *elements: str, packages: tuple[str, ...] = ()) -> str:
    """One ``AR-PACKAGE`` holding *elements* and the sub-packages *packages*."""
    body = f"<SHORT-NAME>{name}</SHORT-NAME>\n"
    if elements:
        body += "<ELEMENTS>\n" + "".join(elements) + "</ELEMENTS>\n"
    if packages:
        body += "<AR-PACKAGES>\n" + "".join(packages) + "</AR-PACKAGES>\n"
    return f"<AR-PACKAGE>\n{body}</AR-PACKAGE>\n"


def acme(**sub_packages: tuple[str, ...]) -> str:
    """The ``/Acme`` root package with one sub-package per keyword, each holding its elements."""
    return package("Acme", packages=tuple(package(name, *elements) for name, elements in sub_packages.items()))


def write(tmp_path: Path, text: str, name: str = "export.arxml") -> Path:
    """Write *text* as an export file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- implementation side ------------------------------------------------------


def props(inner: str) -> str:
    return (
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f"{inner}"
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
    )


def impl_ref(path: str, dest: str = IMPL) -> str:
    return f'<IMPLEMENTATION-DATA-TYPE-REF DEST="{dest}">{path}</IMPLEMENTATION-DATA-TYPE-REF>'


def base(name: str, size: int = 16) -> str:
    return (
        f"<SW-BASE-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        f"<BASE-TYPE-SIZE>{size}</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
        f"<NATIVE-DECLARATION>{name}</NATIVE-DECLARATION></SW-BASE-TYPE>\n"
    )


def value(name: str, base_name: str, base_package: str = TYPES) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        + props(f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{base_package}/{base_name}</BASE-TYPE-REF>')
        + "</IMPLEMENTATION-DATA-TYPE>\n"
    )


def alias(name: str, target: str) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
        + props(impl_ref(target))
        + "</IMPLEMENTATION-DATA-TYPE>\n"
    )


def sub(name: str, target: str, *, category: str = "TYPE_REFERENCE", extra: str = "", dest: str = IMPL) -> str:
    """One depth-one sub-element referencing the named implementation type *target*."""
    return (
        f"<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>"
        f"{extra}{props(impl_ref(target, dest))}</IMPLEMENTATION-DATA-TYPE-ELEMENT>"
    )


def inline_value_sub(name: str, base_path: str) -> str:
    """An inline ``VALUE`` member: its own ``BASE-TYPE-REF``, no named type."""
    return (
        f"<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        + props(f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{base_path}</BASE-TYPE-REF>')
        + "</IMPLEMENTATION-DATA-TYPE-ELEMENT>"
    )


def nested_sub(name: str, *inner: str) -> str:
    """A nested inline ``STRUCTURE`` member carrying sub-elements of its own."""
    return (
        f"<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
        "<SUB-ELEMENTS>" + "".join(inner) + "</SUB-ELEMENTS></IMPLEMENTATION-DATA-TYPE-ELEMENT>"
    )


def structure(name: str, *subs: str, category: str = "STRUCTURE") -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>"
        "<SUB-ELEMENTS>" + "".join(subs) + "</SUB-ELEMENTS></IMPLEMENTATION-DATA-TYPE>\n"
    )


def array(
    name: str,
    element_name: str,
    target: str,
    size: int | str = 4,
    *,
    semantics: str | None = "FIXED-SIZE",
    handling: str | None = None,
) -> str:
    extra = f"<ARRAY-SIZE>{size}</ARRAY-SIZE>"
    if handling is not None:
        extra += f"<ARRAY-SIZE-HANDLING>{handling}</ARRAY-SIZE-HANDLING>"
    if semantics is not None:
        extra += f"<ARRAY-SIZE-SEMANTICS>{semantics}</ARRAY-SIZE-SEMANTICS>"
    return structure(name, sub(element_name, target, extra=extra), category="ARRAY")


def pointer(name: str, target_props: str) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>DATA_REFERENCE</CATEGORY>"
        + props(f"<SW-POINTER-TARGET-PROPS>{target_props}</SW-POINTER-TARGET-PROPS>")
        + "</IMPLEMENTATION-DATA-TYPE>\n"
    )


def pointer_to(name: str, target: str, dest: str = IMPL) -> str:
    """A pointer whose target is the named implementation type *target* -- the measured shape."""
    return pointer(name, "<TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>" + props(impl_ref(target, dest)))


def other_category(name: str, category: str) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>"
        "</IMPLEMENTATION-DATA-TYPE>\n"
    )


# --- application side ---------------------------------------------------------


def app_value(name: str) -> str:
    """A bare application primitive: no unit, no compu method, no constraint."""
    return f"<{PRIMITIVE}><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY></{PRIMITIVE}>\n"


def app_enum(name: str, compu: str) -> str:
    return (
        f"<{PRIMITIVE}><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        + props(f'<COMPU-METHOD-REF DEST="COMPU-METHOD">{compu}</COMPU-METHOD-REF>')
        + f"</{PRIMITIVE}>\n"
    )


def texttable(name: str, *literals: tuple[str, int]) -> str:
    scales = "".join(
        f"<COMPU-SCALE><LOWER-LIMIT>{v}</LOWER-LIMIT><UPPER-LIMIT>{v}</UPPER-LIMIT>"
        f"<COMPU-CONST><VT>{literal}</VT></COMPU-CONST></COMPU-SCALE>"
        for literal, v in literals
    )
    return (
        f"<COMPU-METHOD><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
        f"<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>{scales}</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>\n"
    )


def app_record(name: str, *fields: tuple[str, str, str]) -> str:
    """An application record; each field is ``(name, type path, DEST)``."""
    elements = "".join(
        f"<APPLICATION-RECORD-ELEMENT><SHORT-NAME>{field}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        f'<TYPE-TREF DEST="{dest}">{path}</TYPE-TREF></APPLICATION-RECORD-ELEMENT>'
        for field, path, dest in fields
    )
    return (
        f"<{RECORD}><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
        f"<ELEMENTS>{elements}</ELEMENTS></{RECORD}>\n"
    )


def app_array(name: str, element: str, path: str, dest: str, count: int) -> str:
    return (
        f"<{ARRAY}><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>ARRAY</CATEGORY><ELEMENT>"
        f'<SHORT-NAME>{element}</SHORT-NAME><CATEGORY>VALUE</CATEGORY><TYPE-TREF DEST="{dest}">{path}</TYPE-TREF>'
        f"<MAX-NUMBER-OF-ELEMENTS>{count}</MAX-NUMBER-OF-ELEMENTS></ELEMENT></{ARRAY}>\n"
    )


def mapping_set(name: str, *maps: tuple[str, str, str]) -> str:
    """A ``DATA-TYPE-MAPPING-SET``; each map is ``(application path, its DEST, implementation path)``."""
    body = "".join(
        f'<DATA-TYPE-MAP><APPLICATION-DATA-TYPE-REF DEST="{dest}">{application}</APPLICATION-DATA-TYPE-REF>'
        f"{impl_ref(implementation)}</DATA-TYPE-MAP>"
        for application, dest, implementation in maps
    )
    return (
        f"<DATA-TYPE-MAPPING-SET><SHORT-NAME>{name}</SHORT-NAME>"
        f"<DATA-TYPE-MAPS>{body}</DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>\n"
    )


# --- interfaces, constants, components ---------------------------------------


def numerical(v: int) -> str:
    return f"<NUMERICAL-VALUE-SPECIFICATION><VALUE>{v}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"


def record_value(*members: str) -> str:
    return "<RECORD-VALUE-SPECIFICATION><FIELDS>" + "".join(members) + "</FIELDS></RECORD-VALUE-SPECIFICATION>"


def array_value(*members: str) -> str:
    return "<ARRAY-VALUE-SPECIFICATION><ELEMENTS>" + "".join(members) + "</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"


def constant(name: str, value_spec: str) -> str:
    return (
        f"<CONSTANT-SPECIFICATION><SHORT-NAME>{name}</SHORT-NAME>"
        f"<VALUE-SPEC>{value_spec}</VALUE-SPEC></CONSTANT-SPECIFICATION>\n"
    )


def sr_interface(name: str, *elements: tuple[str, str, str | None]) -> str:
    """A sender-receiver interface; each element is ``(name, type path, constant path or None)``."""
    body = ""
    for element, type_path, init in elements:
        body += (
            f"<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>{element}</SHORT-NAME>"
            f'<TYPE-TREF DEST="{IMPL}">{type_path}</TYPE-TREF>'
        )
        if init is not None:
            body += (
                "<INIT-VALUE><CONSTANT-REFERENCE>"
                f'<CONSTANT-REF DEST="CONSTANT-SPECIFICATION">{init}</CONSTANT-REF>'
                "</CONSTANT-REFERENCE></INIT-VALUE>"
            )
        body += "</VARIABLE-DATA-PROTOTYPE>"
    return (
        f"<SENDER-RECEIVER-INTERFACE><SHORT-NAME>{name}</SHORT-NAME><IS-SERVICE>false</IS-SERVICE>"
        f"<DATA-ELEMENTS>{body}</DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>\n"
    )


def cs_interface(name: str, operation: str, *arguments: tuple[str, str, str]) -> str:
    """A client-server interface of one operation; each argument is ``(name, type path, direction)``."""
    body = "".join(
        f'<ARGUMENT-DATA-PROTOTYPE><SHORT-NAME>{argument}</SHORT-NAME><TYPE-TREF DEST="{IMPL}">{type_path}</TYPE-TREF>'
        f"<DIRECTION>{direction}</DIRECTION></ARGUMENT-DATA-PROTOTYPE>"
        for argument, type_path, direction in arguments
    )
    return (
        f"<CLIENT-SERVER-INTERFACE><SHORT-NAME>{name}</SHORT-NAME><IS-SERVICE>false</IS-SERVICE><OPERATIONS>"
        f"<CLIENT-SERVER-OPERATION><SHORT-NAME>{operation}</SHORT-NAME><ARGUMENTS>{body}</ARGUMENTS>"
        "</CLIENT-SERVER-OPERATION></OPERATIONS></CLIENT-SERVER-INTERFACE>\n"
    )


def component(name: str, *ports: tuple[str, str, str, str], mapping_sets: tuple[str, ...] = ()) -> str:
    """An application component; each port is ``(P or R, name, interface path, interface DEST)``.

    Its behaviour states ``NO-SUPPORT`` for the termination handling: a component without a
    value is refused, and no case here is about the absence.
    """
    body = ""
    for direction, port, interface, dest in ports:
        if direction == "P":
            body += (
                f"<P-PORT-PROTOTYPE><SHORT-NAME>{port}</SHORT-NAME>"
                f'<PROVIDED-INTERFACE-TREF DEST="{dest}">{interface}</PROVIDED-INTERFACE-TREF></P-PORT-PROTOTYPE>'
            )
        else:
            body += (
                f"<R-PORT-PROTOTYPE><SHORT-NAME>{port}</SHORT-NAME>"
                f'<REQUIRED-INTERFACE-TREF DEST="{dest}">{interface}</REQUIRED-INTERFACE-TREF></R-PORT-PROTOTYPE>'
            )
    refs = "".join(
        f'<DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">{ref}</DATA-TYPE-MAPPING-REF>' for ref in mapping_sets
    )
    behaviour = (
        f"<INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR><SHORT-NAME>{name}_InternalBehavior</SHORT-NAME>"
        + (f"<DATA-TYPE-MAPPING-REFS>{refs}</DATA-TYPE-MAPPING-REFS>" if refs else "")
        + "<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>"
        + "</SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS>"
    )
    return (
        f"<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME><PORTS>{body}</PORTS>{behaviour}"
        "</APPLICATION-SW-COMPONENT-TYPE>\n"
    )


# --- the export every control starts from -------------------------------------


def base_types() -> tuple[str, ...]:
    """Two base types and one value type over each."""
    return (base("uint8", 8), base("uint16", 16), value("u8", "uint8"), value("u16", "uint16"))


def composites() -> tuple[str, ...]:
    """The measured implementation shapes.

    A structure, a fixed-size array, a pointer, a pointer to that pointer through the named type, and an alias of the
    structure.

    ``Frame`` and ``Samples`` share their ``SHORT-NAME`` with an application record and
    an application array the mapping set pairs them with (one name in two layers, each
    element kept under it);
    ``WheelImpl`` pairs with ``Wheel`` under another name.
    """
    return (
        structure("Frame", sub("speed", f"{TYPES}/u16"), sub("flags", f"{TYPES}/u8")),
        array("Samples", "Element", f"{TYPES}/u16", 4),
        structure("WheelImpl", sub("samples", f"{TYPES}/Samples")),
        pointer_to("FramePtr", f"{TYPES}/Frame"),
        pointer_to("FramePtrPtr", f"{TYPES}/FramePtr"),
        alias("FrameAlias", f"{TYPES}/Frame"),
    )


def application_types() -> tuple[str, ...]:
    """``Wheel`` is declared BEFORE the application array its one field names."""
    return (
        app_value("Speed"),
        app_value("Flags"),
        app_record("Wheel", ("samples", f"{APP}/Samples", ARRAY)),
        app_record("Frame", ("speed", f"{APP}/Speed", PRIMITIVE), ("flags", f"{APP}/Flags", PRIMITIVE)),
        app_array("Samples", "sample", f"{APP}/Speed", PRIMITIVE, 4),
    )


def maps() -> tuple[str, ...]:
    return (
        mapping_set(
            "Maps",
            (f"{APP}/Speed", PRIMITIVE, f"{TYPES}/u16"),
            (f"{APP}/Flags", PRIMITIVE, f"{TYPES}/u8"),
            (f"{APP}/Frame", RECORD, f"{TYPES}/Frame"),
            (f"{APP}/Samples", ARRAY, f"{TYPES}/Samples"),
            (f"{APP}/Wheel", RECORD, f"{TYPES}/WheelImpl"),
        ),
    )


def interfaces() -> tuple[str, ...]:
    return (
        sr_interface(
            "IFrame",
            ("frame", f"{TYPES}/Frame", f"{CONSTANTS}/CFrame"),
            ("samples", f"{TYPES}/Samples", f"{CONSTANTS}/CSamples"),
            ("aliased", f"{TYPES}/FrameAlias", f"{CONSTANTS}/CAliased"),
        ),
        cs_interface("IFrameService", "Get", ("frame", f"{TYPES}/FramePtr", "OUT")),
    )


def constants() -> tuple[str, ...]:
    return (
        constant("CFrame", record_value(numerical(3), numerical(1))),
        constant("CSamples", array_value(numerical(1), numerical(2), numerical(3), numerical(4))),
        constant("CAliased", record_value(numerical(5), numerical(0))),
    )


def components() -> tuple[str, ...]:
    return (
        component(
            "Ctrl",
            ("P", "pFrame", f"{INTERFACES}/IFrame", "SENDER-RECEIVER-INTERFACE"),
            ("R", "rService", f"{INTERFACES}/IFrameService", "CLIENT-SERVER-INTERFACE"),
            mapping_sets=(f"{MAPS}/Maps",),
        ),
    )


def the_export(
    *,
    types: tuple[str, ...] | None = None,
    app: tuple[str, ...] | None = None,
    mapping: tuple[str, ...] | None = None,
    ifaces: tuple[str, ...] | None = None,
    consts: tuple[str, ...] | None = None,
    comps: tuple[str, ...] | None = None,
    extra_packages: tuple[str, ...] = (),
) -> str:
    """The whole synthetic export, each package replaceable by a control."""
    return document(
        acme(
            Types=(*base_types(), *composites()) if types is None else types,
            AppTypes=application_types() if app is None else app,
            Maps=maps() if mapping is None else mapping,
            Interfaces=interfaces() if ifaces is None else ifaces,
            Constants=constants() if consts is None else consts,
            Components=components() if comps is None else comps,
        ),
        *extra_packages,
    )
