"""The hand-built behaviours the behaviour-loss suites share.

``test_reader_behaviour_losses.py`` (the reader's closed tables) and
``test_behaviour_losses_import.py`` (the report and ``import component``'s abort)
both start from ``minimal_swc.arxml``'s writer-shaped behaviour and add to it:
the fixture's paths, the table rows, the XML builders and the one reason sentence
an unread item carries live here, so neither suite imports the other.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from swcraft_tools.arxml_version import NAMESPACE

#: The AUTOSAR namespace as an ElementTree tag prefix (``{uri}``).
NS = f"{{{NAMESPACE}}}"

#: The writer-shaped reader fixture every hand-built behaviour starts from. It
#: reads with no skip entry at all, which the reader suite's first test holds.
MINIMAL_SWC = Path(__file__).resolve().parents[1] / "fixtures" / "arxml" / "minimal_swc.arxml"
SWC_PATH = "/Acme/Chassis/Components/WheelSpeedSensorSwc"
BEHAVIOUR = f"{SWC_PATH}/WheelSpeedSensorSwc_InternalBehavior"
RUNNABLE = f"{BEHAVIOUR}/WheelSpeedSensorRun"

#: ``(owner, container, item tag, named)`` -- every container row of the measurement
#: table that is not in the compared group. ``named`` items carry a ``SHORT-NAME`` and are reported at their
#: own path; the others are references and are reported at the parent's. The
#: containers the reader reads are not rows: the inter-runnable variable wrappers,
#: the local-variable containers (``test_inter_runnable_variable_import.py``),
#: ``EXCLUSIVE-AREAS`` and its reference wrappers (``test_exclusive_area_import.py``),
#: ``DATA-READ-ACCESSS``, ``DATA-WRITE-ACCESSS`` and the result points' wrapper
#: (``test_access_forms_import.py``).
CONTAINER_ROWS = [
    ("behaviour", "AR-TYPED-PER-INSTANCE-MEMORYS", "VARIABLE-DATA-PROTOTYPE", True),
    ("behaviour", "PER-INSTANCE-PARAMETERS", "PARAMETER-DATA-PROTOTYPE", True),
    ("behaviour", "SERVICE-DEPENDENCYS", "SWC-SERVICE-DEPENDENCY", True),
    ("behaviour", "INCLUDED-DATA-TYPE-SETS", "INCLUDED-DATA-TYPE-SET", False),
    ("runnable", "PARAMETER-ACCESSS", "PARAMETER-ACCESS", True),
    ("runnable", "MODE-ACCESS-POINTS", "MODE-ACCESS-POINT", False),
    ("runnable", "MODE-SWITCH-POINTS", "MODE-SWITCH-POINT", True),
]


def unread_reason(container: str, parent_tag: str) -> str:
    """The reason of an item the closed table of *parent_tag* does not read, for its *container*."""
    return (
        f"{container} of a {parent_tag} is not read by SWCraft -- no SWCraft representation, not imported or preserved"
    )


def snippet(xml: str) -> list[etree._Element]:
    """The elements of *xml*, parsed in the AUTOSAR namespace."""
    wrapper = etree.fromstring(f'<W xmlns="{NAMESPACE}">{xml}</W>'.encode())
    return list(wrapper)


def add_xml(parent: etree._Element, xml: str) -> None:
    """Append the elements of *xml* to *parent*."""
    for element in snippet(xml):
        parent.append(element)


def items_xml(tag: str, named: bool, prefix: str) -> str:
    """Two *tag* items: named ``{prefix}1``/``{prefix}2``, or two unnamed ones, each holding such a path as text.

    The unnamed items name no element the reader reads (not an exclusive area, which
    it does read), so each stays an unread item of its own kind.
    """
    if named:
        return "".join(f"<{tag}><SHORT-NAME>{prefix}{index}</SHORT-NAME></{tag}>" for index in (1, 2))
    return "".join(f"<{tag}>{BEHAVIOUR}/{prefix}{index}</{tag}>" for index in (1, 2))


def option_xml(port: str = "pWheelSpeed", dest: str = "P-PORT-PROTOTYPE", extra: str = "", flags: bool = True) -> str:
    """One ``PORT-API-OPTION`` naming *port* of the fixture's component.

    Both flags are false (what no option states) unless told otherwise."""
    flag_xml = "<ENABLE-TAKE-ADDRESS>false</ENABLE-TAKE-ADDRESS><INDIRECT-API>false</INDIRECT-API>" if flags else ""
    return f'<PORT-API-OPTION>{flag_xml}<PORT-REF DEST="{dest}">{SWC_PATH}/{port}</PORT-REF>{extra}</PORT-API-OPTION>'
