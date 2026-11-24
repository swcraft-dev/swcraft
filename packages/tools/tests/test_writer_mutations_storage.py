"""The ``create_*`` writers compose their targets through the kernel's ``compose_path``.

Each layer's directory and file suffix come from ``model-contract.json``'s ``storage`` block. Two frozen oracles
hold what the writers accept and refuse: ``writer_paths.json`` (the composed targets) and ``writer_refusals.json``
(per function, six identity shapes and, for ``create_swc``, five group shapes, each with the exception it raises,
or none). A row changes only when its function changes or goes.

The table is why ``compose_path`` validates nothing: ``create_swc`` accepts a bare ``.`` group and a
control-character group and lets an over-long one reach the filesystem as an ``OSError``, which the guarded composer
in ``editing/registry.py`` refuses; routing the writers through it would tighten public input validation.

**What this suite does not reach.**

- The ``add_*`` / ``set_*`` / ``unset_*`` mutations (``test_reader_generic.py``).
- Refusals the table does not probe, and the name guards beyond the six shapes (``test_reserved_name_policies.py``,
  ``test_identifier_coverage.py``); that ``writer/`` imports no ``editing.*``
  (``test_editing_package_boundaries.py``).
- ``OSError`` message text: asserted by ``errno``, which differs between hosts.
- Nine rows record the recording host's filesystem, not SWCraft, under a ``filesystem_dependent`` declaration (a
  U+0001 component, an over-long component's errno); the replay asks this host through :mod:`filesystem_support`
  which outcome to require.
"""

from __future__ import annotations

import ast
import copy
import errno
import inspect
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import filesystem_support
import pytest
import yaml
from element_identity_support import declared_identity
from swcraft_tools import exclusive_create
from swcraft_tools.auditor import AuditLog
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.model_registry import (
    ModelRegistry,
    build_model_registry,
    path_refusal,
    production_registry,
    use_registry,
)
from swcraft_tools.model_state import free_storage_path
from swcraft_tools.writer import mutations

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded_oracles"

#: The two ``filesystem_dependent`` declarations ``writer_refusals.json`` carries and
#: this module branches on; constants so that
#: :func:`test_every_filesystem_dependent_declaration_is_one_the_replay_reads` can hold
#: the fixture's vocabulary to exactly this pair and a typo is not silent.
CONTROL_CHARACTER_COMPONENT = "control-character-in-a-component"
OVER_LONG_COMPONENT = "over-long-component"


# ---------------------------------------------------------------------------
# The frozen oracles and the model they were recorded over
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def paths_oracle() -> dict[str, Any]:
    """``writer_paths.json``, shared per module because it is frozen evidence no test mutates."""
    return json.loads((FIXTURE_ROOT / "writer_paths.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def refusals_oracle() -> dict[str, Any]:
    """``writer_refusals.json`` — read, never written."""
    return json.loads((FIXTURE_ROOT / "writer_refusals.json").read_text(encoding="utf-8"))


@pytest.fixture
def model_root(tmp_path: Path, paths_oracle: dict[str, Any]) -> str:
    """A fresh copy of the model both oracles were recorded against.

    One per test because every probe writes, and a shared copy would make a second
    create of the same element hit the duplicate check instead of the composition.
    """
    destination = tmp_path / "model"
    shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], destination)
    return str(destination)


def _call(function: str, root: str, arguments: dict[str, Any]) -> str:
    return getattr(mutations, function)(root, **arguments)


def _relative(result: str, root: str) -> str:
    return Path(result).relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# The twelve frozen targets
# ---------------------------------------------------------------------------


def test_every_frozen_target_is_reproduced(paths_oracle, tmp_path, request) -> None:
    """Every frozen target is reproduced, each row against its own copy of the recorded model."""
    for index, target in enumerate(paths_oracle["targets"]):
        root = tmp_path / f"model-{index}"
        shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
        result = _call(target["function"], str(root), target["arguments"])
        assert _relative(result, str(root)) == target["path"], target["site"]


def test_the_oracle_covers_every_public_create_function(paths_oracle) -> None:
    """The target rows name every ``create_*`` the module exposes, so none is unconstrained."""
    exposed = {name for name in dir(mutations) if name.startswith("create_")}
    assert {target["function"] for target in paths_oracle["targets"]} == exposed


# ---------------------------------------------------------------------------
# The frozen accept/refuse table
# ---------------------------------------------------------------------------


def _probe_arguments(
    paths_oracle: dict[str, Any], function: str, identity_parameter: str, case: str, probe_values: dict[str, str]
) -> dict[str, Any]:
    """The baseline call with the one probed argument substituted."""
    baseline = next(t for t in paths_oracle["targets"] if t["function"] == function)
    arguments = dict(baseline["arguments"])
    if case == "accepted_baseline":
        return arguments
    parameter = "group" if case.startswith("group_") else identity_parameter
    arguments[parameter] = probe_values[case]
    return arguments


def _cases(refusals_oracle: dict[str, Any]) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    for entry in refusals_oracle["functions"]:
        for case, expected in entry["cases"].items():
            yield entry["function"], entry["identity_parameter"], case, expected


def _assert_filesystem_errno(failure: OSError, host_refusal: OSError | None, label: str) -> None:
    """Assert the errno of a create that reached the filesystem, without inventing one.

    ``ENAMETOOLONG`` is what the recording host raised and stays the assertion
    wherever this filesystem raises it too. Where the host disagrees (a Windows
    volume past ``MAX_PATH``, its errno unmeasured), the create must fail with the
    host's own errno for the same segment, obtained by probing; no platform's errno
    is encoded here.
    """
    if host_refusal is not None and host_refusal.errno != errno.ENAMETOOLONG:
        assert failure.errno == host_refusal.errno, (
            f"{label}: this filesystem refuses the segment with errno {host_refusal.errno} "
            f"({host_refusal}), but create_* failed with errno {failure.errno} ({failure})"
        )
        return
    assert failure.errno == errno.ENAMETOOLONG, label


