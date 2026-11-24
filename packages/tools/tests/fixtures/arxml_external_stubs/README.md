# `arxml_external_stubs/` — references to elements no file declares

Every `.arxml` file here is **synthetic**: written by hand, with invented names only (packages such
as `/Remote` and `/Partner` stand for the files of the same delivery that would declare them), and
no content from any real export: nothing is copied. Each file opens with a comment listing what it mints as an
external stub and what it must not.

| File | What it holds |
|---|---|
| `stub_roles.arxml` | every role an absent target may be imported as a stub in, beside the shapes that mint none |
| `stub_members.arxml` | every construct naming a member of an interface the file does not declare, and every reason one is not listed |
| `dest_disagreement.arxml` | absent paths whose references disagree on `DEST`, across one owner and within it, and one that agrees |

Read by `test_external_stubs_import.py`, `test_external_stubs_report.py`, `test_arxml_import_cli.py`
and `test_arxml_xsd_conformance.py`.
