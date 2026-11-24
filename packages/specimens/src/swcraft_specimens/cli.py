"""Command-line interface for the specimen generator.

Three commands:

- `generate PRESET --out DIR` generates a project from a preset of either kind, named (a packaged
  preset) or given by its path;
- `preset-from-passport PASSPORT --out PRESET [--name NAME]` writes the specimen preset of a
  passport, keys in schema order;
- `specimen PASSPORT --out DIR [--name NAME]` is the two composed.

A specimen whose layout needs the entry planner's exact program prints one notice line on stderr;
it is not an error. `--toolchain-version VERSION` on `generate` and `specimen` sets the SWCraft
release `swcraft.yaml` names, and must be one exact PEP 440 release (else exit 2); without it the
installed `swcraft-tools` version is named when it is one, and otherwise none (`toolchain.py`).

Exit codes: 0 on success; 1 for a `GenerationError`, a defect of the gates; 2 for any other
`SpecimenGeneratorError`, a configuration error. A refused passport prints one line per refused leaf,
`refused: <dotted.leaf> (<class>): <sentence>`, with no prefix.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .errors import GenerationError, OutputError, PresetError, SpecimenGeneratorError
from .generate import generate_project
from .output import validate_output
from .packaged import preset_listing, resolve_preset
from .passport import PassportRefused, read_passport
from .preset import Preset, load_any_preset
from .specimen.build import generate_specimen
from .specimen.preset import SpecimenPreset, parse_specimen_text, preset_text
from .toolchain import explicit_toolchain_version

Generator = Callable[[Preset, Path], None]
SpecimenGenerator = Callable[[SpecimenPreset, Path], None]


def build_parser() -> argparse.ArgumentParser:
    presets = preset_listing()
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(prog="swcraft-specimens", epilog=presets, formatter_class=raw)
    parser.add_argument("--version", action="version", version=f"swcraft-specimens {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser(
        "generate", help="generate an architecture project", epilog=presets, formatter_class=raw
    )
    generate.add_argument(
        "preset",
        help="a packaged preset's name, or the path to a YAML preset (./NAME for a file named like a preset)",
    )
    generate.add_argument("--out", required=True, type=Path, help="output directory")
    _toolchain_argument(generate)
    from_passport = commands.add_parser("preset-from-passport", help="write the specimen preset of a model passport")
    from_passport.add_argument("passport", type=Path, help="path to a passport JSON file")
    from_passport.add_argument("--out", required=True, type=Path, help="preset file to write")
    from_passport.add_argument("--name", help="the specimen project's name (an identifier)")
    specimen = commands.add_parser("specimen", help="generate a specimen from a model passport")
    specimen.add_argument("passport", type=Path, help="path to a passport JSON file")
    specimen.add_argument("--out", required=True, type=Path, help="output directory")
    specimen.add_argument("--name", help="the specimen project's name (an identifier)")
    _toolchain_argument(specimen)
    return parser


def _toolchain_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--toolchain-version",
        metavar="VERSION",
        help=(
            "the SWCraft release swcraft.yaml names "
            "(default: the installed swcraft-tools' when it is an exact release, else none)"
        ),
    )


def _passport_preset_text(path: Path, name: str | None) -> str:
    """The specimen preset text of a passport file: read strictly, validated, in schema order."""
    if not path.exists():
        raise PresetError(f"passport not found: {path}")
    if not path.is_file():
        raise PresetError(f"passport is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PresetError(f"cannot read passport {path}: {exc}") from None
    return preset_text(read_passport(text), name)


def _write_preset(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        raise OutputError(f"output preset already exists: {path}")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except OSError as exc:
        raise OutputError(f"cannot write preset {path}: {exc}") from None


def _notice(line: str) -> None:
    """A notice of generation (the entry planner's exact program), one line on stderr."""
    print(line, file=sys.stderr)


def _generate_specimen(preset: SpecimenPreset, output: Path) -> None:
    generate_specimen(preset, output, notify=_notice)


def run(
    argv: Sequence[str] | None = None,
    *,
    generator: Generator = generate_project,
    specimen_generator: SpecimenGenerator = _generate_specimen,
) -> int:
    """Run the CLI and return its documented exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "generate":
            preset = load_any_preset(resolve_preset(args.preset))
            validate_output(args.out)
            with explicit_toolchain_version(args.toolchain_version):
                if isinstance(preset, SpecimenPreset):
                    specimen_generator(preset, args.out)
                else:
                    generator(preset, args.out)
        elif args.command == "preset-from-passport":
            _write_preset(args.out, _passport_preset_text(args.passport, args.name))
        elif args.command == "specimen":
            text = _passport_preset_text(args.passport, args.name)
            specimen = parse_specimen_text(text, str(args.passport))
            validate_output(args.out)
            with explicit_toolchain_version(args.toolchain_version):
                specimen_generator(specimen, args.out)
        else:
            parser.error(f"unsupported command: {args.command}")
    except PassportRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SpecimenGeneratorError as exc:
        code = 1 if isinstance(exc, GenerationError) else 2
        print(f"error: {exc}", file=sys.stderr)
        return code
    return 0


def main() -> int:
    """Main entry point."""
    return run()