def _assert_frozen_verdict(
    function: str,
    arguments: dict[str, Any],
    expected: dict[str, Any],
    segment: str,
    root: Path,
    probe_root: Path,
    label: str,
    support: Any = filesystem_support,
) -> None:
    """Replay one frozen row against live code; *support* is the seam the not-blind case stubs."""
    dependency = expected.get("filesystem_dependent")

    if expected["accepted"]:
        if dependency == CONTROL_CHARACTER_COMPONENT and not support.accepts_path_segment(probe_root, segment):
            # This host refuses a component the recording host accepted, so the
            # frozen path cannot arrive; the refusal must still be the
            # filesystem's, not a ValueError from a composer that validates.
            try:
                _call(function, str(root), arguments)
            except OSError:
                return
            except ValueError as refusal:  # pragma: no cover - the regression this row exists for
                raise AssertionError(
                    f"{label}: create_* refused {segment!r} with {refusal!r}; the kernel's segment "
                    "policy has reached the writers' composer and tightened public input validation"
                ) from refusal
            raise AssertionError(
                f"{label}: the probe says this filesystem refuses the component {segment!r}, "
                "but create_* returned a path"
            )
        try:
            result = _call(function, str(root), arguments)
        except OSError as refusal:
            # Without this a bare OSError would leave the replay naming no row.
            # A declared row means the probe was consulted and was wrong; an
            # undeclared row never asked and owes the fixture a declaration. The
            # not-blind case tells its two halves apart by these two messages.
            if dependency == CONTROL_CHARACTER_COMPONENT:
                raise AssertionError(
                    f"{label}: the probe says this filesystem accepts the component {segment!r}, "
                    f"but create_* failed with {refusal!r}"
                ) from refusal
            raise AssertionError(
                f"{label}: this row carries no filesystem_dependent declaration, and this "
                f"filesystem refused the component {segment!r} with {refusal!r} -- a row whose "
                "verdict depends on the host must declare which host property it records"
            ) from refusal
        assert _relative(result, str(root)) == expected["path"], label
        return

    host_refusal = support.path_segment_refusal(probe_root, segment) if dependency == OVER_LONG_COMPONENT else None
    with pytest.raises(Exception) as caught:  # noqa: B017 - the type is the assertion
        _call(function, str(root), arguments)
    assert type(caught.value).__name__ == expected["exception"], label
    if isinstance(caught.value, OSError):
        _assert_filesystem_errno(caught.value, host_refusal, label)
    else:
        assert str(caught.value).startswith(expected["message"]), label


def test_every_frozen_verdict_is_unchanged(paths_oracle, refusals_oracle, tmp_path) -> None:
    """Every probe is accepted or refused as frozen, with the frozen exception type.

    These asymmetries are why the kernel composer is pure: ``create_swc`` accepts a
    bare ``.`` group (pathlib folds it into ``swc/``) and a control-character group,
    and ``create_requirement``/``create_task``/``create_hardware_element`` accept a
    bare ``.`` identity and write ``..yaml``. ``create_composition`` and
    ``create_connector`` run the AUTOSAR ``Identifier`` check after the path guard,
    so they refuse all three shapes before the filesystem is asked.

    The nine ``filesystem_dependent`` rows go through :mod:`filesystem_support`,
    which asks this host with a real ``mkdir`` (never ``sys.platform``) which of two
    outcomes must follow; nothing is skipped.
    """
    probe_root = tmp_path / "segment-probes"
    probe_root.mkdir()
    for index, (function, identity_parameter, case, expected) in enumerate(_cases(refusals_oracle)):
        root = tmp_path / f"model-{index}"
        shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
        arguments = _probe_arguments(paths_oracle, function, identity_parameter, case, refusals_oracle["probe_values"])
        _assert_frozen_verdict(
            function,
            arguments,
            expected,
            refusals_oracle["probe_values"].get(case, ""),
            root,
            probe_root,
            f"{function}/{case}",
        )


def test_every_filesystem_dependent_declaration_is_one_the_replay_reads(refusals_oracle) -> None:
    """The fixture's ``filesystem_dependent`` vocabulary is exactly the two words the replay branches on.

    A declaration nothing reads is an exemption for nothing: a third property would
    be ignored, and a misspelling would take the undeclared path and pass wherever
    that path happens to work. The counts: the control-character property is
    recorded by ``create_swc``'s group row and the three
    ``identity_control_character`` rows, the over-long property by the five
    ``OSError`` rows. ``create_composition`` and ``create_connector`` carry none:
    they refuse both shapes before the filesystem is reached.

    **Does not reach** whether an undeclared row should be declared: the four
    ``identity_bare_dot`` rows write ``..yaml`` and stay undeclared because no host
    has been observed refusing it.
    """
    declared = {
        (entry["function"], case): expected["filesystem_dependent"]
        for entry in refusals_oracle["functions"]
        for case, expected in entry["cases"].items()
        if "filesystem_dependent" in expected
    }
    known = {CONTROL_CHARACTER_COMPONENT, OVER_LONG_COMPONENT}
    assert set(declared.values()) == known, declared
    assert set(refusals_oracle["filesystem_dependent_properties"]) == known
    assert sorted(declared.values()).count(CONTROL_CHARACTER_COMPONENT) == 4
    assert sorted(declared.values()).count(OVER_LONG_COMPONENT) == 5


class _RefusingHost:
    """A filesystem stub that refuses every component, as Windows refuses U+0001.

    ``EINVAL`` is what Python reports for ``WinError 123``; it is a stub value that
    witnesses a branch, never an assertion about Windows.
    """

    @staticmethod
    def accepts_path_segment(root: Path, segment: str) -> bool:
        return False

    @staticmethod
    def path_segment_refusal(root: Path, segment: str) -> OSError:
        return OSError(errno.EINVAL, "Invalid argument")


class _AcceptingHost:
    """A filesystem stub that accepts every component, as Linux and macOS accept U+0001.

    The not-blind case needs both stubs, because the stub must say the opposite of
    the host it runs on. Its over-long refusal is ``ENAMETOOLONG``, an accepting
    host's own, so the over-long row falls back onto the frozen constant.
    """

    @staticmethod
    def accepts_path_segment(root: Path, segment: str) -> bool:
        return True

    @staticmethod
    def path_segment_refusal(root: Path, segment: str) -> OSError:
        return OSError(errno.ENAMETOOLONG, "File name too long")


def _host_refusing_with(code: int) -> Any:
    """A stub whose ``path_segment_refusal`` carries *code*.

    The over-long row is refused everywhere measured, so it is contradicted on the
    errno, which must be one this host did not raise; hosts raise different ones.
    """

    class _Host:
        @staticmethod
        def accepts_path_segment(root: Path, segment: str) -> bool:
            return False

        @staticmethod
        def path_segment_refusal(root: Path, segment: str) -> OSError:
            return OSError(code, "a refusal this host did not make")

    return _Host


