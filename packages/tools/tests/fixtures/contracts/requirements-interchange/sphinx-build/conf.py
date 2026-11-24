# A requirements team's Sphinx-Needs project, in its own vocabulary: the source of needs.json.
project = "Brake system requirements"
version = "SYS-2.1"
extensions = ["sphinx_needs"]

needs_types = [
    {"directive": "sysreq", "title": "System Requirement", "prefix": "SYS_", "color": "#BFD8D2", "style": "node"},
    {"directive": "swreq", "title": "Software Requirement", "prefix": "SW_", "color": "#FEDCD2", "style": "node"},
    {"directive": "test", "title": "Test Case", "prefix": "TC_", "color": "#DCB239", "style": "node"},
]
needs_fields = {
    "safety": {"description": "The ASIL allocated to the requirement.", "nullable": True},
    "rev": {"description": "The requirements tool's own revision.", "nullable": True},
    "reviewer": {"description": "Who reviewed the requirement.", "nullable": True},
}
needs_links = {
    "parent": {"description": "The requirement this one derives from."},
    "verifies": {"description": "The requirement a test verifies."},
}
# No timestamps, so a rebuild of the same sources writes the same file.
needs_reproducible_json = True
needs_build_json = True
