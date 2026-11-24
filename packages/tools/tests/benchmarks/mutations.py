"""The diff and impact benchmarks' fixed mutation of the ``benchmark-default`` specimen.

:func:`apply_fixed_mutations` makes one add, one remove and one change on a copy of the specimen's
model, so the diff scenario has a difference to find; :data:`HUB_TYPE_NAME` is the element the impact
scenario walks from. The removed interface, the changed data type and the hub type are the specimen's
elements, each named here by its role:

- :data:`HUB_TYPE_NAME` -- the implementation type the most references name (data elements, arguments,
  mapping entries and other implementation types), ties broken by name in code-point order;
- :data:`MUTATION_REMOVED_INTERFACE_NAME` -- a client-server interface no port names, the first by name;
- :data:`MUTATION_CHANGED_DATA_TYPE_NAME` -- a value type other than the hub type, the first by name.

``test_benchmarks.py`` holds each name to its role on the taken specimen, so a regeneration that moves a
role fails there rather than as a benchmark measuring something else. The added component's name is
this test tree's own (:data:`MUTATION_ADDED_SWC_NAME`); no generator writes it, and the mutation refuses
a model that already holds it.

What this does not reach: a model other than ``benchmark-default`` -- the three names are that
specimen's, and a model lacking one of them is refused, never mutated in part.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from conftest import model_element_path, write_model_element
from swcraft_tools.model_state import load_model_state
from swcraft_tools.yaml_load import load_yaml_document

#: The specimen the benchmarks and the mutation read.
SPECIMEN = "benchmark-default"

#: The implementation type the impact scenario walks from (the role: the most referenced).
HUB_TYPE_NAME = "Vt01abcdef"

#: The component the mutation adds: a name this test tree owns, absent from the specimen.
MUTATION_ADDED_SWC_NAME = "WlMutationAddedSwc"
#: The interface the mutation removes (the role: a client-server interface no port names).
MUTATION_REMOVED_INTERFACE_NAME = "If151abcdefghijk"
#: The data type whose description the mutation changes (the role: a value type, not the hub).
MUTATION_CHANGED_DATA_TYPE_NAME = "Vt02abcdef"

#: The description the changed data type is given.
CHANGED_DESCRIPTION = "mutated for the diff/impact benchmark scenario"


def apply_fixed_mutations(model_root: Path) -> None:
    """One fixed add, remove and change applied in place to *model_root*, a copy of the specimen's model.

    - add: the portless component :data:`MUTATION_ADDED_SWC_NAME`, written into the model's one swc
      group directory, satisfying the model's first requirement by identifier at that requirement's own
      version, so the mutated model holds no dangling link;
    - remove: the interface :data:`MUTATION_REMOVED_INTERFACE_NAME`;
    - change: the description of :data:`MUTATION_CHANGED_DATA_TYPE_NAME`.

    Raises:
        FileNotFoundError: the model lacks the removed interface or the changed data type, or holds no
            requirement or not exactly one swc group directory (it is not ``benchmark-default``).
        FileExistsError: the model already holds a component named :data:`MUTATION_ADDED_SWC_NAME`.
    """
    root = Path(model_root)
    removed = model_element_path(root, MUTATION_REMOVED_INTERFACE_NAME, layer="interfaces")
    changed = model_element_path(root, MUTATION_CHANGED_DATA_TYPE_NAME, kind="value_type")
    for path in (removed, changed):
        if not path.is_file():
            raise FileNotFoundError(f"the model holds no {path.stem}: it is not the {SPECIMEN} specimen")
    groups = sorted((entry for entry in (root / "swc").iterdir() if entry.is_dir()), key=lambda p: p.as_posix())
    if len(groups) != 1:
        raise FileNotFoundError(f"the model has {len(groups)} swc group directories, not one")
    state = load_model_state(root)
    if any(swc.name == MUTATION_ADDED_SWC_NAME for swc in state["swc"]):
        raise FileExistsError(f"the model already holds a component named {MUTATION_ADDED_SWC_NAME}")
    if not state["requirements"]:
        raise FileNotFoundError("the model holds no requirement for the added component to satisfy")
    first = min(state["requirements"], key=lambda requirement: requirement.id)

    # A component's file needs its group directory, which the registry's path composition does not pick;
    # the element is written as write_model_element writes one.
    added = {
        "name": MUTATION_ADDED_SWC_NAME,
        "type": "application",
        "ports": [],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
        "satisfies": [f"{first.id}@{first.version}"],
    }
    (groups[0] / f"{MUTATION_ADDED_SWC_NAME}.yaml").write_text(
        yaml.safe_dump(added, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n"
    )
    removed.unlink()
    document = dict(load_yaml_document(changed.read_text(encoding="utf-8")))  # type: ignore[arg-type]
    document["description"] = CHANGED_DESCRIPTION
    write_model_element(root, document)