def _an_errno_this_host_did_not_raise(real: OSError | None) -> int:
    """An errno that is neither the host's own nor ``ENAMETOOLONG``.

    ``ENAMETOOLONG`` is excluded because :func:`_assert_filesystem_errno` reads it
    as agreement with the frozen row, so a stub carrying it would contradict nothing.
    """
    if real is not None and real.errno == errno.EACCES:
        return errno.EIO
    return errno.EACCES


def _contradicting_host(probe_root: Path, segment: str, row: dict[str, Any]) -> tuple[Any, str]:
    """The stub that disagrees with *this* filesystem about *this* row.

    The not-blind case lies to the replay and watches the row fail, so the lie must
    be one here: a fixed stub merely agrees with some host and lets the case pass
    there for the wrong reason. The accepted row is contradicted on acceptance, the
    refusing row on the errno.

    Does not reach a host that accepts the over-long segment: the create then
    succeeds and the case fails as pytest's ``DID NOT RAISE``. No measured host does.
    """
    if row["accepted"]:
        if filesystem_support.accepts_path_segment(probe_root, segment):
            return _RefusingHost, "refuses the component"
        return _AcceptingHost, "accepts the component"
    real = filesystem_support.path_segment_refusal(probe_root, segment)
    return _host_refusing_with(_an_errno_this_host_did_not_raise(real)), "refuses the segment with errno"


def _replay_with_a_stubbed_host(
    paths_oracle: dict[str, Any],
    refusals_oracle: dict[str, Any],
    case: str,
    expected: dict[str, Any],
    root: Path,
    support: Any,
) -> None:
    """One ``create_swc`` row through :func:`_assert_frozen_verdict`, host stubbed."""
    shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
    arguments = _probe_arguments(paths_oracle, "create_swc", "name", case, refusals_oracle["probe_values"])
    _assert_frozen_verdict(
        "create_swc",
        arguments,
        expected,
        refusals_oracle["probe_values"][case],
        root,
        root.parent,
        f"not-blind/{case}",
        support=support,
    )


def _swc_row(refusals_oracle: dict[str, Any], case: str) -> dict[str, Any]:
    entry = next(e for e in refusals_oracle["functions"] if e["function"] == "create_swc")
    return entry["cases"][case]


@pytest.mark.parametrize(
    ("case", "declaration"),
    [
        ("group_control_character", CONTROL_CHARACTER_COMPONENT),
        ("group_over_long", OVER_LONG_COMPONENT),
    ],
)
def test_a_filesystem_dependent_row_is_not_replayed_blind(
    paths_oracle, refusals_oracle, tmp_path, case, declaration
) -> None:
    """Each declaration is read, on its row, spelled that way, proven both ways.

    On the host whose answers were frozen both filesystem-dependent branches
    collapse onto the undeclared one, so ``test_every_frozen_verdict_is_unchanged``
    alone would miss a misspelled or misread declaration. The probe is stubbed to
    say the opposite of this host, over the real ``create_swc``:

    - with the declaration, the stub is obeyed and the row fails with the branch's
      message;
    - without it, the stub is no longer obeyed, so whatever follows is not the
      stub's complaint (not "the row passes": on a host that disagrees with the
      recording host the undeclared row must fail).

    Where the host agrees with the frozen row the failure disappears; where it
    disagrees it changes to the host's own. :func:`_contradicting_host` picks the
    stub per row and per host, and :class:`TestTheContradictionIsChosenAgainstThisHost`
    asserts that choice in both directions.

    **Does not reach** the eight other declared rows' own wiring;
    ``test_every_filesystem_dependent_declaration_is_one_the_replay_reads`` holds
    their vocabulary and count.
    """
    row = _swc_row(refusals_oracle, case)
    assert row["filesystem_dependent"] == declaration
    segment = refusals_oracle["probe_values"][case]
    support, message = _contradicting_host(tmp_path, segment, row)

    with pytest.raises(AssertionError, match=message):
        _replay_with_a_stubbed_host(paths_oracle, refusals_oracle, case, row, tmp_path / "declared", support)

    undeclared = {key: value for key, value in row.items() if key != "filesystem_dependent"}
    try:
        _replay_with_a_stubbed_host(paths_oracle, refusals_oracle, case, undeclared, tmp_path / "undeclared", support)
    except AssertionError as without_the_declaration:
        # Not "it passes": that holds only on the host whose answers were frozen.
        # What holds everywhere is that the stub is no longer obeyed.
        assert not re.search(message, str(without_the_declaration)), (
            "with the declaration removed the stub was still obeyed, so nothing here reads it: "
            f"{without_the_declaration}"
        )


def test_the_branch_a_refusing_host_takes_is_itself_exercised(paths_oracle, refusals_oracle, tmp_path) -> None:
    """The refusing-host branch runs to a green end on this host, not only on Windows.

    No accepted row is refused by this filesystem, so the accepted
    ``group_control_character`` expectation is spliced onto ``group_over_long``'s
    arguments, a component this filesystem refuses too: the shape a Windows volume
    presents for U+0001. The branch must accept the filesystem's ``OSError``
    without asserting the frozen path. The fixture is not touched.
    """
    spliced = dict(_swc_row(refusals_oracle, "group_control_character"))
    assert spliced["accepted"] and spliced["filesystem_dependent"] == CONTROL_CHARACTER_COMPONENT
    _replay_with_a_stubbed_host(
        paths_oracle, refusals_oracle, "group_over_long", spliced, tmp_path / "refused", _RefusingHost
    )


