# Allocation report golden — `report.md` without links

One file, `report_without_links.txt`: `allocation.render_markdown(document)` with no
`ReportLinks`, for the `zero_rule()` fixture of `packages/tools/tests/support/carma_models.py` run with
`locked=["Y", "W"], max_moves=2, candidates=1` (`test_allocation_search.py::TestTheReportLinks`).
It was captured at `e2c4a4c6`, after the report gained its links, so that "without links the
report is the plain text" is compared with a committed text and not only with the same renderer's
second call.

The solver line reads `- Solver: SciPy <version>`: the test puts the installed version in its
place before comparing, because the version is the host's and not the renderer's. The model digest
and the SWCraft version are the test helper's fixed `sha256:0` and `0`.

What it does **not** reach: it is one document of one fixture, rendered on one host. It is a change
detector and never a correctness proof — the text it records was checked by reading it, not by an
independent oracle. **Never regenerated to make a test go away**: regenerate it only after an
intentional rendering change, by writing `render_markdown(document)` for that document with
`encoding="utf-8"` and `newline="\n"` and restoring the placeholder in the solver line.
