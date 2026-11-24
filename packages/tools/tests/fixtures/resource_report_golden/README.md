# `resource-report` golden — a byte-identity oracle

One file, `basic-before.txt`, captured from `resource_report(...).render()` over
`examples/basic` before the analysis was moved onto the declared elements (product
`0.5.3`, contract `2.0.1`).

The analysis computes over the declared `Core`
and `Memory` elements themselves, with no shim between it and the model
(`_budgets_from_hardware`, `_CoreBudget` and `_MemoryBudget` are gone). That is a change of *what the arithmetic reads*
and not of *what it produces*, and `basic-before.txt` is the statement of it: `resource-report` is
byte-identical where the semantics did not change, and `test_resource_report.py` reads that file
rather than a paragraph of prose.

**The first line is deliberately not here.** `resource_report.py::ResourceReport.render` opens the
report with `Resource utilization report: <subject>`, and without a `subject` that is the model root it
was given — an absolute path that differs per checkout, so a byte-for-byte golden of line 1 could never
match twice. `cli/read_commands.py::cmd_resource_report` passes `subject=project.name`, so the command's report is
headed by the project's name and never by where the project is stored. The fixture holds the report
**from line 2 on**; the header line is asserted separately, by shape.

| Golden | Produced by |
|---|---|
| `basic-before.txt` | `"\n".join(resource_report(root).render().split("\n")[1:]) + "\n"` |

What it does **not** reach, stated because a completeness claim must: it is one model
(`examples/basic`) on one host, rendered by one renderer. It covers `render()`'s text only — not
the report's Markdown file (`report_markdown`) nor the build report's budget rows — and nothing
about a model that produces a topology finding — the one admitted difference is the topology section the CLI composes for such a
model, which a clean model does not produce.

**Never hand-edited, and never regenerated to make a test go away** — an unexplained diff is the
finding, because the finding is exactly what this file exists to catch. Regenerate only after an
intentional rendering change has been accepted and recorded in the change's plan first, and only
through the renderer:

```python
from pathlib import Path

from swcraft_tools.project import require_project
from swcraft_tools.resource_report import resource_report

project = require_project("examples/basic/swcraft.yaml")
body = "\n".join(resource_report(project.resolved_model_root).render().split("\n")[1:])
with Path("basic-before.txt").open("w", encoding="utf-8", newline="\n") as handle:
    handle.write(body + "\n")
```

Editing `examples/basic` invalidates the golden for the same reason the differ goldens are
invalidated by editing their fixture pair: what it records is one fixed model's rendering, so a
changed model is a different record and no longer evidence about the change made under it.