class TestTheContradictionIsChosenAgainstThisHost:
    """The not-blind stub disagrees with the filesystem it runs on, in both directions.

    A fixed refusing stub merely agrees with a host that refuses U+0001, and the
    not-blind case then passes there for the wrong reason. The probe is stubbed
    because one host can see only one direction.
    """

    _SEGMENT = "irrelevant-to-the-selection"

    def _row(self, accepted: bool) -> dict[str, Any]:
        return {"accepted": accepted}

    def test_an_accepting_host_is_contradicted_by_a_refusing_stub(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(filesystem_support, "accepts_path_segment", lambda root, segment: True)

        support, message = _contradicting_host(tmp_path, self._SEGMENT, self._row(accepted=True))

        assert support is _RefusingHost
        assert not support.accepts_path_segment(tmp_path, self._SEGMENT)
        assert message == "refuses the component"

    def test_a_refusing_host_is_contradicted_by_an_accepting_stub(self, tmp_path, monkeypatch) -> None:
        """The Windows direction, unreachable on this host any other way."""
        monkeypatch.setattr(filesystem_support, "accepts_path_segment", lambda root, segment: False)

        support, message = _contradicting_host(tmp_path, self._SEGMENT, self._row(accepted=True))

        assert support is _AcceptingHost
        assert support.accepts_path_segment(tmp_path, self._SEGMENT)
        assert message == "accepts the component", "and the complaint has to be the other one too"

    def test_the_refusing_row_is_contradicted_on_the_errno_not_on_acceptance(self, tmp_path, monkeypatch) -> None:
        """Refused everywhere measured; only the code differs, so only it can lie."""
        monkeypatch.setattr(
            filesystem_support,
            "path_segment_refusal",
            lambda root, segment: OSError(errno.ENAMETOOLONG, "File name too long"),
        )

        support, message = _contradicting_host(tmp_path, self._SEGMENT, self._row(accepted=False))

        code = support.path_segment_refusal(tmp_path, self._SEGMENT).errno
        assert code != errno.ENAMETOOLONG, "a stub carrying it contradicts nothing -- _assert_filesystem_errno defers"
        assert message == "refuses the segment with errno"

    def test_the_windows_errno_is_contradicted_too(self, tmp_path, monkeypatch) -> None:
        """``EINVAL`` is what that host raised, so the stub may not be ``EINVAL``."""
        monkeypatch.setattr(
            filesystem_support,
            "path_segment_refusal",
            lambda root, segment: OSError(errno.EINVAL, "Invalid argument"),
        )

        support, _ = _contradicting_host(tmp_path, self._SEGMENT, self._row(accepted=False))

        code = support.path_segment_refusal(tmp_path, self._SEGMENT).errno
        assert code not in {errno.EINVAL, errno.ENAMETOOLONG}

    def test_the_stub_errno_never_collides_with_the_hosts_own(self, tmp_path, monkeypatch) -> None:
        """Not-blind for the chooser: it must move when the host's answer moves."""
        monkeypatch.setattr(
            filesystem_support,
            "path_segment_refusal",
            lambda root, segment: OSError(errno.EACCES, "Permission denied"),
        )

        support, _ = _contradicting_host(tmp_path, self._SEGMENT, self._row(accepted=False))

        assert support.path_segment_refusal(tmp_path, self._SEGMENT).errno != errno.EACCES


def test_a_probe_that_says_accepts_over_a_filesystem_that_refuses_is_an_assertion(
    paths_oracle, refusals_oracle, tmp_path
) -> None:
    """An accepting probe over a refusing filesystem fails with the probe's complaint, naming the row.

    The Windows path: the contradicting stub is the accepting one and
    ``create_swc`` meets the volume's ``OSError``. Reproduced by the same splice as
    the refusing-branch case, with the other stub.
    """
    spliced = dict(_swc_row(refusals_oracle, "group_control_character"))
    assert spliced["accepted"]

    with pytest.raises(AssertionError, match="accepts the component"):
        _replay_with_a_stubbed_host(
            paths_oracle, refusals_oracle, "group_over_long", spliced, tmp_path / "claimed", _AcceptingHost
        )


def test_an_undeclared_row_a_filesystem_refuses_says_the_declaration_is_missing(
    paths_oracle, refusals_oracle, tmp_path
) -> None:
    """An undeclared accepted row the filesystem refuses says the declaration is missing.

    No probe was consulted, so the failure must not wear the probe's complaint; the
    not-blind case's second half rests on that difference. Reproduced by the splice,
    with the declaration removed.
    """
    spliced = {
        key: value
        for key, value in _swc_row(refusals_oracle, "group_control_character").items()
        if key != "filesystem_dependent"
    }
    assert spliced["accepted"] and "filesystem_dependent" not in spliced

    with pytest.raises(AssertionError, match="carries no filesystem_dependent declaration") as excinfo:
        _replay_with_a_stubbed_host(
            paths_oracle, refusals_oracle, "group_over_long", spliced, tmp_path / "undeclared", _AcceptingHost
        )

    assert "accepts the component" not in str(excinfo.value), (
        "the two complaints must stay distinguishable -- the not-blind case tells its halves apart by them"
    )


def test_the_hosts_own_errno_is_what_an_unmeasured_platform_is_held_to() -> None:
    """:func:`_assert_filesystem_errno` holds a create to the host's errno, or to ``ENAMETOOLONG``.

    The Windows errno past ``MAX_PATH`` is unmeasured, so none is invented. Both
    directions are asserted directly because no live call on this host yields a
    probe and a create agreeing on another errno.
    """
    other = errno.EINVAL
    _assert_filesystem_errno(OSError(other, "create"), OSError(other, "probe"), "agreeing")

    with pytest.raises(AssertionError, match="refuses the segment with errno"):
        _assert_filesystem_errno(OSError(errno.EIO, "create"), OSError(other, "probe"), "disagreeing")

    # Where the host does say ENAMETOOLONG, the recorded verdict is still the
    # assertion — the probe may not soften it.
    with pytest.raises(AssertionError):
        _assert_filesystem_errno(OSError(other, "create"), OSError(errno.ENAMETOOLONG, "probe"), "recorder-like")
    with pytest.raises(AssertionError):
        _assert_filesystem_errno(OSError(other, "create"), None, "undeclared")


def test_the_refusal_table_probes_every_public_create_function(refusals_oracle) -> None:
    """Every ``create_*`` carries a verdict row, so none is unconstrained."""
    exposed = {name for name in dir(mutations) if name.startswith("create_")}
    assert {entry["function"] for entry in refusals_oracle["functions"]} == exposed


# ---------------------------------------------------------------------------
# The collision rows
# ---------------------------------------------------------------------------


def _seeded_copy(paths_oracle: dict[str, Any], root: Path, seed: dict[str, str]) -> Path:
    """A fresh copy of the recorded model with one seed file written into it."""
    shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
    target = root / seed["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(seed["content"], encoding="utf-8", newline="\n")
    return target


#: Every ``(function, case)`` of ``writer_refusals.json``'s ``collisions`` block, read
#: at collection so each case is a test of its own.
_COLLISION_CASES: tuple[tuple[str, str], ...] = tuple(
    (entry["function"], case)
    for entry in json.loads((FIXTURE_ROOT / "writer_refusals.json").read_text(encoding="utf-8"))["collisions"]
    for case in entry["cases"]
)


@pytest.mark.parametrize(("function", "case"), _COLLISION_CASES, ids=[f"{f}/{c}" for f, c in _COLLISION_CASES])
def test_every_frozen_collision_verdict_is_reproduced(paths_oracle, refusals_oracle, tmp_path, function, case) -> None:
    """Both collision cases of every ``create_*``, each against its own seeded copy.

    A path held by another identity takes the nearest free ``~N`` and leaves the
    holding file byte-identical; an identity already declared by content, in any
    file, is refused.
    """
    entry = next(e for e in refusals_oracle["collisions"] if e["function"] == function)
    expected = entry["cases"][case]
    arguments = _probe_arguments(paths_oracle, function, "", "accepted_baseline", {})
    root = tmp_path / "model"
    seed = _seeded_copy(paths_oracle, root, expected["seed"])
    if expected["accepted"]:
        result = _call(function, str(root), arguments)
        assert _relative(result, str(root)) == expected["path"]
        assert declared_identity(Path(result)) == arguments.get("name", arguments.get("req_id"))
        assert seed.read_text(encoding="utf-8") == expected["seed"]["content"]
        return
    with pytest.raises(Exception) as caught:  # noqa: B017 - the type is the assertion
        _call(function, str(root), arguments)
    assert type(caught.value).__name__ == expected["exception"]
    assert str(caught.value).startswith(expected["message"])
    # The refusal names the file that declares the identity, not the composed one.
    assert Path(str(caught.value)[len(expected["message"]) :]).name == seed.name


_COLLISION_CASES = {"path_held_by_another_identity": True, "identity_held_under_another_file_name": False}


def _collision_table_problems(collisions: list[dict], exposed: set[str]) -> list[str]:
    """Return what the collision table gets wrong: a create missing or extra, a case set, a reason or a verdict."""
    problems = []
    if {entry["function"] for entry in collisions} != exposed:
        problems.append("the table's functions are not the public creates")
    for entry in collisions:
        cases = entry["cases"]
        if set(cases) != set(_COLLISION_CASES):
            problems.append(f"{entry['function']}: cases {sorted(cases)}")
            continue
        problems.extend(
            f"{entry['function']}: {name}"
            for name, accepted in _COLLISION_CASES.items()
            if not cases[name]["reason"].strip() or cases[name]["accepted"] is not accepted
        )
    return problems


def _public_creates() -> set[str]:
    return {name for name in dir(mutations) if name.startswith("create_")}


def test_the_collision_table_covers_every_public_create_function(refusals_oracle) -> None:
    assert _collision_table_problems(refusals_oracle["collisions"], _public_creates()) == []


@pytest.mark.parametrize("fault", ["a create missing", "an empty reason", "a flipped verdict"])
def test_the_collision_table_check_catches_a_planted_fault(refusals_oracle, fault: str) -> None:
    collisions = copy.deepcopy(refusals_oracle["collisions"])
    if fault == "a create missing":
        collisions.pop()
    elif fault == "an empty reason":
        collisions[0]["cases"]["path_held_by_another_identity"]["reason"] = " "
    else:
        collisions[0]["cases"]["identity_held_under_another_file_name"]["accepted"] = True
    assert _collision_table_problems(collisions, _public_creates()) != []


@pytest.mark.parametrize("function", sorted(name for name in dir(mutations) if name.startswith("create_")))
def test_a_path_held_by_a_case_variant_takes_the_suffix_on_every_filesystem(
    paths_oracle, refusals_oracle, tmp_path, function
) -> None:
    """The same row with the seed's file name lower-cased — one answer on every host.

    On APFS and NTFS the lower-cased file IS the composed file; on a case-sensitive
    filesystem it is a different file. The writer asks neither: occupancy is decided
    over the directory's listing compared case-insensitively, so the create takes
    ``~1`` everywhere and never writes the case-variant pair a checkout on a Mac
    would collapse.
    """
    entry = next(e for e in refusals_oracle["collisions"] if e["function"] == function)
    expected = entry["cases"]["path_held_by_another_identity"]
    directory, _, filename = expected["seed"]["path"].rpartition("/")
    seed = {"path": f"{directory}/{filename.lower()}", "content": expected["seed"]["content"]}
    root = tmp_path / "model"
    written_seed = _seeded_copy(paths_oracle, root, seed)
    arguments = _probe_arguments(paths_oracle, function, "", "accepted_baseline", {})

    result = _call(function, str(root), arguments)

    assert _relative(result, str(root)) == expected["path"]
    assert written_seed.read_text(encoding="utf-8") == expected["seed"]["content"]


def test_the_fail_closed_twin_refuses_a_plain_file_that_already_declares_the_identity(tmp_path) -> None:
    """``_target_path`` refuses a plain file that already declares the identity.

    Every ``create_*`` refuses a held identity by content first, so no public call
    reaches this branch; a caller that asks wrongly must meet a ``ValueError``,
    not an overwrite.
    """
    root = tmp_path / "model"
    (root / "data_types" / "implementation").mkdir(parents=True)
    held = root / "data_types" / "implementation" / "Speed_T.yaml"
    held.write_text("name: Speed_T\nkind: value_type\nbase: float32\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="An element 'Speed_T' of layer 'implementation_types' already exists"):
        mutations._target_path(str(root), "implementation_types", "Speed_T")

    assert sorted(path.name for path in (root / "data_types" / "implementation").iterdir()) == ["Speed_T.yaml"]


#: Every public ``create_*``, spelled out so a parametrization over it is a claim
#: about all of them; :func:`test_the_content_check_list_is_every_create_function`
#: holds it to the module's surface.
CREATE_FUNCTIONS = (
    "create_swc",
    "create_interface",
    "create_cs_interface",
    "create_requirement",
    "create_composition",
    "create_connector",
    "create_task",
    "create_sequence",
    "create_data_type",
    "create_mapping_set",
    "create_hardware_element",
)


def test_the_content_check_list_is_every_create_function() -> None:
    """The parametrization is the whole surface, not a sample of it."""
    assert set(CREATE_FUNCTIONS) == {name for name in dir(mutations) if name.startswith("create_")}


@pytest.mark.parametrize("function", CREATE_FUNCTIONS)
def test_an_unparseable_file_of_the_layer_is_refused_alike_by_every_content_check(
    paths_oracle, refusals_oracle, tmp_path, function
) -> None:
    """An unparseable file of the layer makes every content check raise ``ValueError`` and write nothing.

    ``reader.find_element_file`` translates no parse failure, so without the
    wrapping a raw ``yaml.YAMLError`` escapes, a type no caller of these public
    writers catches. The message names the layer and the file.
    """
    entry = next(e for e in refusals_oracle["collisions"] if e["function"] == function)
    seeded = entry["cases"]["path_held_by_another_identity"]["seed"]["path"]
    directory, layer = seeded.rpartition("/")[0], seeded.split("/")[0]
    root = tmp_path / "model"
    _seeded_copy(paths_oracle, root, {"path": f"{directory}/Broken.yaml", "content": "name: [unclosed\n"})
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    arguments = _probe_arguments(paths_oracle, function, "", "accepted_baseline", {})

    with pytest.raises(ValueError) as failure:
        _call(function, str(root), arguments)

    assert not isinstance(failure.value, yaml.YAMLError), "the raw parse failure escaped again"
    assert layer in str(failure.value)
    assert "Broken.yaml" in str(failure.value)
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) == before


def test_an_unparseable_layer_whose_file_can_no_longer_be_named_still_reads_as_one_sentence(tmp_path: Path) -> None:
    """When the failing file can no longer be named, the message is still one sentence.

    ``_unparseable_layer_file`` re-scans the layer because ``yaml``'s error names
    ``<unicode string>``; if the tree changed meanwhile it finds none, and the
    message is composed without a file-name slot.
    """
    root = tmp_path / "model"
    (root / "tasks").mkdir(parents=True)

    with pytest.raises(ValueError) as failure, mutations._readable_layer(str(root), "tasks"):
        raise yaml.YAMLError("boom")

    assert str(failure.value) == (
        "cannot check the 'tasks' layer: a file of it is not valid YAML, and it could no longer be identified (boom)"
    )


# ---------------------------------------------------------------------------
# The storage declaration is what decides the path
# ---------------------------------------------------------------------------


def _perturbed_registry(**storage_changes: dict[str, str]) -> ModelRegistry:
    """A registry over a deep copy of the shipped contract with storage moved.

    Deep-copied so a mutation cannot reach the document a later
    :func:`~swcraft_tools.contract.load_contract` returns.
    """
    contract = copy.deepcopy(load_contract())
    for layer in contract["layers"]:
        if layer["name"] in storage_changes:
            layer["storage"].update(storage_changes[layer["name"]])
    return build_model_registry(contract=contract, binding=load_binding())


def test_a_flat_layers_storage_perturbation_moves_the_file(model_root) -> None:
    """A renamed directory and a changed suffix move both the written file and the returned path.

    The return value is checked because the MCP tool reports it and the audit log
    records it.
    """
    with use_registry(_perturbed_registry(requirements={"directory": "deployments", "fileSuffix": ".yml"})):
        result = mutations.create_requirement(model_root, "REQ_MOVED", "Moved", "Moved by the perturbation.")

    assert _relative(result, model_root) == "deployments/REQ_MOVED.yml"
    assert (Path(model_root) / "deployments" / "REQ_MOVED.yml").is_file()
    assert not (Path(model_root) / "requirements" / "REQ_MOVED.yaml").exists()


def test_the_nesting_layers_perturbation_keeps_the_placement_component(model_root) -> None:
    """Moving ``swc``'s directory keeps the group level beneath it rather than flattening it."""
    with use_registry(_perturbed_registry(swc={"directory": "components"})):
        result = mutations.create_swc(model_root, name="MovedSWC", group="moved")

    assert _relative(result, model_root) == "components/moved/MovedSWC.yaml"
    assert (Path(model_root) / "components" / "moved" / "MovedSWC.yaml").is_file()


def test_outside_a_use_registry_block_the_shipped_storage_is_what_applies(model_root) -> None:
    """The perturbation is confined to its block — the ``ContextVar`` is restored."""
    with use_registry(_perturbed_registry(requirements={"directory": "deployments"})):
        pass

    result = mutations.create_requirement(model_root, "REQ_SHIPPED", "Shipped", "Written under the shipped storage.")
    assert _relative(result, model_root) == "requirements/REQ_SHIPPED.yaml"


# ---------------------------------------------------------------------------
# No literal survives
# ---------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(Path(inspect.getfile(mutations)).read_text(encoding="utf-8"))


def test_no_path_join_in_mutations_has_a_literal_component() -> None:
    """No ``/`` join, ``Path(...)`` or ``join``/``joinpath`` call in ``mutations.py`` has a string operand.

    A target path is composed from the registry or not here, whatever layer a later
    edit adds. ``Path(model_root, "swc", ...)`` composes without a ``/``, hence the
    call scan beside the operator scan.

    Does not cover the ``.yaml`` and layer-name strings in message text or in
    :class:`~swcraft_tools.writer.mutations.ReservedNameError`'s layer argument:
    those are wording, pinned by this module's refusal tests and
    ``test_reserved_name_policies.py``.
    """
    tree = _module_tree()
    offenders = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and any(isinstance(side, ast.Constant | ast.JoinedStr) for side in (node.left, node.right))
    ]
    composers = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "Path")
            or (isinstance(node.func, ast.Attribute) and node.func.attr in {"join", "joinpath"})
        )
        and any(isinstance(argument, ast.Constant | ast.JoinedStr) for argument in node.args)
    ]
    assert offenders == []
    assert composers == []


