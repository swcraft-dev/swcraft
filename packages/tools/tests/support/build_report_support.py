"""What the build report's two suites share: a passing ARXML check and the section reader.

``test_build_report.py`` holds the module's facts and text and ``test_cli.py``
holds ``swcraft validate --report`` end to end; both read a rendered report by
its sections and both hand the run a passing check, so the two read one
document the same way.
"""

from __future__ import annotations

from swcraft_tools.arxml_validation import MODE_XSD, ValidationOutcome

#: A licensed schema's full path: the report names it by its file name alone, never by this.
SCHEMA_PATH = "/opt/licensed/schemas/AUTOSAR_4-2-2.xsd"

#: A passing check against the licensed schema.
PASSED = ValidationOutcome(valid=True, mode=MODE_XSD, schema_path=SCHEMA_PATH)


def report_section(text: str, heading: str) -> list[str]:
    """The non-empty lines under ``## <heading>``, up to the next heading or the footer's rule."""
    lines = text.splitlines()
    start = lines.index(f"## {heading}") + 1
    section: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ") or line == "---":
            break
        if line:
            section.append(line)
    return section
