"""The ``toolchain_version`` pin's sentences: a mismatch named, or the pin withheld.

A leaf shared by the CLI and the guard hook so both report a mismatch in the same words; it
imports nothing of ``cli`` or ``validator``, which keeps the guard hook's start-up cheap.
"""

from __future__ import annotations

from swcraft_tools._version import __version__
from swcraft_tools.project import SwcraftProject, toolchain_versions_agree


def toolchain_pin_problem(project: SwcraftProject, *, withhold_pin: bool = False) -> str | None:
    """The one sentence a mismatched ``toolchain_version`` produces, or ``None``.

    ``None`` for no pin or one of this release in any spelling (``0.7`` is ``0.7.0``). The sentence
    names both versions: the check is an equality, not an ordering. *withhold_pin* (``swcraft
    passport``, whose stderr travels with a bug report) returns a sentence that never names the pinned value.
    """
    pin = project.toolchain_version
    if pin is None or toolchain_versions_agree(pin, __version__):
        return None
    if withhold_pin:
        return withheld_pin_sentence()
    return (
        f"this project's swcraft.yaml pins toolchain_version {pin} and this is SWCraft {__version__} — "
        f"install the {pin} workstation archive (its swcraft_install.cmd on Windows, swcraft_install.sh "
        "elsewhere), or edit the pin if this project is meant to move to this release."
    )


def withheld_pin_sentence() -> str:
    """``swcraft passport``'s pin-mismatch sentence: the field and this release, never the pinned value.

    PEP 440 lets the pin carry a local label (``9.9.9+anything``), which is text the user chose, so
    only the field and the running release are named. What it does not reach: which release is pinned.
    """
    return (
        f"this project's swcraft.yaml pins toolchain_version to another release than this one, SWCraft "
        f"{__version__} (the pinned value is withheld) — install the workstation archive of the release it "
        "pins, or edit the pin if this project is meant to move to this release."
    )
