"""The synthetic sixteenth layer, bound to a test-local class — shared by the kernel suites.

``model_contract_fixtures.merged_contract`` adds ``layer:synthetic`` to an
in-memory copy of the shipped contract and nothing binds
``kind:synthetic.synthetic`` to a class, so a kernel refuses the merged contract
until :func:`merged_binding` merges the binding too.

The bound class lives in a test module because ``test_model_contract_extension.py``
asserts that no production source tree names the fixture. The binding names it by
the bare module name ``"synthetic_support"``, which resolves because the pytest
``pythonpath = ["tests/support"]`` setting in ``packages/tools/pyproject.toml`` puts this
directory on ``sys.path``.
"""

from __future__ import annotations

import copy
import dataclasses
import types
import typing
from typing import Annotated, Any

from model_contract_fixtures import load_synthetic_layer, merged_contract
from swcraft_tools.contract import load_binding
from swcraft_tools.differ import LayerDiff, ModelDiff
from swcraft_tools.model_registry import ModelRegistry, build_model_registry
from swcraft_tools.schema import ModelElement
from swcraft_tools.snapshot import ModelSnapshot


class SyntheticElement(ModelElement):
    """The sixteenth layer's element kind, declared in a **test** module.

    The three fields are the fixture's. ``title`` has a default although the
    contract declares it required: the kernel resolves a binding but does not check
    a bound class against the field declarations (the conformance gate does, for
    the shipped layers only).
    """

    name: str
    title: str = ""
    composition: str


def merged_binding() -> dict[str, Any]:
    """The shipped binding with ``kind:synthetic.synthetic`` bound to :class:`SyntheticElement`.

    Deep-copied, in memory only, so a caller mutating the result cannot reach the
    document a later :func:`~swcraft_tools.contract.load_binding` returns. No
    ``snapshotAttributes`` row is added, deliberately: the sixteenth layer is the
    case a layer with no adapter field has to be proven for.
    """
    binding = copy.deepcopy(load_binding())
    binding["kinds"]["kind:synthetic.synthetic"] = {
        "module": "synthetic_support",
        "class": "SyntheticElement",
    }
    return binding


def merged_registry() -> ModelRegistry:
    """A kernel registry over the merged contract and the merged binding."""
    return build_model_registry(contract=merged_contract(), binding=merged_binding())


# ---------------------------------------------------------------------------
# Layers stored in a two-segment directory
# ---------------------------------------------------------------------------


def nested_layer(name: str, directory: str) -> dict[str, Any]:
    """The synthetic layer re-keyed as layer *name*, stored in *directory*.

    Every identifier the registry indexes by is rewritten, so the copy shares only
    its shape with the fixture layer.
    """
    (source,) = load_synthetic_layer()["layers"]
    layer = copy.deepcopy(source)
    layer["id"] = f"layer:{name}"
    layer["name"] = name
    layer["token"] = name
    layer["storage"]["directory"] = directory
    (kind,) = layer["kinds"]
    kind["kind"] = name
    kind["id"] = f"kind:{name}.{name}"
    for field in kind["fields"]:
        field["id"] = f"field:{name}.{name}.{field['name']}"
    return layer


def nested_registry(*directories: tuple[str, str]) -> ModelRegistry:
    """A kernel registry over the merged contract plus one layer per ``(name, directory)``.

    The merged contract already holds the flat ``synthetic`` layer, stored in
    ``synthetic``; each pair adds a layer of the fixture's shape in a directory
    of one or two segments.
    """
    contract = merged_contract()
    binding = merged_binding()
    for name, directory in directories:
        contract["layers"].append(nested_layer(name, directory))
        binding["kinds"][f"kind:{name}.{name}"] = {"module": "synthetic_support", "class": _nested_class(name)}
    return build_model_registry(contract=contract, binding=binding)


def _nested_class(name: str) -> str:
    """The name of a :class:`SyntheticElement` subclass of this module, bound to layer *name*'s kind.

    Kept as a module attribute so the binding can name it: the editing projection
    keys kind names by class, so two kinds bound to one class would be one kind.
    """
    class_name = "Nested" + "".join(part.capitalize() for part in name.split("_")) + "Element"
    if class_name not in globals():
        globals()[class_name] = type(class_name, (SyntheticElement,), {"__module__": __name__})
    return class_name


