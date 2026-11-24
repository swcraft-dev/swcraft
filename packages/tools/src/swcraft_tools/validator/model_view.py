"""The one edge between the validator's checks and the model's storage.

Holds :class:`ModelView`, through which every check reads the model, and :func:`as_view`. A view
reads each layer on its first access and holds it, so one view handed to every check reads each
layer once.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias, TypeVar

from swcraft_tools.data_description import DataDescription, read_data_description
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import declared_identity, scan_layer
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_hardware,
    read_interfaces,
    read_mapping_sets,
    read_requirements,
    read_sequences,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.schema import (
    Composition,
    Connector,
    Hardware,
    Interface,
    MappingSet,
    Requirement,
    SWComponent,
    Task,
)
from swcraft_tools.schema import Sequence as ModelSequence

_T = TypeVar("_T")


class ModelView:
    """The layers of the model under one root, each read on first use and held for the view's life.

    Each accessor returns a new list over the held elements, so a caller may sort or extend it;
    the elements themselves are shared and must not be mutated.

    A read that raises is not held: the next access raises again. A missing root raises
    :class:`~swcraft_tools.model_state.ModelRootNotFoundError` on the first :attr:`swcs` access,
    a malformed file the reader's own error on the first access to its layer.

    Args:
        model_root: The model's root directory, as a string or a path.
    """

    def __init__(self, model_root: str | os.PathLike[str]) -> None:
        self._model_root = os.fspath(model_root)
        self._layers: dict[str, list[Any]] = {}
        self._data_description: DataDescription | None = None

    def _layer(self, layer: str, read: Callable[[], list[_T]]) -> list[_T]:
        """The held elements of *layer* in a new list, reading them with *read* on the first call."""
        if layer not in self._layers:
            self._layers[layer] = read()
        return list(self._layers[layer])

    @property
    def model_root(self) -> str:
        """The root this view reads, as it was given (``os.fspath`` of it); no message may name it."""
        return self._model_root

    @property
    def swcs(self) -> list[SWComponent]:
        """The ``swc`` layer's components."""
        return self._layer("swc", lambda: read_swc_model(self._model_root))

    @property
    def interfaces(self) -> list[Interface]:
        """The ``interfaces`` layer's elements."""
        return self._layer("interfaces", lambda: read_interfaces(self._model_root))

    @property
    def requirements(self) -> list[Requirement]:
        """The ``requirements`` layer's elements."""
        return self._layer("requirements", lambda: read_requirements(self._model_root))

    @property
    def compositions(self) -> list[Composition]:
        """The ``compositions`` layer's elements."""
        return self._layer("compositions", lambda: read_compositions(self._model_root))

    @property
    def connectors(self) -> list[Connector]:
        """The ``connectors`` layer's elements."""
        return self._layer("connectors", lambda: read_connectors(self._model_root))

    @property
    def tasks(self) -> list[Task]:
        """The ``tasks`` layer's elements."""
        return self._layer("tasks", lambda: read_tasks(self._model_root))

    @property
    def sequences(self) -> list[ModelSequence]:
        """The ``sequences`` layer's elements."""
        return self._layer("sequences", lambda: read_sequences(self._model_root))

    @property
    def hardware(self) -> list[Hardware]:
        """The ``hardware`` layer's elements."""
        return self._layer("hardware", lambda: read_hardware(self._model_root))

    @property
    def mapping_sets(self) -> list[MappingSet]:
        """The ``mapping_sets`` layer's elements."""
        return self._layer("mapping_sets", lambda: read_mapping_sets(self._model_root))

    @property
    def data_description(self) -> DataDescription:
        """The model's data description, read on the first access and the same object on every later one."""
        if self._data_description is None:
            self._data_description = read_data_description(self._model_root)
        return self._data_description

    def layer_present(self, layer: str) -> bool:
        """Whether *layer*'s directory exists under the root (asked on every call); an empty one is present."""
        return scan_layer(self._model_root, layer).present

    def stored_paths(self, layer: str) -> tuple[str, ...]:
        """Every file of *layer*, model-root-relative and POSIX-spelled, in the listing's order."""
        root = Path(self._model_root)
        return tuple(
            entry.path.relative_to(root).as_posix()
            for entry in scan_layer(root, layer, registry=current_registry()).files
        )

    def declared_identity(self, layer: str, path: str) -> str | None:
        """The identity the file at the root-relative *path* declares under *layer*'s identity field, or ``None``."""
        return declared_identity(Path(self._model_root) / path, current_registry().layer(layer).identity_field)

    def stored_text(self, layer: str, path: str) -> str:
        """The UTF-8 text of *layer*'s file at the root-relative *path*, as one of :meth:`stored_paths`."""
        del layer  # the path alone locates the file; the layer names what the caller is reading
        return (Path(self._model_root) / path).read_text(encoding="utf-8")


#: What a check accepts as its model: a view, or the root a new view is built over.
ModelInput: TypeAlias = ModelView | str | os.PathLike[str]


def as_view(model: ModelInput) -> ModelView:
    """*model* itself when it is a view, otherwise a new view over the root it names."""
    return model if isinstance(model, ModelView) else ModelView(model)
