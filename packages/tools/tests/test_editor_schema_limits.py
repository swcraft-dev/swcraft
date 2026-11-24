"""The editor schemas' limitation map, made a gate.

Every limitation class expressible as one model document has a fixture under
``packages/tools/tests/fixtures/editor_schema/accepted_but_invalid/<layer dir>/``: the
layer's generated schema (``Draft7Validator``) **accepts** it and ``swcraft check-model``,
run as the real CLI on a copy of ``examples/basic``, **rejects** the project it is
dropped into (most classes need other files to dangle from). The manifest rows are
refused by the loader rather than a model rule. The divergences where the editor is
stricter are asserted with the signs reversed. Beside them, an unknown key is reported by
both (``check-model``'s ``model.unknown-key`` finding, or a load refusal in a kind
declaring ``extra='forbid'``).

Classes with no fixture: ``FieldFacts.immutable`` (an editing-protocol fact, not a
property of a file); ``FieldFacts.omit_at_default`` (a writer serialization rule); the
association globs (a language-server matching behaviour; this module never runs the
server); ``rule:data-type.application-type-unmapped`` (it needs three files this
one-document harness cannot add; its oracle is ``test_validator_application_types.py``).
Filename-equals-``name``/``id`` has no rejecting fixture because neither oracle rejects it.

What else this module does not reach: one fixture per class, not every rule of a class;
the expected fragments are stderr **substrings**, so a fixture tripping a second rule
still passes; and the schema side is the committed contract's rendering, which
``check-model`` also reads, so both oracles move together with the contract.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, layer_for_path, model_element_path, model_element_storage_name
from jsonschema import Draft7Validator
from swcraft_tools.editor_schema import layer_schema, manifest_schema
from swcraft_tools.model_registry import current_registry

_EXAMPLE_PROJECT = REPO_ROOT / "examples" / "basic"
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "editor_schema" / "accepted_but_invalid"


@dataclass(frozen=True)
class Limitation:
    """One named limitation class, its fixture, and what ``check-model`` says about it."""

    name: str
    fixture: str  # relative to the fixtures root; its first segment selects the layer
    rejection: str  # a substring of check-model's stderr


_LONG_NAME = "A" * 120  # legal as declared; ``_Implementation`` makes it 135

#: The schema-limitation classes, in the limitation table's order.
LIMITATIONS: tuple[Limitation, ...] = (
    Limitation(
        "cross-file reference (a `satisfies` link naming a requirement)",
        "swc/example/DanglingLinkSWC.yaml",
        "SWC 'DanglingLinkSWC' satisfies 'REQ_DANGLING_001@1': no requirement declares 'REQ_DANGLING_001'",
    ),
    Limitation(
        "cross-file reference (a `reference` field: connector endpoint)",
        "connectors/Dangling_Link.yaml",
        "Connector 'Dangling_Link' invalid: SWC not found: NoSuchSWC",
    ),
    Limitation(
        "name uniqueness within a file (sibling ports)",
        "swc/example/TwinPortsSWC.yaml",
        "SWC 'TwinPortsSWC' has duplicate port name 'pOut'",
    ),
    Limitation(
        "composite identity uniqueness within a file (one `(port, element)` read in both forms)",
        "swc/example/TwinReadsSWC.yaml",
        "duplicate reads entry in runnable: rIn.value",
    ),
    Limitation(
        "an access naming a port its component does not declare (`swc.access.unknown-port`)",
        "swc/example/BlindReadSWC.yaml",
        "SWC 'BlindReadSWC' declares no port 'rMissing', which runnable 'BlindReadRun' accesses (1 entry)",
    ),
    Limitation(
        "two access entries of one runnable deriving one access point name (`swc.access.name-collision`)",
        "swc/example/AccessNameCollision.yaml",
        "derives generated access point SHORT-NAME 'R_pA_B_value' from more than one access entry",
    ),
    Limitation(
        "an operation event naming a port its component does not declare (`swc.operation-event.unknown-port`)",
        "swc/example/UnboundOperationEventSWC.yaml",
        "SWC 'UnboundOperationEventSWC' declares no port 'pMissing', which the operation event of runnable "
        "'ServeDiag' names (1 event)",
    ),
    Limitation(
        "name uniqueness across files (the same SWC name in another group)",
        "swc/duplicate/ExampleSWC.yaml",
        "Duplicate SWC name 'ExampleSWC'",
    ),
    Limitation(
        "reserved names (the writer's generated `_Implementation` form)",
        "swc/example/ExampleSWC_Implementation.yaml",
        "conflicts with the auto-generated SWC-IMPLEMENTATION name for 'ExampleSWC'",
    ),
    Limitation(
        "AUTOSAR Identifier, declared form",
        "interfaces/bad-interface.yaml",
        "is not a legal AUTOSAR Identifier",
    ),
    Limitation(
        "AUTOSAR Identifier, generated form",
        f"swc/example/{_LONG_NAME}.yaml",
        f"generates the SHORT-NAME '{_LONG_NAME}_Implementation' (135 characters), "
        "which is not a legal AUTOSAR Identifier",
    ),
    Limitation(
        "an `init` value the declared type cannot hold (the permissive `value` rendering)",
        "interfaces/InitMismatch.yaml",
        "Interface 'InitMismatch' element 'level' init value at 'init' is a list, "
        "but type 'uint8' is a value_type, which takes a scalar.",
    ),
    Limitation(
        # "Exactly one file declares the root" is a statement about every file
        # at once, which no single-document schema can see.
        "cross-layer consistency (a second composition declaring `root: true`)",
        "compositions/Root2.yaml",
        "Compositions Root, Root2 all declare 'root: true'; exactly one may",
    ),
    Limitation(
        # `interface` is optional so a component stub's port can omit it; the
        # editor cannot say "required unless external".
        "a declared component's port without an interface (`port.undeclared-interface`)",
        "swc/example/InterfacelessPortSWC.yaml",
        "SWC 'InterfacelessPortSWC' port 'pOut' declares no interface",
    ),
    Limitation(
        # The key is optional (a component stub may not carry it), so the editor
        # cannot say "required unless external".
        "a component without `handle_termination_and_restart` (`swc.undeclared-termination-and-restart`)",
        "swc/example/NoTerminationSWC.yaml",
        "SWC 'NoTerminationSWC' sets no handle_termination_and_restart",
    ),
    Limitation(
        # `data_element` is a plain string; the elements it may name are in
        # another file.
        "a `com_specs` entry naming no data element of its port's interface (`swc.port.com-spec-unknown-element`)",
        "swc/example/ComSpecUnknownElementSWC.yaml",
        "SWC 'ComSpecUnknownElementSWC' port 'rIn' declares com_specs for data element 'missing', but "
        "sender/receiver interface 'IExample' declares no data element 'missing'",
    ),
    Limitation(
        # Which symbol a runnable is written with depends on a sibling member's
        # name, which no schema `pattern` can see.
        "two runnables of one component written with one symbol (`name.duplicate-runnable-symbol`)",
        "swc/example/DuplicateSymbolSWC.yaml",
        "SWC 'DuplicateSymbolSWC' runnables 'First' and 'Second' are written with one symbol 'First'",
    ),
    Limitation(
        # A port's own `init` renders as the permissive `value`, and the type it
        # must fit is in another file.
        "a port's own `init` its data element's type cannot hold (`interface.init-shape-mismatch` at a port)",
        "swc/example/PortInitMismatchSWC.yaml",
        "SWC 'PortInitMismatchSWC' port 'rIn' com_specs init value for data element 'value' at 'init' is a list, "
        "but type 'float32' is a value_type, which takes a scalar.",
    ),
    Limitation(
        # `port` names a sibling member, and the interface stub it must be typed
        # by lives in another file.
        "a disabled mode on a port that is not a require port on a mode-switch stub "
        "(`swc.timing_event.disabled-mode-port`)",
        "swc/example/DisabledModeSWC.yaml",
        "SWC 'DisabledModeSWC' runnable 'DisabledRun' timing event (period 0.01s) is disabled in mode "
        "'/Modes/ModeDeclarationGroups/Modes/Off' on port 'rMode', but the component declares no port 'rMode'",
    ),
    Limitation(
        # `queued` is stated in the interface file, what the queue needs in each
        # receiving component file. The fixture replaces the example's own
        # interface, so the example's require port is the receiver;
        # `swc.access.queued-read-by-value` has its own oracle in
        # `test_behavioural_rules.py`.
        "a queued element's receivers "
        "(`swc.port.queued-receiver-without-queue-length`, `swc.access.queued-read-by-value`)",
        "interfaces/IExample.yaml",
        "SWC 'ExampleConsumerSWC' require port 'rIn' receives queued data element 'value' of interface 'IExample' "
        "and states no queue_length for it",
    ),
    Limitation(
        # A `description` renders as a plain `string`; a `pattern` for the
        # characters XML 1.0 cannot carry would be a third copy of
        # `xml_text.py`'s rule. The fixture holds a bell as a YAML escape.
        "a text XML 1.0 cannot carry (`text.xml-unrepresentable-character`)",
        "data_types/implementation/BellText.yaml",
        "Data type 'BellText' `description` holds U+0007, a character XML 1.0 cannot carry",
    ),
    Limitation(
        "a `@field_validator` (Task.period must be > 0)",
        "tasks/Task_negative.yaml",
        "period must be > 0",
    ),
    Limitation(
        "a `@model_validator` (a reference type's base equals its own name)",
        "data_types/implementation/SelfAlias.yaml",
        "self-referential reference type",
    ),
    Limitation(
        "`allow_inf_nan=False` (`.nan` is a number to the schema)",
        "tasks/Task_nan.yaml",
        "Task_nan.yaml: period: Input should be a finite number",
    ),
    Limitation(
        "resource budgets (a strict CPU core over threshold)",
        "tasks/Task_heavy.yaml",
        "CPU core 'CORE0' utilization",
    ),
    Limitation(
        "a `reference` field whose target is a name, not a path (a task's `partition`)",
        "tasks/Task_undeclared_partition.yaml",
        "Task 'Task_undeclared_partition' runs in partition 'PARTITION9', which no hardware element declares",
    ),
    Limitation(
        "a mapping set's two sides, whose accepted *kinds* live in the named types' own files",
        model_element_storage_name("WrongKinds", layer="mapping_sets"),
        # The application side resolves in the `application_types` layer alone,
        # so a value type there is undeclared.
        "Mapping set 'WrongKinds' maps 'uint8', which is not declared in the application_types layer",
    ),
    Limitation(
        "a {swc, runnable} pair mapped to more than one task",
        "tasks/Task_20ms_dup.yaml",
        "Runnable 'ExampleConsumerSWC.ExampleConsumerRun' is mapped to multiple tasks: Task_10ms, Task_20ms_dup",
    ),
    Limitation(
        "sequence steps a subsequence of the task's runnables",
        "sequences/unmapped_step.yaml",
        "Sequence 'unmapped_step' step 'ExampleSWC.ExampleRun' is not mapped to task 'Task_10ms'",
    ),
)


#: The manifest's own classes, all refused by the *loader*. The path rules are
#: invisible to a schema that never learns where the file lives, and are two rows
#: because escape and overlap fail in opposite directions; `toolchain_version`'s
#: grammar has no schema `pattern` deliberately (what JSON Schema cannot express is
#: answered by `check-model`, not a hand-written regular expression).
#: `test_every_fixture_file_is_claimed_by_exactly_one_limitation` holds these rows to
#: the fixtures on disk, never to the loader's rules.
MANIFEST_LIMITATIONS: tuple[Limitation, ...] = (
    Limitation(
        "the manifest's path rules (`model_root` escaping the project root)",
        "manifest/swcraft.yaml",
        "Manifest field 'model_root' resolves to",
    ),
    Limitation(
        "the `toolchain_version` pin's grammar (a specifier where one version is declared)",
        "manifest/swcraft-range-pin.yaml",
        "toolchain_version '>=0.7' is not a version",
    ),
    Limitation(
        "an absolute output root, under either platform's rules (`dist_root: C:/dist`)",
        "manifest/swcraft-absolute-dist-root.yaml",
        "'dist_root' is written as the absolute path",
    ),
    Limitation(
        "a generated root that overlaps `model_root` (`dist_root: model/dist`)",
        "manifest/swcraft-overlapping-dist-root.yaml",
        "which overlaps 'model_root'",
    ),
)


# The two oracles


def _schema_errors(relative: str, document: dict) -> list[str]:
    layer = layer_for_path(Path(relative).as_posix(), current_registry())
    return sorted(error.message for error in Draft7Validator(layer_schema(layer)).iter_errors(document))


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _project_copy(tmp_path: Path) -> Path:
    """A private copy of ``examples/basic`` — the manifest and the model, not the outputs."""
    target = tmp_path / "project"
    shutil.copytree(_EXAMPLE_PROJECT, target, ignore=shutil.ignore_patterns("dist", "views", "__pycache__"))
    return target


def _check_model(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


# The gate


def test_the_copied_example_project_passes_check_model_on_its_own(tmp_path: Path) -> None:
    """Without a fixture the oracle says yes — so every rejection below is the fixture's doing."""
    result = _check_model(_project_copy(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "Validation passed" in result.stdout


@pytest.mark.parametrize("limitation", LIMITATIONS, ids=[entry.fixture for entry in LIMITATIONS])
def test_the_schema_accepts_what_check_model_rejects(limitation: Limitation, tmp_path: Path) -> None:
    source = _FIXTURE_ROOT / limitation.fixture
    document = _load(source)

    # Direction one: the generated schema for the layer accepts the document.
    assert _schema_errors(limitation.fixture, document) == [], limitation.name

    # Direction two: the product refuses the project the document is dropped into.
    project = _project_copy(tmp_path)
    target = project / "model" / limitation.fixture
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    result = _check_model(project)
    assert result.returncode == 1, f"{limitation.name}: check-model accepted the fixture\n{result.stdout}"
    assert limitation.rejection in result.stderr, f"{limitation.name}:\n{result.stderr}"


@pytest.mark.parametrize("limitation", MANIFEST_LIMITATIONS, ids=lambda entry: entry.fixture)
def test_the_manifest_schema_accepts_what_the_loader_rejects(limitation: Limitation, tmp_path: Path) -> None:
    """The manifest rows: well-typed keys the schema passes and the loader refuses."""
    document = _load(_FIXTURE_ROOT / limitation.fixture)
    assert list(Draft7Validator(manifest_schema()).iter_errors(document)) == []

    project = _project_copy(tmp_path)
    shutil.copyfile(_FIXTURE_ROOT / limitation.fixture, project / "swcraft.yaml")
    result = _check_model(project)
    assert result.returncode == 1, result.stdout
    assert limitation.rejection in result.stderr, result.stderr


def test_every_fixture_file_is_claimed_by_exactly_one_limitation() -> None:
    """No fixture without a class, no class without a fixture — in both directions."""
    on_disk = {path.relative_to(_FIXTURE_ROOT).as_posix() for path in _FIXTURE_ROOT.rglob("*.yaml")}
    claimed = [entry.fixture for entry in (*LIMITATIONS, *MANIFEST_LIMITATIONS)]
    assert len(claimed) == len(set(claimed)), "one fixture per class"
    assert on_disk == set(claimed), (
        f"unclaimed {sorted(on_disk - set(claimed))}, missing {sorted(set(claimed) - on_disk)}"
    )


# Where the editor is stricter


def test_an_unknown_key_is_reported_by_the_schema_and_by_check_model(tmp_path: Path) -> None:
    """Both report the key: ``check-model`` as the ``model.unknown-key`` finding, the model still loading."""
    project = _project_copy(tmp_path)
    task_file = project / "model" / "tasks" / "Task_10ms.yaml"
    document = _load(task_file)
    document["descripton"] = document.pop("description")

    errors = _schema_errors("tasks/Task_10ms.yaml", document)
    assert errors == ["Additional properties are not allowed ('descripton' was unexpected)"]

    task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = _check_model(project)
    assert result.returncode == 1, result.stderr
    output = result.stdout + result.stderr
    assert "carries the key 'descripton', which kind `task` does not declare" in output, output
    assert "the nearest declared key is 'description'" in output, output


def test_an_unknown_key_in_a_base_type_is_reported_by_the_schema_and_refused_by_the_loader(tmp_path: Path) -> None:
    """A kind declaring ``extra='forbid'``: the loader refuses the key at load.

    Not reached: the other forbidding kinds, which
    ``test_schema.py::TestTheForbidIsScopedToTheKindsThatDeclareIt`` holds at the class level.
    """
    project = _project_copy(tmp_path)
    type_file = model_element_path(project / "model", "bool_base", kind="base_type")
    document = _load(type_file)
    assert document["kind"] == "base_type"
    document["descripton"] = "typo"

    assert _schema_errors(model_element_storage_name("bool_base", kind="base_type"), document)

    type_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = _check_model(project)
    assert result.returncode == 1, result.stderr
    assert "bool_base.yaml: descripton: Extra inputs are not permitted\n" in result.stderr, result.stderr


@pytest.mark.parametrize(
    ("field", "value", "schema_error"),
    [
        ("priority", "5", "'5' is not of type 'integer'"),
        ("priority", False, "False is not of type 'integer'"),
    ],
    ids=["priority-as-string", "priority-as-yaml-bool"],
)
def test_a_lax_coerced_scalar_is_reported_by_the_schema_and_accepted_by_check_model(
    field: str, value: object, schema_error: str, tmp_path: Path
) -> None:
    """Pydantic's lax mode coerces the wrong scalar type the schema reports, so ``check-model`` exits 0.

    Left open on purpose: the schema is right about the file, and ``strict=True`` on
    ``ModelElement`` would change product behaviour.
    """
    project = _project_copy(tmp_path)
    task_file = project / "model" / "tasks" / "Task_10ms.yaml"
    document = _load(task_file)
    document[field] = value

    assert _schema_errors("tasks/Task_10ms.yaml", document) == [schema_error]

    task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    text = task_file.read_text(encoding="utf-8")
    assert f"{field}: '5'" in text or f"{field}: false" in text, "the YAML really carries the wrong scalar type"
    result = _check_model(project)
    assert result.returncode == 0, result.stderr


def test_a_filename_that_differs_from_the_element_name_is_rejected_by_neither(tmp_path: Path) -> None:
    """Filename-equals-``name`` is the writer's convention, not a rule either oracle enforces.

    ``reader.find_element_file`` scans by content, so the sequence naming ``Task_10ms`` still resolves.
    """
    project = _project_copy(tmp_path)
    tasks = project / "model" / "tasks"
    (tasks / "Task_10ms.yaml").rename(tasks / "Renamed.yaml")

    assert _schema_errors("tasks/Renamed.yaml", _load(tasks / "Renamed.yaml")) == []
    result = _check_model(project)
    assert result.returncode == 0, result.stderr