def test_every_target_path_call_names_a_layer_the_contract_declares() -> None:
    """Every ``_target_path`` call's layer argument is a layer the contract declares.

    The argument is a layer name handed to ``ModelRegistry.compose_path``, where a
    misspelling is a ``KeyError`` at write time, invisible to the structural scan.
    A "no layer directory literal" scan would be wrong: a layer such as ``swc`` or
    ``tasks`` is named as its own directory, and ``connector_to_dict``/``task_to_dict`` carry
    ``"swc"`` as a payload key. This classifies positions, not spellings.
    """
    declared = {layer["name"] for layer in load_contract()["layers"]}
    named: list[str] = []
    variable: list[str] = []
    for function in ast.walk(_module_tree()):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_target_path"
                and len(node.args) >= 2
            ):
                continue
            argument = node.args[1]
            if isinstance(argument, ast.Constant):
                named.append(argument.value)
            elif isinstance(argument, ast.Name):
                # `create_data_type` picks its layer by kind from a conditional
                # expression. Every result branch is collected, never filtered by
                # the expected names (which would let a misspelling through), and
                # each must be a declared layer.
                variable.append(function.name)
                named.extend(
                    layer
                    for assignment in ast.walk(function)
                    if isinstance(assignment, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == argument.id for t in assignment.targets)
                    for layer in _result_branches(assignment.value)
                )

    assert variable == ["create_data_type"], variable
    assert sorted(set(named) & {"application_types", "base_types", "implementation_types"}) == [
        "application_types",
        "base_types",
        "implementation_types",
    ]
    assert len(named) == 13, f"expected the eleven composed targets (one naming three layers), found {len(named)}"
    assert set(named) <= declared, sorted(set(named) - declared)


