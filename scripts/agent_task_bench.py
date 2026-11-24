"""Build the agent task benchmark's benches: the large synthetic one, or a specimen from a passport.

The large bench is the ``large-bench`` specimen (about 3 750 element files, a 550-port hub
component), generated from its committed passport into the specimen cache, never committed::

    python scripts/agent_task_bench.py large TARGET [--with-import]

copies it into ``TARGET``; ``--with-import`` adds ``imports/WlLampController.arxml``, the
component export task 10 imports (optional because it runs the writer over the whole bench). A
specimen that cannot be taken exits 1, and a failed ``--with-import`` removes ``TARGET``.

A *specimen* is a project generated from a passport configuration, the model the any-model set
is bound on when no real project is at hand::

    python scripts/agent_task_bench.py specimen CONFIG TARGET

writes into ``TARGET`` (which must not exist) the generated project, its manifest replaced by
SWCraft's. A configuration the generator rejects exits 2 with the number of lines on its stderr,
never their text (which may carry the configuration's values); any other generator failure, or
none configured, exits 1; either way no ``TARGET`` is left.

Not reached: both are synthetic. A figure on the large bench says how the tools scale, not how a
given project reads; a specimen meets its configuration on the leaves it states and nothing else.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"

#: One module name in every loader, so the specimen cache's per-process memo is one object.
_GENERATOR_MODULE = "swcraft_specimen_generator"


def _generator_module() -> ModuleType:
    """``scripts/specimen_generator.py``, loaded by path once per interpreter."""
    cached = sys.modules.get(_GENERATOR_MODULE)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        _GENERATOR_MODULE, Path(__file__).resolve().parent / "specimen_generator.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_GENERATOR_MODULE] = module
    spec.loader.exec_module(module)
    return module


_GENERATOR = _generator_module()
#: What :func:`build_large` raises when the specimen cannot be taken.
TAKE_REFUSALS: tuple[type[Exception], ...] = (
    _GENERATOR.GeneratorNotConfigured,
    _GENERATOR.SpecimenUnavailable,
    _GENERATOR.SpecimenDigestMismatch,
)

#: The specimen the large bench is (a key of the pin file).
LARGE_SPECIMEN = "large-bench"
#: The pin file's ``model_sha256`` of :data:`LARGE_SPECIMEN` (see :func:`digest`).
LARGE_DIGEST = _GENERATOR.load_pins(REPO_ROOT)["specimens"][LARGE_SPECIMEN]["model_sha256"]

#: The large bench's manifest, the specimen cache's own.
MANIFEST = _GENERATOR.manifest_text(LARGE_SPECIMEN)

#: The name a configuration handed to ``specimen`` is generated under (the project manifest's name).
SPECIMEN_PASSPORT_NAME = "agent-bench"

#: Benchmark task 10's component, written into a scratch copy before the export. What it names is taken from
#: the large bench by role: the interface of the hub's first provide port (the hub the component with
#: the most ports, ties by name), the bench's one mapping set, every memory with the budget on the
#: first by name.
IMPORT_COMPONENT = "WlLampController"
#: The bench's one swc group directory, relative to the model root.
IMPORT_GROUP_DIR = "swc/specimen"
IMPORT_COMPONENT_YAML = """\
name: WlLampController
type: application
ports:
- name: rLamp
  direction: require
  interface: If0159abcdefghij
runnables:
- name: UpdateLamp
  events:
  - type: timing
    period: 0.05
data_type_mappings:
- Ms1abcdefghi
handle_termination_and_restart: no_support
memory:
  Me1abc: 512
  Me2abc: 0
  Me3abc: 0
  Me4abc: 0
"""


def _claim(target: Path) -> None:
    """Create *target*, so it is this call's from then on; ``FileExistsError`` when another made it first."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()


