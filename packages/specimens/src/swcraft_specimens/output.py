"""Non-destructive output-directory preflight checks."""

from pathlib import Path

from .errors import OutputError


def validate_output(path: Path) -> None:
    """Ensure *path* is a safe future generation target without creating it."""
    if path.exists():
        if path.is_file():
            raise OutputError(f"output path is a file: {path}")
        if not path.is_dir():
            raise OutputError(f"output path is not a directory: {path}")
        try:
            next(path.iterdir())
        except StopIteration:
            return
        except OSError as exc:
            raise OutputError(f"cannot inspect output directory {path}: {exc}") from None
        raise OutputError(f"output directory is not empty: {path}")