def _result_branches(node: ast.expr) -> list[object]:
    """Every value a conditional expression can evaluate to, by its branches; a non-constant result is kept as text.

    Conditions are skipped (they hold kind names); a non-literal branch is kept so
    the ``<= declared`` assertion refuses it rather than dropping it unseen.
    """
    if isinstance(node, ast.IfExp):
        return _result_branches(node.body) + _result_branches(node.orelse)
    if isinstance(node, ast.Constant):
        return [node.value]
    return [ast.unparse(node)]


def test_the_eleven_target_compositions_all_route_through_the_one_helper() -> None:
    """Each of the eleven ``create_*`` composes its target exactly once, through ``_target_path``.

    Paired with the literal-join scan because a module that composes nothing also
    passes that. The count moves with the number of ``create_*`` functions.
    """
    tree = _module_tree()
    per_function: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("create_"):
            continue
        per_function[node.name] = sum(
            1
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "_target_path"
        )

    assert sum(per_function.values()) == 11
    assert len(per_function) == 11
    assert set(per_function.values()) == {1}


# ---------------------------------------------------------------------------
# The exclusive create
# ---------------------------------------------------------------------------


#: The bytes the racing writer puts at the chosen name. Not YAML of the layer on
#: purpose: what the assertion needs is a file the create must not have written,
#: and a well-formed element there would be indistinguishable from one it did.
FOREIGN_BYTES = "# another writer got here first\n"


