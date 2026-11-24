"""``swcraft passport``, run as a subprocess on a specimen's copy, as its user runs it.

The passport on stdout and in ``--output`` is exactly ``compute_passport``'s, serialized as the command promises
(indent 2, a trailing newline). The command names no element and no project location: a refusal names the
field or layer and a closed reason, any other exception its class alone, and a sentinel carried by names, texts,
paths, pins and ``.env`` appears nowhere, nor its lowercase prefix ``zqsen``. Withholding a pinned value is
passport's alone: ``check-model`` keeps the shared pin sentence.

Exit 0 with the passport, also on a model ``check-model`` refuses and on a manifest still holding the retired
``outputs.rst``; exit 1 on an unreadable element file, a missing project or ``--project``, a manifest that does
not parse or names an absolute root, an output it cannot write, an unreadable ``.env`` and a pin of another
release (which writes nothing); exit 2 on a usage error. Only ``passport`` defers the ``.env`` load past parsing.

What this does not reach: the passport's values themselves (``test_passport.py``); a bare OS error of the
manifest read (a permission refused, say), which no test here provokes on the manifest itself; and a directory
given as ``--project``, refused as for every command (the manifest is named by its file).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import copy_specimen, load_repo_script
from swcraft_tools import __version__
from swcraft_tools.cli import main as cli_main
from swcraft_tools.cli import project_args, read_failure_sentence, toolchain_pin_problem
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.guide_reference import cli as guide_reference_cli
from swcraft_tools.passport import compute_passport
from swcraft_tools.project import require_project
from swcraft_tools.toolchain_pin import withheld_pin_sentence
from test_passport import NAME_SENTINEL, TEXT_SENTINEL, sentinel_root  # noqa: F401 - the fixture

BENCH_MODULE = load_repo_script("agent_task_bench", "scripts/agent_task_bench.py")

#: The specimen the passport runs on: every shape the passport counts, at a small size.
SPECIMEN = "passport-small"

#: The lowercase prefix both sentinels share: a lowered, truncated or misspelt derivative of either
#: still carries it.
SENTINEL_PREFIX = "zqsen"


def _run(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "passport", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def _manifest(root: Path) -> str:
    return (root / "swcraft.yaml").as_posix()


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of ``passport-small`` under the large bench's manifest shape."""
    root = copy_specimen(SPECIMEN, tmp_path_factory.mktemp("passport-project") / SPECIMEN)
    (root / "swcraft.yaml").write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    return root


def _expected(project: Path) -> str:
    return json.dumps(compute_passport(project / "model").passport, indent=2) + "\n"