def build_large(target: Path, *, with_import: bool = False) -> Path:
    """Copy the large bench into *target* (which must not exist) and return it.

    The specimen is taken first, so a refusal creates nothing. ``FileExistsError`` when another
    process creates *target* first (left alone); any later failure removes *target*.
    """
    if target.exists():
        raise FileExistsError(target)
    source = _GENERATOR.take(LARGE_SPECIMEN, repo_root=REPO_ROOT)
    _claim(target)
    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
        if with_import:
            write_import(target)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def build_specimen(
    configuration: dict[str, Any], target: Path, *, command: list[str] | None = None
) -> _GENERATOR.Outcome:
    """Write the specimen *configuration* describes into *target* (which must not exist): the outcome.

    *command* overrides the configured generator (``None``: the configured one). Returns ``Built``,
    ``Rejected`` or ``GeneratorDefect``; raises ``GeneratorNotConfigured`` before anything is created,
    and ``FileExistsError`` when another process creates *target* first (left alone). *target* is
    removed on any outcome but ``Built`` and on any failure.
    """
    if target.exists():
        raise FileExistsError(target)
    argv = _GENERATOR.generator_command(command, None, REPO_ROOT)
    _claim(target)
    try:
        # A short sibling name keeps the generator's paths short.
        scratch = Path(tempfile.mkdtemp(prefix=".s", dir=target.parent))
        try:
            outcome = _GENERATOR.run_document(argv, configuration, scratch / "o", SPECIMEN_PASSPORT_NAME)
            if isinstance(outcome, _GENERATOR.Built):
                for entry in sorted(outcome.project.iterdir(), key=lambda path: path.as_posix()):
                    os.rename(entry, target / entry.name)
                outcome = _GENERATOR.Built(target)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    if not isinstance(outcome, _GENERATOR.Built):
        shutil.rmtree(target, ignore_errors=True)
    return outcome


def write_import(bench: Path) -> Path:
    """Benchmark task 10's export, ``imports/WlLampController.arxml``, made from a scratch copy of *bench*.

    Made through ``agent_task_bind.write_component_export``, the one place a task's component
    export is made.
    """
    export = _bind_module().write_component_export(
        bench / "swcraft.yaml", IMPORT_COMPONENT_YAML, IMPORT_GROUP_DIR, IMPORT_COMPONENT
    )
    target = bench / "imports" / f"{IMPORT_COMPONENT}.arxml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(export)
    return target


def _checker() -> ModuleType:
    """``scripts/agent_task_check.py``, loaded once by path; its ``load_script`` loads the other siblings."""
    name = "swcraft_agent_task_check"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "agent_task_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _bind_module() -> ModuleType:
    """``scripts/agent_task_bind.py``, loaded only when asked because it imports the product."""
    checker = _checker()
    return checker.load_script(checker.BIND_MODULE)


def digest(bench: Path) -> str:
    """SHA-256 over every file under *bench*'s ``model/``, by POSIX relative path then bytes."""
    return _GENERATOR.digest(bench / "model")


def _read_configuration(path: Path) -> dict[str, Any]:
    """The passport configuration at *path*, a JSON object; ``ValueError`` (or ``OSError``) otherwise."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("a passport configuration is a JSON object")
    return document


def _exists(target: Path) -> int:
    print(f"error: {target} exists; give a new directory", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, command: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the agent task benchmark's generated benches.")
    commands = parser.add_subparsers(dest="bench", required=True)
    large = commands.add_parser("large", help="The large synthetic bench")
    large.add_argument("target", type=Path, help="A directory that does not exist yet")
    large.add_argument("--with-import", action="store_true", help="Also write task 10's component export")
    specimen = commands.add_parser("specimen", help="A project generated from a passport configuration")
    specimen.add_argument("config", type=Path, help="A passport configuration (JSON)")
    specimen.add_argument("target", type=Path, help="A directory that does not exist yet")
    args = parser.parse_args(argv)
    if args.target.exists():
        return _exists(args.target)
    if args.bench == "large":
        try:
            bench = build_large(args.target, with_import=args.with_import)
        except FileExistsError:
            return _exists(args.target)
        except TAKE_REFUSALS as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"large bench in {bench}, digest {digest(bench)}")
        return 0
    try:
        configuration = _read_configuration(args.config)
    except (OSError, ValueError) as error:
        print(f"error: the configuration could not be read ({type(error).__name__})", file=sys.stderr)
        return 2
    try:
        outcome = build_specimen(configuration, args.target, command=command)
    except FileExistsError:
        return _exists(args.target)
    except _GENERATOR.GeneratorNotConfigured as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if isinstance(outcome, _GENERATOR.Rejected):
        print(
            f"error: the specimen generator rejects the configuration ({outcome.lines} lines on its stderr)",
            file=sys.stderr,
        )
        return 2
    if not isinstance(outcome, _GENERATOR.Built):
        print(f"error: the specimen generator failed ({outcome.cause})", file=sys.stderr)
        return 1
    print(f"specimen in {outcome.project}, digest {digest(outcome.project)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
