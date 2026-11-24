"""The model passport's test helpers that are not generation.

``ABSENT`` and :func:`leaf` read a dotted leaf path of a passport document; :func:`first_schema_error`
names a validator's first error by its path, the same one on every run; :data:`PASSPORT_VECTORS_DIR`
is the passport contract's vectors (``examples/`` and ``negative/``); :func:`schema_leaf_paths` is
every leaf path of the package schema -- the closed vocabulary a test or the calibration prints leaf
paths from -- and :func:`leaves_differing` compares a computed passport with a stated one on every
leaf the stated one carries.

The leaf comparison -- ``ABSENT``, :func:`leaf`, :func:`schema_leaf_paths` and :func:`leaves_differing`
-- is ``scripts/specimens.py``'s, loaded by path and named here, so the pin script and the tests compare
with one definition: a script cannot import from the test trees, so the script owns it.

What this does not reach: a leaf the stated passport leaves out (it is not compared), and the
meaning of a leaf -- the schema's words, restated by no helper here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import specimens_module

_SPECIMENS = specimens_module()

#: The passport contract's vectors (``examples/`` and ``negative/``). The schema itself is package
#: data, read through ``swcraft_tools.passport.schema_text``.
PASSPORT_VECTORS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "model-passport"

#: What :func:`leaf` answers for a path the document does not have.
ABSENT = _SPECIMENS.ABSENT
leaf = _SPECIMENS.leaf
schema_leaf_paths = _SPECIMENS.schema_leaf_paths
leaves_differing = _SPECIMENS.leaves_differing


def first_schema_error(validator: Any, document: Any) -> Any:
    """The first of *validator*'s errors for *document* by its path (a ``ValidationError``), or ``None``.

    The order is by the error's ``absolute_path`` spelled as strings, so a refusal names the same
    error on every run.
    """
    errors = sorted(validator.iter_errors(document), key=lambda e: list(map(str, e.absolute_path)))
    return errors[0] if errors else None
