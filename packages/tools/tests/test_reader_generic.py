"""``reader.py`` reads any declared layer.

The public wrappers keep their names, signatures and refusal text. The oracle for the text is
:data:`READER_LABELS`, transcribed from the wrappers' call sites before the generic reader
replaced them, so a refusal such as ``Invalid implementation type YAML in <path>`` is compared
against a frozen table rather than against itself.

What these tests do not reach: a storage record that is not one directory of one suffix (a
nesting depth beyond one, a second placement field, a suffix other than ``.yaml``), since the
contract declares none; the *writers*, which compose paths through ``model_state.py``'s
``free_storage_path``; and ``check-model``'s complaint for a missing required layer. The one legacy
behaviour asserted rather than assumed is ``read_swc_model_with_placement``'s
``FileNotFoundError`` on a missing model root: the only wrapper that raises one, and the generic
reader deliberately does not.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path, write_model_element
from swcraft_tools import reader
from swcraft_tools.model_registry import ModelRegistry, production_registry, use_registry
from swcraft_tools.model_state import scan_layer
from synthetic_support import SyntheticElement, merged_registry

#: layer name -> the wrapper whose output the generic reader must reproduce.
#: Spelled out so the *pairing* is not recomputed by the code under test. The
#: wrappers are one-line delegations, so the equality catches a wrapper that
#: stopped delegating or delegated under the wrong layer name.
READ_WRAPPERS = {
    "mapping_sets": reader.read_mapping_sets,
    "swc": reader.read_swc_model,
    "interfaces": reader.read_interfaces,
    "connectors": reader.read_connectors,
    "implementation_types": reader.read_data_types,
    "requirements": reader.read_requirements,
    "compositions": reader.read_compositions,
    "tasks": reader.read_tasks,
    "sequences": reader.read_sequences,
    "hardware": reader.read_hardware,
}

#: The layers with no ``read_*`` wrapper, read through :func:`reader.read_layer`
#: alone. Spelled out so a new layer without a wrapper is a decision written
#: here rather than a silent gap.
LAYERS_WITHOUT_A_READ_WRAPPER = frozenset(
    {"base_types", "application_types", "units", "compu_methods", "data_constraints"}
)

#: layer name -> the ``find_*_file`` wrapper for it; the other layers have none.
FIND_WRAPPERS = {
    "swc": reader.find_swc_file,
    "connectors": reader.find_connector_file,
    "implementation_types": reader.find_data_type_file,
    "requirements": reader.find_requirement_file,
    "tasks": reader.find_task_file,
    "sequences": reader.find_sequence_file,
    "hardware": reader.find_hardware_file,
}

#: layer name -> the word a refusal names it by, transcribed rather than
#: derived: the frozen oracle for the messages.
READER_LABELS = {
    "mapping_sets": "mapping set",
    "swc": "SWC",
    "interfaces": "interface",
    "connectors": "connector",
    # Contract 8.0.0 renamed the layer; its label, and the word, follow it.
    "implementation_types": "implementation type",
    "base_types": "base type",
    "application_types": "application type",
    # Each layer's token, an underscore spelled as a space.
    "units": "unit",
    "compu_methods": "compu method",
    "data_constraints": "data constraint",
    "requirements": "requirement",
    "compositions": "composition",
    "tasks": "task",
    "sequences": "sequence",
    "hardware": "hardware",
}

#: layer name -> a document that is valid YAML and invalid for that layer's
#: model, so the refusal path is the one under test rather than a syntax error.
INVALID_DOCUMENTS = {
    "mapping_sets": {"name": ""},
    "swc": {"name": "", "type": "application"},
    "interfaces": {"name": "IBad", "kind": "sender_receiver"},
    "connectors": {"name": ""},
    "implementation_types": {"name": "Bad", "kind": "base_type"},
    "base_types": {"name": "Bad", "kind": "base_type"},
    "application_types": {"name": "Bad", "kind": "enum"},
    "units": {"name": ""},
    "compu_methods": {"name": "Bad", "kind": "texttable", "entries": []},
    "data_constraints": {"name": ""},
    "requirements": {"id": "REQ_1"},
    "compositions": {"name": "Comp"},
    "tasks": {"name": "T"},
    "sequences": {"name": "s"},
    "hardware": {"name": "R"},
}

#: layer name -> the identity the ``find_*`` assertions look for in the model
#: below, under a filename that deliberately disagrees with it.
FIND_TARGETS = {
    "swc": "SpeedSensor",
    "connectors": "Speed_to_Flat",
    "implementation_types": "Speed",
    "requirements": "REQ_001",
    "tasks": "Task_10ms",
    "sequences": "speed_cycle",
    "hardware": "CORE0",
}


def test_the_hand_written_tables_name_every_declared_layer() -> None:
    """The tables' key sets are the registry's layer names, so a new layer cannot go uncovered.

    The tables are transcribed on purpose (deriving them would compare the code with
    itself), and transcription cannot notice a new layer. :data:`FIND_WRAPPERS` and
    :data:`FIND_TARGETS` cover only the layers with a ``find_*_file`` wrapper, so they
    are held to each other instead.
    """
    declared = {layer.name for layer in production_registry().layers}

    assert set(READ_WRAPPERS) | LAYERS_WITHOUT_A_READ_WRAPPER == declared
    assert not set(READ_WRAPPERS) & LAYERS_WITHOUT_A_READ_WRAPPER
    assert set(READER_LABELS) == declared
    assert set(INVALID_DOCUMENTS) == declared
    assert set(FIND_TARGETS) == set(FIND_WRAPPERS)
    assert set(FIND_WRAPPERS) <= declared


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A model with an element in each wrapped layer and a grouped, a flat and a nested SWC.

    No wrapper equality can hold because its layer happened to be empty, and the SWC layer
    carries all three placement shapes the nesting layer distinguishes.
    """
    root = tmp_path / "model"
    _write(
        root / "compositions" / "Powertrain.yaml",
        {"name": "Powertrain", "title": "Powertrain", "root": True},
    )
    _write(
        root / "swc" / "sensors" / "SpeedSensor.yaml",
        {
            "name": "SpeedSensor",
            "type": "application",
            "ports": [{"name": "pOut", "direction": "provide", "interface": "ISpeed"}],
            "runnables": [{"name": "SenseRun", "events": [{"type": "timing", "period": 0.01}]}],
            "memory": {"RAM": 512},
        },
    )
    _write(root / "swc" / "Flat.yaml", {"name": "Flat", "type": "service"})
    _write(root / "swc" / "a" / "b" / "Deep.yaml", {"name": "Deep", "type": "application"})
    _write(
        root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        },
    )
    _write(
        root / "connectors" / "Speed_to_Flat.yaml",
        {"name": "Speed_to_Flat", "from": {"swc": "SpeedSensor", "port": "pOut"}, "to": {"swc": "Flat", "port": "rIn"}},
    )
    write_model_element(root, {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
    write_model_element(root, {"name": "SpeedMode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
    # A filename that disagrees with the declared name, so `find_*` is shown to
    # be a content scan and not a filename match.
    _write(
        model_element_path(root, "renamed", kind="value_type"),
        {"name": "Speed", "kind": "value_type", "base": "float32"},
    )
    _write(
        root / "requirements" / "not-the-id.yaml",
        {
            "id": "REQ_001",
            "title": "Speed is sensed",
            "description": "The system shall sense speed.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )
    _write(
        root / "tasks" / "Task_10ms.yaml",
        {
            "name": "Task_10ms",
            "period": 0.01,
            "priority": 5,
            "partition": "PARTITION0",
            "wcet": "1200us",
            "runnables": [{"swc": "SpeedSensor", "runnable": "SenseRun"}],
        },
    )
    _write(
        root / "sequences" / "speed_cycle.yaml",
        {
            "name": "speed_cycle",
            "task": "Task_10ms",
            "title": "Speed cycle",
            "steps": [{"swc": "SpeedSensor", "runnable": "SenseRun"}],
        },
    )
    _write(root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    _write(root / "hardware" / "CORE0.yaml", {"name": "CORE0", "kind": "core", "threshold": 0.7})
    _write(
        root / "hardware" / "RAM0.yaml",
        {
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0"],
            "threshold": 0.8,
        },
    )
    return root


# ---------------------------------------------------------------------------
# The wrappers are the generic functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", sorted(READ_WRAPPERS))
def test_every_read_wrapper_returns_exactly_what_the_generic_reader_returns(model: Path, layer: str) -> None:
    assert READ_WRAPPERS[layer](str(model)) == reader.read_layer(str(model), layer)


@pytest.mark.parametrize("layer", sorted(FIND_WRAPPERS))
def test_every_find_wrapper_returns_exactly_what_find_element_file_returns(model: Path, layer: str) -> None:
    identity = FIND_TARGETS[layer]
    found = FIND_WRAPPERS[layer](str(model), identity)
    assert found is not None
    assert found == reader.find_element_file(str(model), layer, identity)


def test_find_reads_the_layers_declared_identity_field_not_its_filename(model: Path) -> None:
    """``requirements`` is searched by ``id``, the others by ``name``.

    Both files are named otherwise than their identity, so a filename match would answer ``None``.
    """
    assert reader.find_requirement_file(str(model), "REQ_001") == str(model / "requirements" / "not-the-id.yaml")
    assert reader.find_data_type_file(str(model), "Speed") == str(
        model_element_path(model, "renamed", kind="value_type")
    )
    assert reader.find_requirement_file(str(model), "not-the-id") is None


@pytest.mark.parametrize("layer", sorted(FIND_WRAPPERS))
def test_an_absent_layer_directory_finds_nothing_and_reads_empty(tmp_path: Path, layer: str) -> None:
    empty = tmp_path / "model"
    empty.mkdir()
    assert FIND_WRAPPERS[layer](str(empty), "whatever") is None
    assert reader.read_layer(str(empty), layer) == []


def test_the_nesting_layer_is_still_searched_recursively(model: Path) -> None:
    """``find_swc_file`` reaches a component two directories down."""
    assert reader.find_swc_file(str(model), "Deep") == str(model / "swc" / "a" / "b" / "Deep.yaml")


# ---------------------------------------------------------------------------
# The refusals, word for word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", sorted(READER_LABELS))
def test_every_layers_refusal_names_the_file_with_the_label_it_always_used(tmp_path: Path, layer: str) -> None:
    root = tmp_path / "model"
    facts = production_registry().layer(layer)
    bad = root / facts.storage.directory / f"Bad{facts.storage.file_suffix}"
    _write(bad, INVALID_DOCUMENTS[layer])

    with pytest.raises(ValueError) as excinfo:
        if layer in LAYERS_WITHOUT_A_READ_WRAPPER:
            reader.read_layer(str(root), layer)
        else:
            READ_WRAPPERS[layer](str(root))

    # Shared by its storage name; the full path only when a local terminal asks.
    assert str(excinfo.value).startswith(f"Invalid {READER_LABELS[layer]} YAML in {bad.relative_to(root).as_posix()}: ")
    assert excinfo.value.render(full_path=True).startswith(f"Invalid {READER_LABELS[layer]} YAML in {bad}: ")
    # One self-contained line per field, with none of Pydantic's own text.
    lines = excinfo.value.render_lines(full_path=False)
    assert lines and all(line.startswith(f"Invalid {READER_LABELS[layer]} YAML in ") for line in lines)
    markers = ("input_value", "errors.pydantic.dev", "\n")
    assert not any(marker in line for line in [*lines, str(excinfo.value)] for marker in markers), lines


def test_a_refusal_still_carries_the_model_root_relative_target(tmp_path: Path) -> None:
    """``ElementParseError.target`` is what ``editing/engine.py`` answers with."""
    root = tmp_path / "model"
    _write(root / "swc" / "sensors" / "Bad.yaml", INVALID_DOCUMENTS["swc"])

    with pytest.raises(reader.ElementParseError) as excinfo:
        reader.read_swc_model(str(root))

    assert excinfo.value.target == "swc/sensors/Bad.yaml"


def test_read_swc_model_with_placement_still_refuses_a_missing_model_root(tmp_path: Path) -> None:
    """The one wrapper that raises on a missing root, and the one message it raises."""
    missing = tmp_path / "nope"
    # `re.escape`: a Windows path is full of regex escapes (`C:\Users` is
    # "incomplete escape \U"). The shared message names no location, so the path
    # is matched on the full rendering the CLI prints.
    with pytest.raises(FileNotFoundError, match="^Model root not found: the model root does not exist$") as refused:
        reader.read_swc_model_with_placement(str(missing))
    assert re.fullmatch(f"Model root not found: {re.escape(str(missing))}", refused.value.render(full_path=True))
    with pytest.raises(FileNotFoundError):
        reader.read_swc_model(str(missing))
    # Every other wrapper answers empty rather than raising, exactly as before.
    assert reader.read_interfaces(str(missing)) == []
    assert reader.find_task_file(str(missing), "T") is None


def test_placement_is_still_reported_beside_the_element(model: Path) -> None:
    pairs = reader.read_swc_model_with_placement(str(model))
    by_name = {element.name: values for element, values in pairs}
    assert by_name["SpeedSensor"] == {"group": "sensors"}
    assert by_name["Flat"] == {"group": ""}
    assert by_name["Deep"] == {"group": "a/b"}
    assert [element for element, _ in pairs] == reader.read_swc_model(str(model))


# ---------------------------------------------------------------------------
# The three removed derived views
# ---------------------------------------------------------------------------


def test_the_module_declares_none_of_the_three_deleted_views() -> None:
    """``LAYER_GLOBS``, ``SWC_PLACEMENT_FIELD`` and ``UNGROUPED_SWC_PLACEMENT`` are gone.

    The traversal is ``model_state.scan_layer`` and the placement field is read off
    ``_NESTING_LAYER.storage``; the tests below hold both directly.
    """
    assert not hasattr(reader, "LAYER_GLOBS")
    assert not hasattr(reader, "SWC_PLACEMENT_FIELD")
    assert not hasattr(reader, "UNGROUPED_SWC_PLACEMENT")


def test_the_recursion_scan_layer_performs_is_the_layers_own_storage_record(tmp_path: Path) -> None:
    """A nested file is found for exactly the layers whose storage record nests.

    The suffix comes from ``facts.storage.file_suffix``: the shipped layers agreeing on
    ``.yaml`` is a fact of the contract, not of this test.
    """
    registry = production_registry()
    root = tmp_path / "model"
    for facts in registry.layers:
        directory = root / facts.storage.directory
        (directory / "nested").mkdir(parents=True)
        (directory / f"Top{facts.storage.file_suffix}").write_text("name: Top\n", encoding="utf-8")
        (directory / "nested" / f"Deep{facts.storage.file_suffix}").write_text("name: Deep\n", encoding="utf-8")

    recursive_layers = {facts.name for facts in registry.layers if facts.storage.is_recursive}
    assert recursive_layers, "the shipped contract declares one nesting layer; a probe over none proves nothing"

    for facts in registry.layers:
        scan = scan_layer(root, facts.name, registry=registry)
        names = {entry.path.name for entry in scan.files}
        assert f"Top{facts.storage.file_suffix}" in names
        assert (f"Deep{facts.storage.file_suffix}" in names) == (facts.name in recursive_layers)


def test_the_scan_order_is_the_case_sensitive_posix_order_on_every_platform(tmp_path: Path) -> None:
    """The file order ``scan_layer`` returns, pinned against the platform it varies on.

    ``sorted()`` over ``Path`` compares ``PurePath._str_normcase``, on Windows the
    lowercased string with ``\\`` separators, so the same files come back
    ``alpha, Beta, Zebra`` there. The callers of ``scan_layer`` read this order; the
    element order is ``model_state.py::load_layer_with_placement``'s. The
    case-sensitive POSIX answer is written out, so a Windows run reports a difference
    instead of absorbing it.
    """
    registry = production_registry()
    facts = next(item for item in registry.layers if item.storage.is_recursive)
    directory = tmp_path / "model" / facts.storage.directory
    suffix = facts.storage.file_suffix
    names = ["Zebra", "alpha", "Beta"]
    for name in names:
        (directory / "Zone").mkdir(parents=True, exist_ok=True)
        (directory / f"{name}{suffix}").write_text("name: x\n", encoding="utf-8")
        (directory / "Zone" / f"{name}{suffix}").write_text("name: x\n", encoding="utf-8")

    scan = scan_layer(tmp_path / "model", facts.name, registry=registry)
    relative = [entry.path.relative_to(directory).as_posix() for entry in scan.files]

    assert relative == sorted(relative), relative
    assert relative == [
        f"Beta{suffix}",
        f"Zebra{suffix}",
        f"Zone/Beta{suffix}",
        f"Zone/Zebra{suffix}",
        f"Zone/alpha{suffix}",
        f"alpha{suffix}",
    ], relative


def test_the_placement_field_is_the_nesting_layers_own_declaration() -> None:
    """The one nesting layer declares ``group`` placement, and ``reader._NESTING_LAYER`` reads that record."""
    registry = production_registry()
    nesting = [facts for facts in registry.layers if facts.storage.is_recursive]
    assert len(nesting) == 1
    assert nesting[0].storage.nesting == "group"
    assert reader._NESTING_LAYER is not None
    assert reader._NESTING_LAYER.storage.nesting == nesting[0].storage.nesting


def test_swc_placement_still_answers_for_the_partial_consumer_that_imports_it() -> None:
    """``component_exchange/import_.py`` imports ``reader.swc_placement`` by name."""
    swc_dir = Path("/model/swc")
    assert reader.swc_placement(swc_dir / "Flat.yaml", swc_dir) == {"group": ""}
    assert reader.swc_placement(swc_dir / "sensors" / "S.yaml", swc_dir) == {"group": "sensors"}
    assert reader.swc_placement(swc_dir / "a" / "b" / "D.yaml", swc_dir) == {"group": "a/b"}


# ---------------------------------------------------------------------------
# No traversal of its own
# ---------------------------------------------------------------------------


def test_reader_runs_no_directory_traversal_of_its_own() -> None:
    """Not one ``glob``/``rglob``/``iterdir``/``scandir``/``walk`` call is left: ``model_state.py`` owns traversal.

    Asserted over the AST so a call spelled across two lines is still a ``Call`` node.
    """
    tree = ast.parse(inspect.getsource(reader))
    forbidden = {"glob", "rglob", "iterdir", "scandir", "listdir", "walk"}
    found = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden
    ]
    found += [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden
    ]
    assert found == []


# ---------------------------------------------------------------------------
# The injection seam and the sixteenth layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("function", [reader.read_layer, reader.find_element_file])
def test_the_registry_parameter_defaults_to_none_rather_than_to_a_registry(function: Any) -> None:
    """The seam: a default expression would pin every caller at definition time."""
    parameter = inspect.signature(function).parameters["registry"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


@pytest.fixture
def synthetic_model(tmp_path: Path, merged: ModelRegistry) -> Path:
    root = tmp_path / "model"
    directory = merged.layer("synthetic").storage.directory
    _write(root / directory / "Alpha.yaml", {"name": "Alpha", "title": "First", "composition": "VehicleTop"})
    _write(root / directory / "named-otherwise.yaml", {"name": "Beta", "title": "Second", "composition": "VehicleTop"})
    return root


def test_the_synthetic_layer_is_read_with_no_source_edit(synthetic_model: Path, merged: ModelRegistry) -> None:
    """A layer only the merged contract declares, read by the same generic function."""
    with use_registry(merged):
        elements = reader.read_layer(str(synthetic_model), "synthetic")
    assert [type(element) for element in elements] == [SyntheticElement, SyntheticElement]
    assert [element.name for element in elements] == ["Alpha", "Beta"]


def test_the_synthetic_layers_element_is_found_by_its_declared_identity(
    synthetic_model: Path, merged: ModelRegistry
) -> None:
    with use_registry(merged):
        found = reader.find_element_file(str(synthetic_model), "synthetic", "Beta")
        missing = reader.find_element_file(str(synthetic_model), "synthetic", "named-otherwise")
    assert found == str(synthetic_model / "synthetic" / "named-otherwise.yaml")
    assert missing is None


def test_an_explicitly_passed_registry_needs_no_context_block(synthetic_model: Path, merged: ModelRegistry) -> None:
    """The registry is resolved in the body, so passing one is enough on its own."""
    elements = reader.read_layer(str(synthetic_model), "synthetic", registry=merged)
    assert [element.name for element in elements] == ["Alpha", "Beta"]
    assert reader.find_element_file(str(synthetic_model), "synthetic", "Alpha", registry=merged) == str(
        synthetic_model / "synthetic" / "Alpha.yaml"
    )


def test_the_shipped_registry_does_not_know_the_synthetic_layer(synthetic_model: Path) -> None:
    """The negative half: without the merged registry the layer is not declared."""
    with pytest.raises(KeyError):
        reader.read_layer(str(synthetic_model), "synthetic")


def test_a_refusal_in_the_synthetic_layer_names_the_layer(tmp_path: Path, merged: ModelRegistry) -> None:
    """No ``snapshotAttributes`` row, so the label is the layer name — the honest answer."""
    root = tmp_path / "model"
    _write(root / "synthetic" / "Bad.yaml", {"title": "no name at all"})
    with pytest.raises(ValueError) as excinfo, use_registry(merged):
        reader.read_layer(str(root), "synthetic")
    assert str(excinfo.value).startswith("Invalid synthetic YAML in synthetic/Bad.yaml: ")
    full = excinfo.value.render(full_path=True)
    assert full.startswith(f"Invalid synthetic YAML in {root / 'synthetic' / 'Bad.yaml'}: ")


# ---------------------------------------------------------------------------
# Rule E in the reader — the locale's codec never decides what a model says
# ---------------------------------------------------------------------------

#: The codec a measured Windows host resolves for a text read that names none
#: (``locale.getpreferredencoding(False)``): a real host's codec, not an invented
#: worst case. :func:`forced_locale_codec` installs it.
LOCALE_CODEC = "cp1252"


def _locale_read_text(path: Path) -> str:
    """``path.read_text()`` with no encoding — the shape Rule E forbids.

    Reached through ``getattr`` so it resolves on the class at call time (the shim
    answers), and so ``test_platform_io.py``'s Rule E scan, which reads the test
    trees with no allow-list, does not flag this module.
    """
    return getattr(Path, "read_text")(path)  # noqa: B009


def _write_utf8(path: Path, document: Any) -> None:
    """Write *document* as the **UTF-8 bytes** a hand-authored model file has.

    Not :func:`_write`: ``yaml.safe_dump`` escapes non-ASCII to ``\\uXXXX`` by
    default, so its output is ASCII no codec can misread. ``write_bytes`` keeps the
    fixture outside every codec, the forced one included.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8"))


@contextlib.contextmanager
def forced_locale_codec(codec: str) -> Iterator[None]:
    """Make every *undeclared* text read and write resolve to *codec*.

    Forcing the codec makes the encoding defect an oracle on every platform, not
    only Windows. Monkeypatching ``locale.getpreferredencoding`` would not do: since
    Python 3.11 a ``TextIOWrapper`` with no encoding resolves the locale codec in C.

    **What this does not reach.** Only ``Path.read_text``, ``Path.write_text`` and
    ``Path.open`` are substituted; a builtin ``open()``, an ``io.TextIOWrapper``, a
    text-mode ``subprocess`` and third-party code keep the real locale's codec. It
    changes no console encoding and says nothing about which bytes a platform
    writes (a byte comparison is that oracle).
    """
    original_read_text = Path.read_text
    original_write_text = Path.write_text
    original_open = Path.open

    def read_text(self: Path, encoding: str | None = None, *args: Any, **kwargs: Any) -> str:
        return original_read_text(self, codec if encoding is None else encoding, *args, **kwargs)

    def write_text(self: Path, data: str, encoding: str | None = None, *args: Any, **kwargs: Any) -> int:
        return original_write_text(self, data, codec if encoding is None else encoding, *args, **kwargs)

    def open_(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "b" not in mode and encoding is None:
            encoding = codec
        return original_open(self, mode, buffering, encoding, *args, **kwargs)

    Path.read_text = read_text  # type: ignore[method-assign]
    Path.write_text = write_text  # type: ignore[method-assign]
    Path.open = open_  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.read_text = original_read_text  # type: ignore[method-assign]
        Path.write_text = original_write_text  # type: ignore[method-assign]
        Path.open = original_open  # type: ignore[method-assign]


#: An em dash in a description: the character observed coming back as mojibake.
EM_DASH_DESCRIPTION = "256 KiB application RAM pool — 80% consumption budget"

#: A description cp1252 cannot decode at all. ``č`` is U+010D, whose UTF-8 bytes
#: are ``C4 8D``, and ``0x8D`` is undefined in cp1252 — so this text raises where
#: the em dash silently lies; both failure modes are asserted.
UNDECODABLE_DESCRIPTION = "řídicí jednotka — počet cyklů"


def test_the_forced_codec_harness_actually_bites(tmp_path: Path) -> None:
    """The shim changes an undeclared read and leaves a declared one alone; a no-op shim would pass everything."""
    path = tmp_path / "probe.yaml"
    path.write_bytes(EM_DASH_DESCRIPTION.encode("utf-8"))
    pristine = (Path.read_text, Path.write_text, Path.open)

    with forced_locale_codec(LOCALE_CODEC):
        mojibake = EM_DASH_DESCRIPTION.encode("utf-8").decode(LOCALE_CODEC)
        assert mojibake != EM_DASH_DESCRIPTION
        assert _locale_read_text(path) == mojibake, "the shim did not reach an undeclared read"
        assert path.read_text(encoding="utf-8") == EM_DASH_DESCRIPTION, "a declared read must be untouched"
        with path.open(encoding="utf-8") as handle:
            assert handle.read() == EM_DASH_DESCRIPTION
        assert (Path.read_text, Path.write_text, Path.open) != pristine, "the shim was never installed"

    # Restoration is asserted as identity, not behaviour: on Windows the ambient
    # codec is cp1252 too, so an undeclared read returns the same mojibake with the
    # shim gone, and a behavioural check fails there on a correctly removed shim.
    assert (Path.read_text, Path.write_text, Path.open) == pristine, "the shim outlived its block"


def test_a_description_round_trips_when_the_locale_codec_is_not_utf8(tmp_path: Path) -> None:
    """``model_state.parse_element`` decodes the model, not the operator's locale.

    Every element is loaded through this read; under cp1252 a valid UTF-8 file came
    back as mojibake with no error. The file is built in ``tmp_path`` so the oracle
    depends on neither the platform nor the demo model's content.
    """
    root = tmp_path / "model"
    _write_utf8(
        root / "swc" / "Diagnostics.yaml",
        {"name": "Diagnostics", "type": "application", "description": EM_DASH_DESCRIPTION},
    )

    with forced_locale_codec(LOCALE_CODEC):
        elements = reader.read_swc_model(str(root))

    assert [element.description for element in elements] == [EM_DASH_DESCRIPTION]


def test_find_element_file_locates_an_element_the_locale_codec_cannot_decode(tmp_path: Path) -> None:
    """``reader.find_element_file``'s scan, the second read of every layer, decodes UTF-8.

    The loud failure: an undeclared read raises ``UnicodeDecodeError`` here, and the
    writers call this function to decide whether they would overwrite something.
    """
    root = tmp_path / "model"
    _write_utf8(
        root / "swc" / "Diagnostics.yaml",
        {"name": "Diagnostics", "type": "application", "description": UNDECODABLE_DESCRIPTION},
    )

    with forced_locale_codec(LOCALE_CODEC):
        found = reader.find_element_file(str(root), "swc", "Diagnostics")

    assert found == str(root / "swc" / "Diagnostics.yaml")
