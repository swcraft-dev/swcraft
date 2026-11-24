# `imports/` — the component file task 10 imports

`BrakeLightController.arxml` is **synthetic**: SWCraft's own output, written by
`swcraft generate arxml --component` from a scratch copy of the bench with the component added. Its
names come from the bench model, which is the showcase's synthetic model; there is no content from any
real export.

`test_agent_task_check.py` imports it into a copy of the bench. `.gitignore` negates this folder by
name, because `*.arxml` is ignored at the root.