@pytest.mark.parametrize("function", CREATE_FUNCTIONS)
def test_every_create_refuses_a_file_that_appeared_after_the_store_chose_the_name(
    paths_oracle, tmp_path, monkeypatch, function
) -> None:
    """Every create refuses a file that appeared at the chosen name after the listing, and leaves it unchanged.

    The file is chosen from a directory listing (``model_state.free_storage_path``)
    and written a moment later; the wrapper writes at the chosen name in between,
    staging the interleaving itself rather than a race. The editing protocol writes
    through ``writer/elements.write_element(create=True)`` and is not exposed; this
    is the Python API's window.
    """
    root = tmp_path / "model"
    shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
    arguments = _probe_arguments(paths_oracle, function, "", "accepted_baseline", {})
    chosen: list[Path] = []
    real_target_path = mutations._target_path

    def _a_file_appears_at_the_chosen_name(*args: Any, **kwargs: Any) -> Path:
        target = real_target_path(*args, **kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FOREIGN_BYTES, encoding="utf-8", newline="\n")
        chosen.append(target)
        return target

    monkeypatch.setattr(mutations, "_target_path", _a_file_appears_at_the_chosen_name)

    with pytest.raises(ValueError) as caught:
        _call(function, str(root), arguments)

    assert len(chosen) == 1, "the create asked the store for exactly one file"
    assert chosen[0].read_text(encoding="utf-8") == FOREIGN_BYTES, "the other writer's bytes were overwritten"
    message = str(caught.value)
    # The sentence states what is OBSERVED -- the file is there and the listing
    # did not name it -- rather than inferring that it *appeared* in between.
    # The race this probe stages is one cause; a filesystem that folds Unicode
    # normalization, where the file was there all along, is the other, and one
    # sentence has to be true of both (`_write_new_file`'s own `Raises:`).
    assert "already exists and the store's listing did not name it" in message
    # The refusal names the layer, the element and the file name -- never where
    # the project is stored (the path-free rule).
    assert arguments.get("name", arguments.get("req_id")) in message
    assert str(root) not in message
    assert str(tmp_path) not in message


def test_the_exclusive_create_leaves_no_audit_entry(paths_oracle, tmp_path, monkeypatch) -> None:
    """A refused create writes no file, so it logs no write either.

    ``audit`` runs after the write in every ``create_*``; asserted because the
    recoverability invariant reads the audit log as the record of what happened.
    """
    root = tmp_path / "model"
    shutil.copytree(FIXTURE_ROOT / paths_oracle["model"], root)
    arguments = _probe_arguments(paths_oracle, "create_swc", "", "accepted_baseline", {})
    real_target_path = mutations._target_path

    def _a_file_appears_at_the_chosen_name(*args: Any, **kwargs: Any) -> Path:
        target = real_target_path(*args, **kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FOREIGN_BYTES, encoding="utf-8", newline="\n")
        return target

    monkeypatch.setattr(mutations, "_target_path", _a_file_appears_at_the_chosen_name)
    audit_path = tmp_path / "audit.jsonl"
    auditor = AuditLog(str(audit_path))

    with pytest.raises(ValueError):
        mutations.create_swc(str(root), auditor=auditor, **arguments)

    assert not audit_path.exists() or audit_path.read_text(encoding="utf-8") == ""


def test_the_add_and_update_path_still_overwrites_the_file_it_rewrites(model_root) -> None:
    """``add_*`` and the updates still overwrite the existing file they rewrite.

    The behavioural half, over two sites on two layers;
    :func:`test_only_the_creates_write_exclusively_and_they_all_do` is the
    structural claim about all of them.
    """
    swc_file = Path(mutations.create_swc(model_root, "ProbeSWC", "probe"))
    before = swc_file.read_text(encoding="utf-8")

    after = Path(mutations.add_port(model_root, "ProbeSWC", "pProbe", "provide", "ISpeed"))

    assert after == swc_file
    assert swc_file.read_text(encoding="utf-8") != before
    assert "pProbe" in swc_file.read_text(encoding="utf-8")

    requirement = Path(mutations.add_trace_link(model_root, "REQ_SPEED_001", "ProbeSWC"))
    assert "ProbeSWC" in requirement.read_text(encoding="utf-8")


def test_only_the_creates_write_exclusively_and_they_all_do() -> None:
    """Each ``create_*`` writes through ``_write_new_file`` once and never by ``write_text``; nothing else calls it.

    A ``create_*`` with a ``write_text`` would reopen the window, and an ``add_*``
    on the helper could no longer rewrite its file.
    """
    tree = _module_tree()
    exclusive: dict[str, int] = {}
    overwriting: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = [inner for inner in ast.walk(node) if isinstance(inner, ast.Call)]
        exclusive[node.name] = sum(
            1 for call in calls if isinstance(call.func, ast.Name) and call.func.id == "_write_new_file"
        )
        overwriting[node.name] = sum(
            1 for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "write_text"
        )

    creates = {name: count for name, count in exclusive.items() if name.startswith("create_")}
    assert set(creates) == set(CREATE_FUNCTIONS)
    assert set(creates.values()) == {1}, creates
    assert [name for name, count in exclusive.items() if count and not name.startswith("create_")] == []
    assert [name for name in CREATE_FUNCTIONS if overwriting[name]] == []


