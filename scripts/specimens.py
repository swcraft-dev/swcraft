"""Re-pin the specimens: generate each from its committed passport, check it, write its digests.

Run with the repository's ``.venv`` interpreter::

    .venv/bin/python scripts/specimens.py pin [--check] [NAME ...]

``pin`` re-pins ``packages/tools/tests/fixtures/specimens/specimens.json`` from the generator ``.venv``
holds. For each named specimen (all by default), in sorted order, it (1) generates the specimen from
``<name>.passport.json`` into a fresh temporary directory; (2) on the generator's exit 2 prints its
stderr, records the specimen as rejected and continues, and on any other failure stops with exit 1;
(3) computes SWCraft's passport of the generated ``model/`` and refuses the specimen on any difference
from the committed passport on a schema leaf it states -- the generator's self-check and SWCraft's pass
are two implementations that must agree; (4) records ``passport_sha256`` and ``model_sha256``. The pin
file is written once, at the end, only when no specimen was refused; the exit is 2 when one was
rejected (the others are pinned).

``--check`` runs steps 1-3 and compares the digests with the pins instead, exit 1 naming every
specimen that differs; it alone runs a candidate generator (``specimen_generator.VARIABLE``). ``pin``
without ``--check`` refuses any generator but ``.venv``'s, and that one when its interpreter is not the
pinned one. Never run in a gate. Progress goes to stdout; refusals, rejections and differences to stderr.

The leaf comparison is defined here once; the test trees load this script by path, since a script
cannot import from them.

What this does not reach: a leaf the committed passport leaves out, and a model property no passport
leaf carries.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

from swcraft_tools.passport import compute_passport, schema_text

REPO_ROOT = Path(__file__).resolve().parent.parent

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

#: What :func:`leaf` answers for a path the document does not have.
ABSENT = object()


def schema_leaf_paths() -> tuple[str, ...]:
    """Every leaf path of the package's passport schema, dotted, in the schema's order."""
    schema = json.loads(schema_text())

    def walk(node: dict[str, Any], at: str) -> list[str]:
        if "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
        if node.get("type") != "object":
            return [at]
        return [path for key, child in node["properties"].items() for path in walk(child, f"{at}.{key}" if at else key)]

    return tuple(walk(schema, ""))


def leaf(document: Any, path: str) -> Any:
    """The value at the dotted *path* of *document*'s mappings, or :data:`ABSENT`."""
    node = document
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return ABSENT
        node = node[key]
    return node


