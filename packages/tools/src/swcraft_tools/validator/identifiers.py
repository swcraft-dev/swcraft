"""The layers whose element names must be AUTOSAR Identifiers.

Holds :func:`_identifier_layer_tokens`, the contract's set of ARXML-bound layer tokens.
"""

from __future__ import annotations

from swcraft_tools.model_registry import ModelRegistry, current_registry


def _identifier_layer_tokens(registry: ModelRegistry | None = None) -> frozenset[str]:
    """The ARXML-bound layers' singular tokens, from the contract.

    A layer whose ``layerDecl.arxmlBound`` is ``true`` emits its declared names as AUTOSAR
    ``SHORT-NAME``s, so they must be legal Identifiers; the other layers emit none. A layer that
    declares nothing counts as ``false``.

    This states the scope; it does not drive it: :func:`check_identifier_conformance` reads a
    hard-coded list of layers, so the set must equal the tokens those loops report under (``swc``,
    ``connector``, ``interface``, ``data_type``, ``composition``, ``mapping_set``), and
    ``editing/diagnosis._identifier_layers`` must equal it.

    Args:
        registry: the model registry to read, or None for the one in effect (resolved in the body,
            so a swapped contract is seen).
    """
    registry = registry or current_registry()
    return frozenset(layer.token for layer in registry.layers if layer.arxml_bound)