def test_the_exclusive_open_is_the_one_site_that_states_the_two_keywords() -> None:
    """The creates' one text ``open`` is ``exclusive_create.open_new``'s, naming ``encoding`` and ``newline``.

    ``test_platform_io.py`` scans ``open`` calls by spelling, so routing the creates
    through the helper moves their sites into this one. ``mutations.py`` makes no
    ``open`` of its own; ``open_new`` opens the descriptor
    :func:`~swcraft_tools.exclusive_create.create_new` returned in mode ``"w"``
    (bound to a name first, so a failing wrap closes it). The descriptor is binary
    (``test_exclusive_create.py``), so ``newline`` alone decides the line ends.
    """
    tree = _module_tree()
    assert [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == "open"] == []
    writer = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_write_new_file"
    )
    helper_calls = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func).startswith("exclusive_create.")
    ]
    assert helper_calls == ["exclusive_create.open_new"]
    assert [
        node
        for node in ast.walk(writer)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "exclusive_create.open_new"
    ], "the creates' one write no longer goes through the helper"

    helper = ast.parse(Path(inspect.getfile(exclusive_create)).read_text(encoding="utf-8"))
    open_new = next(node for node in ast.walk(helper) if isinstance(node, ast.FunctionDef) and node.name == "open_new")
    text_opens = [
        node
        for node in ast.walk(open_new)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
        and [argument.value for argument in node.args[1:] if isinstance(argument, ast.Constant)] == ["w"]
    ]
    assert len(text_opens) == 1
    call = text_opens[0]
    descriptor = call.args[0]
    assert isinstance(descriptor, ast.Name)
    # The descriptor is the one bound from `create_new`, closed by the guard when the wrap fails.
    sources = [
        node.value
        for node in ast.walk(open_new)
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == [descriptor.id]
    ]
    assert len(sources) == 1 and isinstance(sources[0], ast.Call)
    assert ast.unparse(sources[0].func) == "create_new"
    declared = {keyword.arg: keyword.value for keyword in call.keywords}
    assert isinstance(declared["encoding"], ast.Constant) and declared["encoding"].value == "utf-8"
    assert isinstance(declared["newline"], ast.Constant) and declared["newline"].value == "\n"


# ---------------------------------------------------------------------------
# The kernel policy is not on the writers' path
# ---------------------------------------------------------------------------


def test_the_kernel_policy_refuses_the_three_groups_create_swc_still_takes(model_root, tmp_path) -> None:
    """``path_refusal`` refuses the three group shapes the real ``create_swc`` still takes.

    The kernel holds the segment policy as
    :func:`~swcraft_tools.model_registry.path_refusal`, while
    :func:`compose_storage_path` validates nothing, so the public ``create_*``
    reach an unguarded composer; this fails if the policy is folded into it. Only
    calling the public function proves the guard is absent from the whole write
    path:

    - a bare ``.`` group is accepted (pathlib folds it into ``swc/``);
    - a control-character group is accepted and becomes a real directory;
    - an over-long group fails in the filesystem, never as a validation refusal.

    The last two are filesystem properties (Windows refuses U+0001 with
    ``WinError 123``; its errno past ``MAX_PATH`` is unmeasured). On every
    platform the answer must not be a ``ValueError``; only the follow-on is
    decided per host, by asking it through :mod:`filesystem_support`, never by
    ``sys.platform`` or ``skipif``.

    **Does not reach** the identity half over all eleven
    (``test_every_frozen_verdict_is_unchanged``), nor a probe for ``.``, which
    pathlib folds away before any filesystem sees it.
    """
    record = production_registry().storage("swc")
    # One identity per case: the duplicate check is recursive, so a reused name
    # would fail on the first case's file. The fourth field asks this filesystem
    # first; the third is the answer where the frozen table was recorded.
    cases = {
        "bare dot": ("ProbeDotSWC", ".", "swc/ProbeDotSWC.yaml", False),
        "control character": ("ProbeCtrlSWC", "A\x01B", "swc/A\x01B/ProbeCtrlSWC.yaml", True),
        "over long": ("ProbeLongSWC", "L" * 300, None, True),
    }

    for label, (name, group, recorded_relative, filesystem_dependent) in cases.items():
        assert path_refusal(record, name, {"group": group}, layer="swc") is not None, label

        host_refusal = filesystem_support.path_segment_refusal(tmp_path, group) if filesystem_dependent else None
        expected_relative = recorded_relative if host_refusal is None else None

        try:
            result = mutations.create_swc(model_root, name, group=group, swc_type="application")
        except OSError as failure:
            assert expected_relative is None, f"{label} failed in the filesystem: {failure}"
            _assert_filesystem_errno(failure, host_refusal, label)
        except ValueError as refusal:  # pragma: no cover - the regression this test exists for
            raise AssertionError(
                f"create_swc refused a {label} group with {refusal!r}; the kernel's segment policy has "
                "reached the writers' composer and tightened public input validation"
            ) from refusal
        else:
            assert expected_relative is not None, (
                f"{label}: this filesystem refuses the component {group!r} ({host_refusal}), "
                "but create_swc returned a path"
            )
            assert _relative(result, model_root) == expected_relative, label
            assert Path(result).is_file(), label


def test_a_bare_dot_group_is_stored_where_the_store_says_and_records_no_target(tmp_path: Path) -> None:
    """For a bare ``.`` group the store's free path and the file ``create_swc`` writes are one string.

    The audit's inverse is not reached: ``create_swc`` records its arguments and the
    written path, with no ``args.target`` for ``audit.element_address_of_target`` to
    re-compose. The generic writers' targets come from ``staging._locate``, which
    refuses ``.`` and ``""`` before storage is asked.
    """
    model_root = tmp_path / "model"
    (model_root / "swc").mkdir(parents=True)
    audit_path = tmp_path / "audit.jsonl"

    free = free_storage_path(model_root, "swc", "ProbeDotSWC", {"group": "."}, registry=production_registry())
    written = mutations.create_swc(
        str(model_root),
        "ProbeDotSWC",
        group=".",
        swc_type="application",
        auditor=AuditLog(str(audit_path), project_root=str(tmp_path)),
    )

    assert free.path == "swc/ProbeDotSWC.yaml"
    assert _relative(written, model_root) == free.path
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["operation"] for entry in entries] == ["create_swc"]
    assert set(entries[0]["args"]) == {"name", "group", "swc_type"}, "no target for the inverse to re-compose"
