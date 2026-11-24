"""The presets the specimen generator ships as package data, taken by name.

The WATERS production presets live under `swcraft_specimens/presets/`, the specimen presets under
`presets/specimens/`. A preset's name is its file stem; the stems are distinct across both
directories.

`generate` takes a name or a path. An argument with no path separator and no suffix is a name,
and an unknown name is refused with the list of names; anything else is a path. A file named like
a preset is therefore reached as `./small`, never as `small`.

The directories are read through `importlib.resources`, so the presets resolve the same way from a
wheel, an editable install or a source checkout.
"""

from importlib.resources import files
from pathlib import Path, PurePath

from .errors import PresetError

#: The package-relative directories that hold presets, production first.
PRESET_DIRECTORIES = ("presets", "presets/specimens")


def _directory(relative: str) -> Path:
    return Path(str(files("swcraft_specimens").joinpath(*relative.split("/"))))


def packaged_presets() -> dict[str, Path]:
    """Every packaged preset by name, production presets first, each directory sorted."""
    presets: dict[str, Path] = {}
    for relative in PRESET_DIRECTORIES:
        for path in sorted(_directory(relative).glob("*.yaml"), key=lambda p: p.name):
            if path.stem in presets:
                raise PresetError(f"two packaged presets are named {path.stem!r}")
            presets[path.stem] = path
    return presets


def preset_names() -> tuple[str, ...]:
    """The names `generate` accepts, in `packaged_presets` order."""
    return tuple(packaged_presets())


def preset_listing() -> str:
    """The help text's list of names, one line per directory, for `--help`."""
    lines = ["packaged presets (generate NAME):"]
    for relative in PRESET_DIRECTORIES:
        names = sorted(path.stem for path in _directory(relative).glob("*.yaml"))
        label = "specimens" if relative.endswith("specimens") else "production"
        lines.append(f"  {label}: {', '.join(names)}")
    return "\n".join(lines)


def preset_path(name: str) -> Path:
    """The packaged preset `name`; an unknown name is refused, naming every preset."""
    presets = packaged_presets()
    if name not in presets:
        raise PresetError(f"unknown preset {name!r}; the presets are: {', '.join(presets)}")
    return presets[name]


def is_preset_name(argument: str) -> bool:
    """Whether a `generate` argument is a name: no path separator and no suffix."""
    return "/" not in argument and "\\" not in argument and PurePath(argument).suffix == ""


def resolve_preset(argument: str) -> Path:
    """The file a `generate` argument names: a packaged preset by name, else the path itself."""
    return preset_path(argument) if is_preset_name(argument) else Path(argument)
