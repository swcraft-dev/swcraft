"""The agent task benchmark's large bench is the ``large-bench`` specimen, pinned by the pin file (fast).

Held here: a built bench meets ``LARGE_DIGEST``; the digest reads the model alone; and task 10's
export through the binder is byte-equal to a plain scratch copy's export. The tests over the large
task set are in ``test_agent_task_bench_large.py`` (``slow``).

The ``specimen`` command, on stub generators: a built project carries SWCraft's manifest and the
generator's ``model/``; a rejection exits 2 with the line count, never the text; a defect or no
generator exits 1; an existing target or an unreadable configuration exits 2; no failure leaves a
target, and a target another process made is left alone.

Not reached: whether a bench resembles a real project, the real generator's answer on a user's
passport, and anything the checker or an agent does on either.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_repo_script, specimen_generator_module, take_specimen
from stub_generator import stub_command, write_stub

BENCH_MODULE = load_repo_script("agent_task_bench", "scripts/agent_task_bench.py")
GENERATOR = specimen_generator_module()

EXAMPLE = (
    REPO_ROOT
    / "packages"
    / "tools"
    / "tests"
    / "fixtures"
    / "contracts"
    / "model-passport"
    / "examples"
    / "minimal.json"
)
#: Written by a rejecting stub on every stderr line; it must never reach the command's output.
SENTINEL = "SentinelRefusalText"


@pytest.fixture(scope="module")
def large(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The large bench without the import export."""
    return BENCH_MODULE.build_large(tmp_path_factory.mktemp("large") / "bench", with_import=False)


def test_the_large_digest_is_the_pin_files() -> None:
    pins = GENERATOR.load_pins(REPO_ROOT)
    assert BENCH_MODULE.LARGE_DIGEST == pins["specimens"][BENCH_MODULE.LARGE_SPECIMEN]["model_sha256"]


def test_the_large_bench_is_the_pinned_specimen(large: Path) -> None:
    assert BENCH_MODULE.digest(large) == BENCH_MODULE.LARGE_DIGEST
    assert (large / "swcraft.yaml").read_text(encoding="utf-8") == BENCH_MODULE.MANIFEST
    assert BENCH_MODULE.MANIFEST == GENERATOR.manifest_text(BENCH_MODULE.LARGE_SPECIMEN)
    assert sorted(entry.name for entry in large.iterdir()) == ["model", "swcraft.yaml"]


def test_build_large_refuses_a_target_that_exists(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        BENCH_MODULE.build_large(tmp_path)


def test_large_bench_digest_is_computed_over_the_model_alone(tmp_path: Path) -> None:
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "a.yaml").write_text("name: a\n", encoding="utf-8", newline="\n")
    (tmp_path / "swcraft.yaml").write_text("ignored\n", encoding="utf-8", newline="\n")
    expected = hashlib.sha256(b"a.yaml\0name: a\n\0").hexdigest()
    assert BENCH_MODULE.digest(tmp_path) == expected


def test_the_import_group_directory_is_the_benchs_one(large: Path) -> None:
    groups = sorted((path.name for path in (large / "model" / "swc").iterdir() if path.is_dir()), key=str)
    assert [f"swc/{group}" for group in groups] == [BENCH_MODULE.IMPORT_GROUP_DIR]
    assert not (large / "model" / BENCH_MODULE.IMPORT_GROUP_DIR / f"{BENCH_MODULE.IMPORT_COMPONENT}.yaml").exists()


def _export_as_before(bench: Path, scratch: Path) -> bytes:
    """The reference: task 10's export from a plain scratch copy with the component added."""
    copy = scratch / "bench"
    shutil.copytree(bench / "model", copy / "model")
    (copy / "swcraft.yaml").write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    component = copy / "model" / BENCH_MODULE.IMPORT_GROUP_DIR / f"{BENCH_MODULE.IMPORT_COMPONENT}.yaml"
    component.write_text(BENCH_MODULE.IMPORT_COMPONENT_YAML, encoding="utf-8", newline="\n")
    out = scratch / f"{BENCH_MODULE.IMPORT_COMPONENT}.arxml"
    subprocess.run(
        [
            sys.executable, "-m", "swcraft_tools.cli", "generate", "arxml",
            "--project", (copy / "swcraft.yaml").as_posix(),
            "--component", BENCH_MODULE.IMPORT_COMPONENT, "--output", out.as_posix(),
        ],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )  # fmt: skip
    return out.read_bytes()


