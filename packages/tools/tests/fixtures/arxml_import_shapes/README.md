# `arxml_import_shapes/` — one import decision per file

Every `.arxml` file here is **synthetic**: written by hand, with invented names only, and no content
from any real export: nothing is copied. Each file is the smallest export that reaches one decision of the
importer — a duplicate merged or refused, a twin collapsed or kept, a mapping set read or skipped, a
reference left unresolved — and opens with a comment naming that decision and why the file is
shaped the way it is.

The file name is the shape. `arxml_import_shapes_support.py` loads them, and the suites named
`test_arxml_import_*.py` hold each one to its verdict. `test_arxml_import_cli.py` imports every file
but `cyclic_composition_graph.arxml`, which the import refuses by design.
