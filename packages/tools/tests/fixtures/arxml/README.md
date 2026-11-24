# `arxml/` — the reader's and importer's hand-written exports

Every `.arxml` file here is **synthetic**: written by hand, with invented names only, and no content
from any real export: nothing is copied. Each file opens with a comment saying which shape it holds and why.

| File | What it holds |
|---|---|
| `minimal_swc.arxml` | the structural shape of a tool export: nested `AR-PACKAGES`, two-level typing, `DESC` blocks |
| `client_server.arxml` | a client-server interface with directed arguments and a runnable started by an operation event |
| `composition_nested.arxml` | two levels of composition with delegation chains across the boundaries |
| `data_types.arxml` | application and implementation types with a mapping set, and the two bare-name collisions |
| `init_values.arxml` | the four value-specification shapes and their lexical spellings |
| `unsupported_content.arxml` | the skip-and-report shapes, among them an invented vendor element (`ACME-EXPORT-METADATA`) outside the AUTOSAR schema |
| `minimal_swc_4_3_0.arxml`, `data_types_4_3_0.arxml` | the 4.3.0-labelled twins of two files above; `test_arxml_fixtures.py::TestVersionTwins` holds their content equal |

The shapes were chosen from what tool exports carry, so the files mirror a structure, never a
project: no element, package or identifier in them is taken from a real model.

`*.arxml` is git-ignored at the root; `.gitignore` negates this folder by name. `test_arxml_fixtures.py`
checks the files' shapes, and `test_arxml_xsd_conformance.py` holds them against the licensed schemas
where those are present.