def test_the_passport_goes_to_stdout_and_equals_the_pass(project: Path, tmp_path: Path) -> None:
    run = _run("--project", _manifest(project), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout == _expected(project)
    assert run.stderr == ""


def test_output_writes_the_passport_to_the_file_and_nothing_to_stdout(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "passport.json"
    run = _run("--project", _manifest(project), "--output", out.as_posix(), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    assert out.read_bytes() == _expected(project).encode("utf-8")


def test_the_output_names_no_element_of_the_model(project: Path, tmp_path: Path) -> None:
    """The locator stays local: no element name of the model reaches the output."""
    run = _run("--project", _manifest(project), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    names = sorted({path.stem for path in (project / "model").rglob("*.yaml")})
    assert names
    assert [name for name in names if name in run.stdout] == []


def test_a_sentinel_model_puts_neither_sentinel_nor_its_prefix_into_the_output(
    sentinel_root: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> None:
    (sentinel_root.parent / "swcraft.yaml").write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    out = tmp_path / "passport.json"
    run = _run("--project", _manifest(sentinel_root.parent), "--output", out.as_posix(), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    for text in (run.stdout, run.stderr, out.read_text(encoding="utf-8")):
        assert NAME_SENTINEL not in text
        assert TEXT_SENTINEL not in text
        assert SENTINEL_PREFIX not in text.lower()


def test_an_unreadable_element_file_exits_1_naming_the_layer_and_nothing_of_the_element(tmp_path: Path) -> None:
    broken = copy_specimen(SPECIMEN, tmp_path / "broken")
    (broken / "swcraft.yaml").write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    victim = sorted((broken / "model" / "swc").rglob("*.yaml"), key=lambda path: path.as_posix())[0]
    victim.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    storage_name = victim.relative_to(broken / "model").as_posix()

    run = _run("--project", _manifest(broken), cwd=tmp_path)

    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr.startswith("Error: the model could not be read: a SWC file is not valid (")
    assert run.stderr.endswith("); its name and its content are withheld\n")
    # The storage name is the element's name, and libyaml's sentence quotes the line it refused.
    assert storage_name not in run.stderr and victim.stem not in run.stderr
    assert "unclosed" not in run.stderr
    assert str(tmp_path) not in run.stderr and tmp_path.as_posix() not in run.stderr


def test_a_sentinel_element_that_does_not_validate_names_neither_the_element_nor_its_values(
    sentinel_root: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> None:
    """Pydantic's sentence repeats the input; the command prints the layer and the error types alone."""
    project = tmp_path / "copy"
    shutil.copytree(sentinel_root.parent, project)
    (project / "swcraft.yaml").write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    victim = sorted((project / "model" / "swc").rglob("*.yaml"), key=lambda path: path.as_posix())[0]
    assert NAME_SENTINEL in victim.name, "the file the refusal is about carries the sentinel in its name"
    victim.write_text(
        f"name: {victim.stem}\ndescription: {TEXT_SENTINEL}\nports: {NAME_SENTINEL}\n", encoding="utf-8", newline="\n"
    )

    run = _run("--project", _manifest(project), cwd=tmp_path)

    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr.startswith("Error: the model could not be read: a SWC file is not valid (")
    assert NAME_SENTINEL not in run.stderr
    assert TEXT_SENTINEL not in run.stderr
    assert SENTINEL_PREFIX not in run.stderr.lower()


def test_an_unexpected_exception_is_named_by_its_class_alone(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any exception of the pass the command does not know is its class, never its text."""

    reached: list[object] = []

    def raises(_model_root: object) -> None:
        reached.append(_model_root)
        raise KeyError(NAME_SENTINEL)

    monkeypatch.setattr("swcraft_tools.passport.compute_passport", raises)
    monkeypatch.setattr(sys, "argv", ["swcraft", "passport", "--project", _manifest(project)])
    with pytest.raises(SystemExit) as exited:
        cli_main()
    assert exited.value.code == 1
    assert len(reached) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Error: the model could not be read: KeyError, its text withheld: it may name an element of the model\n"
    )


def test_a_model_check_model_refuses_still_gets_its_passport(project: Path, tmp_path: Path) -> None:
    """A broken model's shape is what a bug report needs: an interface the ports name is removed."""
    broken = tmp_path / "broken"
    shutil.copytree(project, broken)
    named = compute_passport(broken / "model").locator.heaviest_interface()
    (interface,) = [path for path in (broken / "model" / "interfaces").rglob("*.yaml") if path.stem == named]
    interface.unlink()
    checked = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", _manifest(broken)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert checked.returncode == 1, "check-model must refuse the model this case is about"

    run = _run("--project", _manifest(broken), cwd=tmp_path)

    assert run.returncode == 0, run.stderr
    assert run.stdout == _expected(broken)
    assert run.stderr == ""


def _with_the_retired_rst_key(root: Path, pin: str | None) -> None:
    """*root* as ``passport-small`` under a manifest still holding ``outputs.rst`` beside ``outputs.needs``."""
    manifest = BENCH_MODULE.MANIFEST.replace(
        "  needs: dist/needs.json\n", "  needs: dist/needs.json\n  rst: dist/needs\n", 1
    )
    assert "rst: dist/needs" in manifest
    copy_specimen(SPECIMEN, root)
    text = manifest if pin is None else manifest + f'toolchain_version: "{pin}"\n'
    (root / "swcraft.yaml").write_text(text, encoding="utf-8", newline="\n")


def test_a_manifest_holding_the_retired_rst_key_is_read(tmp_path: Path) -> None:
    """``outputs.rst`` is no field any more: the loader drops it, and only ``check-model`` names it."""
    plain = tmp_path / "plain"
    _with_the_retired_rst_key(plain, None)

    run = _run("--project", _manifest(plain), cwd=tmp_path)

    assert run.returncode == 0, run.stderr
    assert run.stdout == _expected(plain)
    assert run.stderr == ""


def test_a_manifest_holding_the_retired_rst_key_is_refused_by_a_pin_of_another_release(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned"
    _with_the_retired_rst_key(pinned, f"9.9.9+{VALUE_SENTINEL}")

    run = _run("--project", _manifest(pinned), cwd=tmp_path)

    assert run.returncode == 1 and run.stdout == ""
    assert run.stderr == f"Error: {withheld_pin_sentence()}\n"
    _assert_pin_withheld(run)


def test_a_missing_project_exits_1_with_no_absolute_path(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "swcraft.yaml"
    run = _run("--project", missing.as_posix(), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr.startswith("Error: the project could not be read: ")
    assert str(tmp_path) not in run.stderr
    assert tmp_path.as_posix() not in run.stderr


def test_no_project_exits_1_naming_the_flag(tmp_path: Path) -> None:
    run = _run(cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert "--project is required" in run.stderr


def test_an_output_it_cannot_write_exits_1_with_no_absolute_path(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "no-such-directory" / "passport.json"
    run = _run("--project", _manifest(project), "--output", out.as_posix(), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert "could not be written to --output" in run.stderr
    assert "Traceback" not in run.stderr
    assert str(tmp_path) not in run.stderr
    assert tmp_path.as_posix() not in run.stderr


def test_a_manifest_that_does_not_parse_exits_1_with_no_absolute_path(tmp_path: Path) -> None:
    (tmp_path / "swcraft.yaml").write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    run = _run("--project", _manifest(tmp_path), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert "swcraft.yaml does not parse" in run.stderr
    assert "Traceback" not in run.stderr
    assert str(tmp_path) not in run.stderr
    assert tmp_path.as_posix() not in run.stderr


@pytest.mark.parametrize("field", ["dist_root", "views_root"])
def test_a_root_written_absolute_exits_1_with_no_absolute_path(field: str, tmp_path: Path) -> None:
    # The manifest's refusal repeats the value as written; the command withholds it.
    written = (tmp_path / "elsewhere" / field).as_posix()
    manifest = BENCH_MODULE.MANIFEST.replace(f"{field}: ", f"{field}: {written}  # ", 1)
    assert written in manifest
    (tmp_path / "swcraft.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    (tmp_path / "model").mkdir()
    run = _run("--project", _manifest(tmp_path), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert f"Manifest field '{field}' is written as an absolute path, its value withheld" in run.stderr
    assert "Traceback" not in run.stderr
    assert str(tmp_path) not in run.stderr
    assert tmp_path.as_posix() not in run.stderr


def _apostrophe_dir(tmp_path: Path) -> Path:
    """A project directory whose absolute path holds an apostrophe, so no quote style can bound it."""
    root = tmp_path / "O'Neil project"
    root.mkdir()
    (root / "model").mkdir()
    return root


def _assert_withheld(run: subprocess.CompletedProcess[str], root: Path) -> None:
    assert run.returncode == 1
    assert run.stdout == ""
    assert "Traceback" not in run.stderr
    for leaked in ("Neil", "O'Neil", str(root), root.as_posix()):
        assert leaked not in run.stderr, run.stderr


def test_a_root_written_absolute_with_an_apostrophe_leaks_no_part_of_it(tmp_path: Path) -> None:
    # The refusal single-quotes the value; the apostrophe inside it ends no quoted span early.
    root = _apostrophe_dir(tmp_path)
    written = (root / "dist").as_posix()
    manifest = BENCH_MODULE.MANIFEST.replace("dist_root: dist", f'dist_root: "{written}"', 1)
    (root / "swcraft.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    run = _run("--project", _manifest(root), cwd=tmp_path)
    _assert_withheld(run, root)
    assert "Manifest field 'dist_root' is written as an absolute path, its value withheld" in run.stderr


def test_a_schema_refusal_of_a_path_with_an_apostrophe_names_location_and_type_only(tmp_path: Path) -> None:
    # Pydantic's own sentence repeats the input in double quotes when it holds an apostrophe.
    root = _apostrophe_dir(tmp_path)
    written = (root / "model").as_posix()
    manifest = BENCH_MODULE.MANIFEST.replace("model_root: model", f'model_root: ["{written}"]', 1)
    (root / "swcraft.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    run = _run("--project", _manifest(root), cwd=tmp_path)
    _assert_withheld(run, root)
    assert "SwcraftProject is not valid: model_root: string_type" in run.stderr


#: A sentinel no refusal may echo, in any case.
VALUE_SENTINEL = "ZqSecretModel"


#: Pins of another release whose local label carries the sentinel, in three cases: the pinned value is
#: text the user chose, and passport's refusal withholds it.
SENTINEL_PINS = (
    f"9.9.9+{VALUE_SENTINEL}",
    f"9.9.9+{VALUE_SENTINEL.lower()}",
    f"9.9.9+{VALUE_SENTINEL.upper()}",
)


def _assert_pin_withheld(run: subprocess.CompletedProcess[str]) -> None:
    """The refusal names the field and the running release, and nothing of the pinned value."""
    assert "toolchain_version" in run.stderr and __version__ in run.stderr
    for text in (run.stdout, run.stderr):
        assert VALUE_SENTINEL.lower() not in text.lower(), text
        assert "9.9.9" not in text, text


def _pinned_project(root: Path, pin: str) -> Path:
    root.mkdir()
    (root / "swcraft.yaml").write_text(
        BENCH_MODULE.MANIFEST + f"toolchain_version: {json.dumps(pin)}\n", encoding="utf-8", newline="\n"
    )
    # The pin is checked before the model is read, so an empty model root is enough.
    (root / "model").mkdir()
    return root


@pytest.mark.parametrize("pin", SENTINEL_PINS, ids=["mixed", "lower", "upper"])
def test_a_pin_of_another_release_exits_1_withholds_the_pin_and_writes_nothing(pin: str, tmp_path: Path) -> None:
    pinned = _pinned_project(tmp_path / "pinned", pin)
    out = tmp_path / "passport.json"
    run = _run("--project", _manifest(pinned), "--output", out.as_posix(), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr == f"Error: {withheld_pin_sentence()}\n"
    _assert_pin_withheld(run)
    assert not out.exists()


def test_another_command_keeps_the_shared_pin_sentence(tmp_path: Path) -> None:
    """Withholding is passport's alone: ``check-model`` refuses the same pin with the shared sentence."""
    pin = SENTINEL_PINS[0]
    pinned = _pinned_project(tmp_path / "pinned", pin)
    run = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", _manifest(pinned)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert run.returncode == 1
    assert run.stderr == f"Error: {toolchain_pin_problem(require_project(_manifest(pinned)))}\n"
    assert f"pins toolchain_version {pin} and this is SWCraft {__version__}" in run.stderr


@pytest.mark.parametrize(
    ("field", "written", "reason"),
    [
        ("dist_root", f"/ {VALUE_SENTINEL}", "Manifest field 'dist_root' is written as an absolute path"),
        ("dist_root", f"/\t{VALUE_SENTINEL}", "Manifest field 'dist_root' is written as an absolute path"),
        ("views_root", f"C:\\ {VALUE_SENTINEL}", "Manifest field 'views_root' is written as an absolute path"),
        ("dist_root", f"\\\\server\\{VALUE_SENTINEL}", "Manifest field 'dist_root' is written as an absolute path"),
        ("dist_root", f"../{VALUE_SENTINEL}", "Manifest field 'dist_root' resolves outside the project root."),
        ("views_root", f"model/{VALUE_SENTINEL}", "Manifest field 'views_root' overlaps 'model_root'."),
        ("model_root", f"[{VALUE_SENTINEL}]", "SwcraftProject is not valid: model_root: string_type"),
        ("toolchain_version", f">={VALUE_SENTINEL}", "SwcraftProject is not valid: toolchain_version: value_error"),
    ],
    ids=["slash-space", "slash-tab", "drive-space", "unc", "relative-outside", "over-the-model", "schema", "pin"],
)
def test_a_manifest_refusal_echoes_no_value_it_quotes(field: str, written: str, reason: str, tmp_path: Path) -> None:
    """A refusal names the field and a closed reason; the value written -- the sentinel -- appears nowhere."""
    root = tmp_path / "project"
    (root / "model").mkdir(parents=True)
    value = written if written.startswith("[") else json.dumps(written)
    if field == "toolchain_version":
        manifest = BENCH_MODULE.MANIFEST + f"toolchain_version: {value}\n"
    else:
        manifest, replaced = re.subn(
            rf"^(\s*){field}: .*$",
            lambda match: f"{match.group(1)}{field}: {value}",
            BENCH_MODULE.MANIFEST,
            count=1,
            flags=re.MULTILINE,
        )
        assert replaced == 1
    (root / "swcraft.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    assert VALUE_SENTINEL in (root / "swcraft.yaml").read_text(encoding="utf-8")

    run = _run("--project", _manifest(root), cwd=tmp_path)

    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr.startswith("Error: the project could not be read: "), run.stderr
    assert reason in run.stderr, run.stderr
    for text in (run.stdout, run.stderr):
        assert VALUE_SENTINEL.lower() not in text.lower(), text
        assert "Secret" not in text, text
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text


def test_a_missing_manifest_withholds_the_name_it_was_given(tmp_path: Path) -> None:
    missing = tmp_path / f"{VALUE_SENTINEL}.yaml"
    run = _run("--project", missing.as_posix(), cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr == (
        "Error: the project could not be read: the manifest --project names does not exist; its name is withheld\n"
    )


@pytest.mark.parametrize(
    "exc",
    [
        ValueError(f"Manifest field 'views_root' carries {VALUE_SENTINEL}"),
        ValueError(f"Manifest field 'dist_root' is written as the absolute path{VALUE_SENTINEL}"),
        RuntimeError(f"/ {VALUE_SENTINEL}"),
        OSError(f"/ {VALUE_SENTINEL}"),
    ],
    ids=["unknown-value-error", "no-space-after-the-prefix", "runtime", "os-error-text"],
)
def test_any_other_refusal_is_its_class_alone(exc: BaseException) -> None:
    sentence = read_failure_sentence(exc)
    assert VALUE_SENTINEL not in sentence
    assert sentence.startswith(type(exc).__name__)


@pytest.mark.parametrize("argv", [("a",), ("--project", "p", "b"), ("--unknown",)], ids=["positional", "two", "flag"])
def test_a_usage_error_exits_2(argv: tuple[str, ...], tmp_path: Path) -> None:
    run = _run(*argv, cwd=tmp_path)
    assert run.returncode == 2
    assert run.stdout == ""
    assert "usage:" in run.stderr


def test_the_passport_tuple_is_declared_disjoint_and_on_the_page() -> None:
    """The passport tuple is declared, disjoint from the other tuples, and has its own sentence on the page.

    The sentence names the failures the command has and what it does not reach."""
    declared = set(build_parser()._subparsers._group_actions[0].choices)
    tuple_ = set(guide_reference_cli.WRITES_A_PASSPORT)
    assert tuple_ == {"passport"}
    assert tuple_ <= declared
    for other in (
        guide_reference_cli.WRITES_NOTHING,
        guide_reference_cli.WRITES_ONLY_WITH_REPORT,
        guide_reference_cli.ALWAYS_EXITS_ZERO,
        guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST,
        guide_reference_cli.REFUSES_A_MODEL_CHECK_MODEL_REFUSES,
        guide_reference_cli.ANSWERS_A_HOOK_ON_STDOUT,
        guide_reference_cli.WRITES_A_CHANGE_REPORT,
    ):
        assert tuple_ & set(other) == set()
    assert "passport" not in project_args._PIN_ADVISORY_COMMANDS

    sentence = guide_reference_cli._behaviour_sentence("passport")
    section = guide_reference_cli.cli_page().split("## `swcraft passport`")[1].split("\n## ")[0]
    assert sentence in section
    assert "a project pinned to another SWCraft release" in sentence
    assert "a model with errors still gets its passport" in sentence
    assert "exits 2 before the command runs" in sentence
    assert "This command writes no file." not in section
    assert "always succeeds" not in section


# -- The names-free boundary: every step after argument parsing ------------------------------------


def _sentinel_project(tmp_path: Path) -> Path:
    """A valid project whose absolute path carries the sentinel, run from a cwd that carries it too."""
    return copy_specimen(SPECIMEN, tmp_path / f"{VALUE_SENTINEL} project")


def _assert_names_free(run: subprocess.CompletedProcess[str], root: Path) -> None:
    for text in (run.stdout, run.stderr):
        assert "Traceback" not in text, text
        assert VALUE_SENTINEL.lower() not in text.lower(), text
        assert str(root) not in text and root.as_posix() not in text, text


def test_an_unreadable_env_file_exits_1_with_its_reason_and_no_path(tmp_path: Path) -> None:
    root = _sentinel_project(tmp_path)
    env = root / ".env"
    env.write_text(f"SWCRAFT_{VALUE_SENTINEL.upper()}=1\n", encoding="utf-8", newline="\n")
    env.chmod(0)
    try:
        try:
            env.open("rb").close()
        except PermissionError:
            pass
        else:
            pytest.skip("this account reads a file whose permission bits refuse it (no capability to refuse)")
        run = _run("--project", _manifest(root), cwd=root)
    finally:
        env.chmod(0o644)
    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr.startswith("Error: the .env file could not be read: PermissionError: "), run.stderr
    _assert_names_free(run, root)


@pytest.mark.parametrize(
    "content",
    [
        f'{VALUE_SENTINEL} "unclosed\n'.encode(),
        f"KEY='{VALUE_SENTINEL}\n".encode(),
        b"\xff\xfe" + VALUE_SENTINEL.encode(),
    ],
    ids=["no-equals-sign", "unclosed-quote", "not-utf-8"],
)
def test_a_env_file_that_does_not_parse_echoes_nothing_of_it(content: bytes, tmp_path: Path) -> None:
    root = _sentinel_project(tmp_path)
    (root / ".env").write_bytes(content)
    run = _run("--project", _manifest(root), cwd=root)
    _assert_names_free(run, root)
    if run.returncode == 0:
        assert json.loads(run.stdout)
    else:
        assert run.returncode == 1 and run.stdout == ""
        assert run.stderr.startswith("Error: the .env file could not be read: "), run.stderr


def test_a_sentinel_path_through_every_step_is_echoed_nowhere(tmp_path: Path) -> None:
    root = _sentinel_project(tmp_path)
    (root / ".env").write_text(f"SWCRAFT_{VALUE_SENTINEL.upper()}=1\n", encoding="utf-8", newline="\n")
    run = _run("--project", _manifest(root), "--output", (root / "passport.json").as_posix(), cwd=root)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    _assert_names_free(run, root)


@pytest.mark.parametrize(
    "step",
    [
        "swcraft_tools.cli.passport_command.load_env_file",
        "swcraft_tools.cli.passport_command.guard_project_flag",
        "swcraft_tools.cli.passport_command.check_toolchain_pin",
    ],
    ids=["env", "project-flag", "pin"],
)
def test_any_step_that_raises_is_named_by_its_class_alone(
    step: str, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The boundary: an exception of any pre-pass step is a closed sentence or its class, never its text."""

    class SentinelError(OSError):
        pass

    def raises(*_args: object, **_kwargs: object) -> None:
        raise SentinelError(13, f"/{VALUE_SENTINEL}/denied", f"/{VALUE_SENTINEL}/.env")

    monkeypatch.setattr(step, raises)
    monkeypatch.setattr(sys, "argv", ["swcraft", "passport", "--project", _manifest(project)])
    with pytest.raises(SystemExit) as exited:
        cli_main()
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SentinelError, its text withheld" in captured.err
    assert VALUE_SENTINEL not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("argv", "loads"),
    [
        (["passport", "--project", "p"], False),
        (["--version", "passport"], False),
        (["check-model", "--project", "p"], True),
        (["--help"], True),
        ([], True),
    ],
    ids=["passport", "after-a-flag", "check-model", "help", "none"],
)
def test_only_passport_defers_the_env_load_past_parsing(
    argv: list[str], loads: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(project_args, "load_env_file", lambda: calls.append(None))
    project_args.load_env_file_unless_passport(argv)
    assert bool(calls) is loads
