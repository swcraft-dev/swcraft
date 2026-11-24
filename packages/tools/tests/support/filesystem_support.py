"""Ask the filesystem whether it accepts a path segment, and report what it said.

Some frozen rows of ``packages/tools/tests/fixtures/recorded_oracles/writer_refusals.json``
record a property of the recording host's filesystem, not of SWCraft: a segment
carrying U+0001 became a real directory there, and a 300-character segment failed
as ``ENAMETOOLONG``. Neither holds everywhere (Windows refuses the first with
``WinError 123``).

**This module skips nothing.** A ``skipif(sys.platform == "win32")`` would delete
the claim (``path_refusal`` refuses where ``compose_storage_path`` validates
nothing) on the platform where folding the policy into the composer looks most
attractive. The helpers decide only *which* of two outcomes must follow, never
whether anything is checked. A probe that grows a ``pytest.skip`` is a design error.

**What it does not reach.**

- *segment* must be a single ordinary path component; ``"."`` and ``".."`` are not
  (``mkdir`` answers ``FileExistsError`` about something else). No caller probes them.
- It probes with ``mkdir`` while some rows compose a *file* name; for the properties
  declared (characters, length) the questions coincide, but a volume treating
  directory and file names differently would defeat this unnoticed.
- It answers for the filesystem under *root*, once, at the moment of the call; every
  caller probes the ``tmp_path`` its subject writes into.
- The errno it hands back is the host's, recorded and compared, never interpreted;
  no caller may assert a platform's errno this module has not been asked for.

:func:`case_sensitive_under`, :func:`listing_refusal_for_a_non_directory` and
:func:`stores_a_device_named_file_under` ask further questions on the same terms.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def path_segment_refusal(root: Path, segment: str) -> OSError | None:
    """Try to create *segment* as a directory under *root*; return the refusal.

    ``None`` means the filesystem accepted it; otherwise the ``OSError`` is carried
    back whole so a caller compares its ``errno`` (or ``winerror``), not a message.
    The probe works in a private temporary directory under *root* and removes it.
    """
    probe_root = Path(tempfile.mkdtemp(prefix="segment-probe-", dir=str(root)))
    try:
        (probe_root / segment).mkdir()
    except OSError as refusal:
        return refusal
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
    return None


def accepts_path_segment(root: Path, segment: str) -> bool:
    """Whether the filesystem under *root* accepts *segment* as a component.

    A real attempt, not a ``sys.platform`` check: a Windows machine, a container
    bind mount and a case-insensitive DMG differ on the same ``sys.platform``.
    """
    return path_segment_refusal(root, segment) is None


def case_sensitive_under(root: Path) -> bool:
    """Whether two file names differing only in case are two files under *root*.

    ``rule:storage.filename-case-collision`` can be exercised only where this is
    ``True`` (ext4, case-sensitive APFS); where ``False`` a test asserts the rule's
    silence instead — it branches, never skips. It writes ``a.yaml`` and ``A.yaml``
    into a private temporary directory and counts the entries.

    **What it does not reach.**

    - It answers for the filesystem under *root*, once, at the moment of the call.
    - It asks about ASCII case only; Unicode normalisation and non-ASCII case
      mapping are different questions a volume may answer differently.
    """
    probe_root = Path(tempfile.mkdtemp(prefix="case-probe-", dir=str(root)))
    try:
        (probe_root / "a.yaml").write_text("lower\n", encoding="utf-8", newline="\n")
        (probe_root / "A.yaml").write_text("upper\n", encoding="utf-8", newline="\n")
        return len(list(probe_root.iterdir())) == 2
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def listing_refusal_for_a_non_directory(root: Path) -> OSError | None:
    """Try to list a **regular file** as a directory under *root*; return the refusal.

    ``model_state._held_names`` must tell a directory, an absent directory and a
    file where a layer directory belongs apart. The third is ``NotADirectoryError``
    on POSIX; on Windows the class depends on which path component is the file and
    is unmeasured, so a caller branches on this answer instead of asserting a class.
    ``None`` (a file listed without complaint) is expected by no caller, and each
    must state it rather than read it as agreement.

    **What it does not reach.**

    - It asks about the file **itself**; a name *below* a file (``<root>/file/below``)
      is a second question a platform may answer differently.
    - It answers for the filesystem under *root*, once, at the moment of the call.
    - The error it hands back is the host's, carried whole and never interpreted.
    """
    probe_root = Path(tempfile.mkdtemp(prefix="listing-probe-", dir=str(root)))
    try:
        occupant = probe_root / "not-a-directory"
        occupant.write_text("probe\n", encoding="utf-8", newline="\n")
        with os.scandir(occupant) as entries:
            list(entries)
    except OSError as refusal:
        return refusal
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
    return None


def stores_a_device_named_file_under(root: Path, name: str = "NUL.yaml") -> bool:
    """Whether a file named *name* -- a device name Windows reserves -- is an ordinary file under *root*.

    ``rule:storage.reserved-device-name`` can report such a file only where this is
    ``True`` (POSIX); on Windows the name opens a device and nothing is stored, so a
    test asserts the rule's silence there. It checks that the directory **lists** the
    file, not that the write succeeded, because a write to ``NUL`` succeeds on
    Windows and stores nothing. ``NUL.yaml`` is the default because the null device
    discards the bytes; ``CON`` would print to a Windows console.

    **What it does not reach.**

    - It answers for the one *name* asked, once, at the moment of the call; Windows
      releases that stopped treating a name with an extension as a device are
      unmeasured.
    - It answers for the filesystem under *root*, exactly as its siblings do.
    """
    probe_root = Path(tempfile.mkdtemp(prefix="device-probe-", dir=str(root)))
    try:
        try:
            (probe_root / name).write_text("probe\n", encoding="utf-8", newline="\n")
        except OSError:
            return False
        return [entry.name for entry in probe_root.iterdir() if entry.is_file()] == [name]
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
