# The requirements team's Sphinx-Needs project, in its own vocabulary, loading SWCraft's needs.json.
project = "Brake system requirements"
version = "SYS-2.1"
extensions = ["sphinx_needs"]

# --- SWCraft's architecture: the need types, fields and link types its needs.json uses ---
needs_types = [
    # ... the project's own types (a project on the defaults lists req, spec, impl, test, need) ...
    {"directive": "arch_req", "title": "Architecture requirement", "prefix": "AR_"},
    {"directive": "arch_spec", "title": "Architecture specification", "prefix": "AS_"},
    {"directive": "arch_element", "title": "Architecture element", "prefix": "AE_"},
]
needs_fields = {
    "asil": {"description": "The safety integrity level allocated to the requirement.", "nullable": True},
    "swcraft_path": {
        "description": "The file in the SWCraft model that declares the need.", "nullable": True,
        "parse_dynamic_functions": False,
    },
}
needs_links = {
    "satisfies": {
        "outgoing": "satisfies", "incoming": "satisfied by",
        "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "satisfies_unconfirmed": {
        "outgoing": "satisfies (unconfirmed)", "incoming": "satisfied by (unconfirmed)",
        "allow_dead_links": True, "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "derived_from": {
        "outgoing": "derived from", "incoming": "derives",
        "parse_conditions": False, "parse_dynamic_functions": False,
    },
    "derived_from_unconfirmed": {
        "outgoing": "derived from (unconfirmed)", "incoming": "derives (unconfirmed)",
        "allow_dead_links": True, "parse_conditions": False, "parse_dynamic_functions": False,
    },
}
needs_external_needs = [
    {
        "base_url": "",
        "target_url": "https://git.example.com/our-org/architecture/src/branch/main/model/{{need.swcraft_path | urlencode}}",
        "json_path": "swcraft/needs.json",
        "css_class": "swcraft",
    },
]
# --- end of SWCraft's declarations ---

# The team's own vocabulary, beside SWCraft's: its type, its parent link and its level field.
needs_types.insert(
    0, {"directive": "sysreq", "title": "System Requirement", "prefix": "SYS_", "color": "#BFD8D2", "style": "node"}
)
needs_fields["safety"] = {"description": "The ASIL allocated to the requirement.", "nullable": True}
# The team's ids may hold `[` or `[[` (its requirements tool writes `SYS_REQ[12]`), so its link reads an id whole.
needs_links["parent"] = {
    "description": "The requirement this one derives from.",
    "parse_conditions": False,
    "parse_dynamic_functions": False,
}
# No timestamps, so a rebuild of the same sources writes the same file.
needs_reproducible_json = True