def leaves_differing(stated: dict[str, Any], computed: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """``(leaf path, stated, computed)`` for every schema leaf *stated* carries that *computed* does not meet.

    ``contract_version`` is compared by its major: a contract minor moves it under a committed
    passport, and the gate holds every passport's major to the running contract's.
    """
    differing = []
    for path in schema_leaf_paths():
        want = leaf(stated, path)
        if want is ABSENT:
            continue
        have = leaf(computed, path)
        missing = have is ABSENT
        if path == "contract_version":
            same = not missing and str(have).split(".")[0] == str(want).split(".")[0]
        else:
            same = not missing and have == want
        if not same:
            differing.append((path, want, None if missing else have))
    return differing


def _passport_names(repo_root: Path) -> list[str]:
    directory = repo_root / _GENERATOR.SPECIMENS_DIR
    suffix = _GENERATOR.PASSPORT_SUFFIX
    return sorted(path.name.removesuffix(suffix) for path in directory.glob(f"*{suffix}"))


def _write_pins(repo_root: Path, pins: dict[str, Any]) -> None:
    text = json.dumps(pins, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (repo_root / _GENERATOR.PIN_FILE).write_text(text, encoding="utf-8", newline="\n")


#: :func:`_pin_one`'s outcomes besides an entry: generator exit 2, SWCraft's passport failed or differs,
#: any other generator failure.
_REJECTED = "rejected"
_REFUSED = "refused"
_FAILED = "failed"


def _resolve_command(
    command: list[str] | None, *, check: bool, environ: Mapping[str, str], repo_root: Path, err: TextIO
) -> list[str] | None:
    """The generator to run: *command*, else the configured one; ``None`` after printing a refusal to *err*.

    Without *check*, only ``.venv``'s generator on the pinned interpreter is accepted.
    """
    if command is not None:
        return command
    try:
        argv = _GENERATOR.generator_command(None, environ, repo_root)
    except _GENERATOR.GeneratorNotConfigured as error:
        print(f"pin: {error}", file=err)
        return None
    if check:
        return argv
    installed = _GENERATOR.installed_executable(repo_root)
    if not installed.is_file() or not os.path.samefile(argv[0], installed):
        print(
            f"pin: the configured generator is not the one .venv holds; `pin` re-pins from that alone "
            f"(unset {_GENERATOR.VARIABLE}, or use `pin --check` for a candidate)",
            file=err,
        )
        return None
    refusal = _GENERATOR.interpreter_finding(installed)
    if refusal is not None:
        print(f"pin: {refusal}", file=err)
        return None
    return argv


def _pin_one(
    name: str,
    command: list[str],
    *,
    pinned: Mapping[str, Any],
    repo_root: Path,
    err: TextIO,
) -> dict[str, Any] | str:
    """Steps 1-3 for *name*: its new pin entry, else :data:`_REJECTED`, :data:`_REFUSED` or :data:`_FAILED`.

    Each outcome but an entry is printed to *err* first.
    """
    passport = _GENERATOR.passport_path(repo_root, name)
    with tempfile.TemporaryDirectory(prefix="specimen-pin-") as scratch:
        project = Path(scratch) / name
        outcome = _GENERATOR.run(command, passport, project, keep_stderr=True)
        if isinstance(outcome, _GENERATOR.Rejected):
            print(f"{name}: rejected by the generator ({outcome.lines} lines):", file=err)
            print(outcome.text or "", file=err, end="" if (outcome.text or "").endswith("\n") else "\n")
            return _REJECTED
        if not isinstance(outcome, _GENERATOR.Built):
            print(f"pin: the generator failed on {name}: {outcome.cause}", file=err)
            return _FAILED
        model = project / "model"
        stated = json.loads(passport.read_text(encoding="utf-8"))
        try:
            computed = compute_passport(model).passport
        except Exception as error:  # noqa: BLE001 -- any failure of the pass refuses the specimen
            print(f"{name}: SWCraft's passport of the generated model failed: {type(error).__name__}", file=err)
            return _REFUSED
        differing = leaves_differing(stated, computed)
        if differing:
            for path, want, have in differing:
                print(f"{name}: {path}: the passport states {want!r}, the generated model has {have!r}", file=err)
            return _REFUSED
        return {
            **pinned,
            "passport_sha256": _GENERATOR.file_sha256(passport),
            "model_sha256": _GENERATOR.digest(model),
        }


def pin(
    names: list[str] | None = None,
    *,
    check: bool = False,
    command: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
    stream: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Re-pin (or, with *check*, compare) the named specimens; the exit code. *command* is the tests' seam.

    Progress lines go to *stream* (``None``: stdout); refusals, rejections and differences to *err*
    (``None``: stderr).
    """
    out = stream if stream is not None else sys.stdout
    err = err if err is not None else sys.stderr
    environ = os.environ if environ is None else environ
    try:
        pins = _GENERATOR.load_pins(repo_root)
    except ValueError as error:
        print(f"pin: {error}", file=err)
        return 1
    available = _passport_names(repo_root)
    targets = sorted(set(names)) if names else available
    unknown = [name for name in targets if name not in available]
    if unknown:
        print(f"pin: no committed passport for {', '.join(unknown)}", file=err)
        return 1
    command = _resolve_command(command, check=check, environ=environ, repo_root=repo_root, err=err)
    if command is None:
        return 1
    specimens: dict[str, Any] = dict(pins.get("specimens", {}))
    rejected: list[str] = []
    refused: list[str] = []
    differ: list[str] = []
    for name in targets:
        started = time.monotonic()
        pinned = specimens.get(name, {})
        entry = _pin_one(name, command, pinned=pinned, repo_root=repo_root, err=err)
        if entry == _FAILED:
            return 1
        if isinstance(entry, str):
            (rejected if entry == _REJECTED else refused).append(name)
            continue
        if check and (
            pinned.get("model_sha256") != entry["model_sha256"]
            or pinned.get("passport_sha256") != entry["passport_sha256"]
        ):
            print(
                f"{name}: generated {entry['model_sha256']} from passport {entry['passport_sha256']}; "
                f"pinned {pinned.get('model_sha256')} from {pinned.get('passport_sha256')}",
                file=err,
            )
            differ.append(name)
        specimens[name] = entry
        print(f"{name}: built and met in {time.monotonic() - started:.1f} s", file=out)
    if refused:
        print(f"pin: refused {', '.join(refused)}; the pin file is unchanged", file=err)
        return 1
    if check:
        if differ:
            print(f"pin --check: {', '.join(differ)} differ from the pins", file=err)
            return 1
        return 2 if rejected else 0
    if not names:
        specimens = {name: entry for name, entry in specimens.items() if name in available}
    pins["specimens"] = dict(sorted(specimens.items()))
    _write_pins(repo_root, pins)
    if rejected:
        print(f"pin: rejected by the generator: {', '.join(rejected)} (the others are pinned)", file=err)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-pin the specimens from their committed passports.")
    sub = parser.add_subparsers(dest="command", required=True)
    pin_parser = sub.add_parser("pin", help="Generate, check and pin each specimen")
    pin_parser.add_argument("--check", action="store_true", help="Compare with the pins; write nothing")
    pin_parser.add_argument("names", nargs="*", metavar="NAME", help="The specimens (default: all)")
    arguments = parser.parse_args(argv)
    return pin(arguments.names or None, check=arguments.check)


if __name__ == "__main__":
    sys.exit(main())
