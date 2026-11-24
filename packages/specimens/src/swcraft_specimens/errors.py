"""Errors which are safe to present at the CLI boundary."""


class SpecimenGeneratorError(Exception):
    """Base class for expected specimen generator failures."""


class PresetError(SpecimenGeneratorError):
    """The preset cannot be loaded or has the wrong root type."""


class OutputError(SpecimenGeneratorError):
    """The requested output path is not safe to use."""


class GenerationError(SpecimenGeneratorError):
    """Generation failed or is not available yet."""