def test_write_import_through_the_binder_leaves_the_export_byte_equal(large: Path, tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    shutil.copytree(large, bench)
    target = BENCH_MODULE.write_import(bench)
    assert target == bench / "imports" / f"{BENCH_MODULE.IMPORT_COMPONENT}.arxml"
    (tmp_path / "before").mkdir()
    assert target.read_bytes() == _export_as_before(large, tmp_path / "before")
    assert BENCH_MODULE.digest(bench) == BENCH_MODULE.LARGE_DIGEST, "the export leaves the model as it was"


# ---------------------------------------------------------------------------
# The specimen command, on stub generators.
# ---------------------------------------------------------------------------


def _specimen(command: list[str], target: Path, config: Path = EXAMPLE) -> int:
    return BENCH_MODULE.main(["specimen", config.as_posix(), target.as_posix()], command=command)


def test_a_built_specimen_carries_swcrafts_manifest_and_the_generators_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = take_specimen("example-minimal")
    command = stub_command(write_stub(tmp_path / "stub"), "copy", source.as_posix())
    target = tmp_path / "specimen"
    assert _specimen(command, target) == 0
    assert (target / "swcraft.yaml").read_text(encoding="utf-8") == GENERATOR.manifest_text(
        BENCH_MODULE.SPECIMEN_PASSPORT_NAME
    )
    assert sorted(entry.name for entry in target.iterdir()) == ["model", "swcraft.yaml"]
    assert BENCH_MODULE.digest(target) == GENERATOR.digest(source / "model")
    assert capsys.readouterr().out == f"specimen in {target}, digest {BENCH_MODULE.digest(target)}\n"


def test_the_generator_is_handed_the_configuration_as_a_passport_file(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    script = tmp_path / "stub" / "keeping_the_passport.py"
    script.parent.mkdir()
    script.write_text(
        "import shutil, sys\n"
        "argv = sys.argv[1:]\n"
        f"shutil.copyfile(argv[argv.index('specimen') + 1], {seen.as_posix()!r})\n"
        "sys.exit(2)\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _specimen([sys.executable, script.as_posix()], tmp_path / "specimen") == 2
    assert json.loads(seen.read_text(encoding="utf-8")) == json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_a_rejected_configuration_exits_2_with_the_line_count_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = stub_command(write_stub(tmp_path / "stub"), "reject", "3", SENTINEL)
    target = tmp_path / "specimen"
    assert _specimen(command, target) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: the specimen generator rejects the configuration (3 lines on its stderr)\n"
    assert SENTINEL not in captured.err
    assert not target.exists()


@pytest.mark.parametrize(("mode", "cause"), [(("exit", "1"), "exit 1"), (("empty",), "exit 0 without model/")])
def test_a_generator_defect_exits_1_naming_its_cause(
    mode: tuple[str, ...], cause: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = stub_command(write_stub(tmp_path / "stub"), *mode)
    target = tmp_path / "specimen"
    assert _specimen(command, target) == 1
    assert capsys.readouterr().err == f"error: the specimen generator failed ({cause})\n"
    assert not target.exists()


def test_a_generator_that_is_not_configured_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GENERATOR.VARIABLE, (tmp_path / "no-generator").as_posix())
    target = tmp_path / "specimen"
    assert BENCH_MODULE.main(["specimen", EXAMPLE.as_posix(), target.as_posix()]) == 1
    assert capsys.readouterr().err.startswith(f"error: {GENERATOR.VARIABLE} names no file")
    assert not target.exists()


@pytest.mark.parametrize(("text", "kind"), [("{not json", "JSONDecodeError"), ("[1, 2]", "ValueError")])
def test_an_unreadable_configuration_exits_2_naming_its_class(
    text: str, kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text(text, encoding="utf-8", newline="\n")
    command = stub_command(write_stub(tmp_path / "stub"), "exit", "1")
    target = tmp_path / "specimen"
    assert _specimen(command, target, config) == 2
    assert capsys.readouterr().err == f"error: the configuration could not be read ({kind})\n"
    assert not target.exists()


def test_a_specimen_target_that_exists_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    command = stub_command(write_stub(tmp_path / "stub"), "exit", "1")
    assert _specimen(command, tmp_path) == 2
    assert "exists" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Cleanup removes only what this invocation created.
# ---------------------------------------------------------------------------


def _made_by_another(target: Path) -> None:
    """Another process's directory at *target*, made after the command's own existence check."""
    target.mkdir()
    (target / "theirs.txt").write_text("not this call's\n", encoding="utf-8", newline="\n")


def test_a_large_target_made_by_another_after_the_check_is_left_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "bench"
    source = take_specimen(BENCH_MODULE.LARGE_SPECIMEN)

    def taking(*args: object, **kwargs: object) -> Path:
        _made_by_another(target)
        return source

    monkeypatch.setattr(BENCH_MODULE._GENERATOR, "take", taking)
    assert BENCH_MODULE.main(["large", target.as_posix()]) == 2
    assert "exists" in capsys.readouterr().err
    assert sorted(entry.name for entry in target.iterdir()) == ["theirs.txt"]


def test_a_specimen_target_made_by_another_after_the_check_is_left_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "specimen"
    command = stub_command(write_stub(tmp_path / "stub"), "exit", "1")
    given = BENCH_MODULE._GENERATOR.generator_command

    def resolving(*args: object) -> list[str]:
        _made_by_another(target)
        return given(*args)

    monkeypatch.setattr(BENCH_MODULE._GENERATOR, "generator_command", resolving)
    assert _specimen(command, target) == 2
    assert "exists" in capsys.readouterr().err
    assert sorted(entry.name for entry in target.iterdir()) == ["theirs.txt"]


def test_a_failure_while_the_output_is_normalised_leaves_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "out"
    target = parent / "specimen"

    def failing(command: list[str], document: object, out: Path, name: str) -> object:
        (out / "model").mkdir(parents=True)
        raise OSError("swcraft.yaml could not be written")

    monkeypatch.setattr(BENCH_MODULE._GENERATOR, "run_document", failing)
    with pytest.raises(OSError):
        BENCH_MODULE.build_specimen({}, target, command=[sys.executable])
    assert list(parent.iterdir()) == []


def test_a_failed_large_copy_removes_the_target_it_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "out"
    parent.mkdir()
    target = parent / "bench"

    monkeypatch.setattr(BENCH_MODULE._GENERATOR, "take", lambda *args, **kwargs: tmp_path / "vanished")
    with pytest.raises(OSError):
        BENCH_MODULE.build_large(target)
    assert list(parent.iterdir()) == []