#: One flat layer and two nested sibling layers in ``nest/``; no directory is a
#: prefix of another, so the registry is legal under the directory policy (equal,
#: prefix, case-only).
SIBLING_NESTED_LAYERS = (("nest_alpha", "nest/alpha"), ("nest_beta", "nest/beta"))

#: The flat ``synthetic`` layer and a layer stored INSIDE its directory. The
#: directory policy's prefix check refuses it at build: the tests on it assert that
#: refusal, and the longest match is asserted over
#: ``model_registry.longest_directory_match`` itself.
OVERLAPPING_NESTED_LAYERS = (("synthetic_alpha", "synthetic/alpha"),)


# ---------------------------------------------------------------------------
# The adapters against the binding
# ---------------------------------------------------------------------------


def _strip_annotated(annotation: Any) -> Any:
    """*annotation* without an ``Annotated`` wrapper, as ``typing.get_type_hints`` resolves it by default."""
    if typing.get_origin(annotation) is Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def _spell(annotation: Any) -> str:
    """*annotation* as a source spelling: ``list[A | B]``, ``dict[str, str]``, ``LayerDiff``."""
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_spell(argument) for argument in arguments)
    if origin is not None:
        return f"{origin.__name__}[{', '.join(_spell(argument) for argument in arguments)}]"
    return getattr(annotation, "__name__", repr(annotation))


def _expected_fields(cls: type, registry: ModelRegistry) -> list[tuple[str, Any]]:
    """``(field name, resolved type)`` per field *registry*'s binding gives *cls*, in order."""
    layers = [registry.layer(name) for name in registry.adapter_order]
    if cls is ModelDiff:
        return [(facts.snapshot_attribute, LayerDiff) for facts in layers if facts.snapshot_attribute is not None]
    if cls is ModelSnapshot:
        return [
            *(
                (facts.snapshot_attribute, list[_strip_annotated(facts.element_annotation)])
                for facts in layers
                if facts.snapshot_attribute is not None
            ),
            ("swc_placement", dict[str, dict[str, str]]),
        ]
    raise ValueError(f"{cls.__name__} is not one of the two adapters")


def adapter_drift(cls: type, registry: ModelRegistry) -> list[str]:
    """Every disagreement between adapter *cls* (``ModelSnapshot`` or ``ModelDiff``) and *registry*'s binding.

    One line per disagreement, naming the edit that removes it: a missing field, an
    extra field, the order of common fields, and a field's type (resolved by
    ``typing.get_type_hints``, ``Annotated`` stripped) against the layer's element
    annotation (``LayerDiff`` for the diff). Defaults, ``field(...)`` options and
    methods are not compared.
    """
    expected = _expected_fields(cls, registry)
    expected_names = [name for name, _ in expected]
    actual_names = [f.name for f in dataclasses.fields(cls)]
    hints = typing.get_type_hints(cls)
    drift: list[str] = []
    for index, (name, annotation) in enumerate(expected):
        if name not in actual_names:
            where = f"after '{expected_names[index - 1]}'" if index else "first"
            drift.append(f"{cls.__name__}: field '{name}' missing; add '{name}: {_spell(annotation)}' {where}")
        elif hints[name] != annotation:
            drift.append(f"{cls.__name__}: field '{name}' is '{_spell(hints[name])}'; retype it '{_spell(annotation)}'")
    drift.extend(
        f"{cls.__name__}: field '{name}' is not in the binding; remove it"
        for name in actual_names
        if name not in expected_names
    )
    common_expected = [name for name in expected_names if name in actual_names]
    common_actual = [name for name in actual_names if name in expected_names]
    drift.extend(
        f"{cls.__name__}: field '{name}' is at position {common_actual.index(name)}; "
        f"the binding puts it at position {position}"
        for position, name in enumerate(common_expected)
        if common_actual[position] != name
    )
    return drift
