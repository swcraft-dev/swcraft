"""``scripts/specimen_generator.py``: where the specimen generator is, how it is run, what it answered.

Every test runs a stub generator (``stub_generator.py``) through ``run``'s ``command`` argument, never
the variable or the real generator, so the module is held with no generator installed. The POSIX-only
cases use a one-line ``sh`` wrapper, skipped where the executable bit means nothing.

The last class is the call-site scan: an AST read of ``packages/tools/tests``, ``packages/mcp/tests``,
``packages/agent/tests`` and ``scripts/`` that holds the callers of ``configured()``,
``generator_command()``, ``run()`` and ``run_document()``, the spelling of the variable's value and
``keep_stderr=True`` to the named sites. The scripts and tests the open copy does not carry are
scanned by a closed half.

What this module does not reach: the real generator (``test_specimens.py`` runs it); a module that
starts a generator by its own ``subprocess`` call, a hard-coded path, or a caller binding the module
to a name that does not say *generator* -- the scan reads call shapes, not every process started.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from conftest import REPO_ROOT, agent_task_bench_module, specimen_generator_module
from mode_bit_support import require_executable_bit
from stub_generator import stub_command, write_sh_wrapper, write_stub

_GENERATOR = specimen_generator_module()

SENTINEL = "zq-sentinel-stderr-7f3a"

#: The stub's sleep, the child the timeout has to kill.
_STUB_SLEEP_SECONDS = 300

#: The hang guard of the timeout's kill: under half the stub's sleep, over a hundred times the normal
#: 1 s timeout plus the kill.
KILL_HANG_SECONDS = _STUB_SLEEP_SECONDS * 0.4


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    return write_stub(tmp_path / "stub")


@pytest.fixture
def passport(tmp_path: Path) -> Path:
    path = tmp_path / "passports" / "demo.passport.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"contract_version": "8.0.0"}\n', encoding="utf-8", newline="\n")
    return path


def _generated_project(root: Path) -> Path:
    """What the real generator writes: its own manifest, a sidecar, ``model/`` with a ``.gitkeep``."""
    (root / "model" / "interfaces").mkdir(parents=True)
    (root / "model" / "tasks").mkdir(parents=True)
    (root / "model" / "tasks" / ".gitkeep").write_bytes(b"")
    (root / "model" / "interfaces" / "If.yaml").write_bytes(b"name: If\n")
    (root / "swcraft.yaml").write_text('toolchain_version: "0.19.2"\n', encoding="utf-8", newline="\n")
    (root / "generation.json").write_text("{}\n", encoding="utf-8", newline="\n")
    (root / "extra").mkdir()
    (root / "extra" / "note.txt").write_text("x\n", encoding="utf-8", newline="\n")
    return root


# -- run: the three outcomes -----------------------------------------------------------------------


class TestRunOutcomes:
    def test_exit_0_with_a_model_is_built_with_swcrafts_manifest_and_nothing_else(self, tmp_path, stub, passport):
        source = _generated_project(tmp_path / "generated")
        out = tmp_path / "out"

        outcome = _GENERATOR.run(stub_command(stub, "copy", os.fspath(source)), passport, out)

        assert outcome == _GENERATOR.Built(out)
        assert sorted(entry.name for entry in out.iterdir()) == ["model", "swcraft.yaml"]
        assert (out / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text("demo")
        assert (out / "model" / "tasks" / ".gitkeep").is_file()
        assert _GENERATOR.digest(out / "model") == _GENERATOR.digest(source / "model")

    def test_exit_2_is_rejected_with_the_line_count_and_no_text(self, tmp_path, stub, passport):
        outcome = _GENERATOR.run(stub_command(stub, "reject", "3", SENTINEL), passport, tmp_path / "out")

        assert outcome == _GENERATOR.Rejected(3, None)
        assert SENTINEL not in repr(outcome)
        assert all(SENTINEL not in str(value) for value in vars(outcome).values())

    def test_exit_2_keeps_the_text_for_the_caller_that_asks(self, tmp_path, stub, passport):
        outcome = _GENERATOR.run(
            stub_command(stub, "reject", "2", SENTINEL), passport, tmp_path / "out", keep_stderr=True
        )

        assert isinstance(outcome, _GENERATOR.Rejected)
        assert outcome.lines == 2
        assert outcome.text is not None and outcome.text.count(SENTINEL) == 2

    @pytest.mark.parametrize("code", ["1", "3"])
    def test_another_exit_is_a_defect_naming_the_code(self, tmp_path, stub, passport, code):
        outcome = _GENERATOR.run(stub_command(stub, "exit", code), passport, tmp_path / "out")
        assert outcome == _GENERATOR.GeneratorDefect(f"exit {code}")

    def test_exit_0_without_a_model_is_a_defect(self, tmp_path, stub, passport):
        outcome = _GENERATOR.run(stub_command(stub, "empty"), passport, tmp_path / "out")
        assert outcome == _GENERATOR.GeneratorDefect("exit 0 without model/")

    def test_a_command_that_cannot_start_is_a_defect_naming_the_exception_class(self, tmp_path, passport):
        outcome = _GENERATOR.run([os.fspath(tmp_path / "no-such-generator")], passport, tmp_path / "out")
        assert isinstance(outcome, _GENERATOR.GeneratorDefect)
        assert outcome.cause in {"FileNotFoundError", "PermissionError", "OSError"}

    def test_a_generator_past_the_timeout_is_killed_and_a_timeout_defect(self, tmp_path, stub, passport):
        started = time.monotonic()
        outcome = _GENERATOR.run(
            stub_command(stub, "sleep", str(_STUB_SLEEP_SECONDS)), passport, tmp_path / "out", timeout=1
        )
        assert outcome == _GENERATOR.GeneratorDefect("timeout")
        assert time.monotonic() - started < KILL_HANG_SECONDS

    def test_the_ceiling_is_600_seconds(self):
        assert _GENERATOR.TIMEOUT_SECONDS == 600


class TestRunArguments:
    def test_the_argv_is_specimen_passport_out_absolute_and_the_cwd_a_temporary_directory(
        self, tmp_path, stub, passport, monkeypatch
    ):
        record = tmp_path / "record.jsonl"
        monkeypatch.setenv("STUB_RECORD", os.fspath(record))
        monkeypatch.chdir(tmp_path)
        source = _generated_project(tmp_path / "generated")

        _GENERATOR.run(stub_command(stub, "copy", os.fspath(source)), Path("passports/demo.passport.json"), Path("out"))

        (line,) = record.read_text(encoding="utf-8").splitlines()
        seen = json.loads(line)
        assert seen["argv"][-4:] == ["specimen", os.fspath(passport), "--out", os.fspath(tmp_path / "out")]
        assert Path(seen["cwd"]).resolve() != tmp_path.resolve()
        assert not Path(seen["cwd"]).exists(), "the working directory is a temporary one, removed after"

    def test_a_command_and_paths_with_spaces_run(self, tmp_path, passport):
        script = write_stub(tmp_path / "a stub dir")
        source = _generated_project(tmp_path / "gen erated")
        out = tmp_path / "an out dir"
        outcome = _GENERATOR.run(stub_command(script, "copy", os.fspath(source)), passport, out)
        assert outcome == _GENERATOR.Built(out)

    @pytest.mark.parametrize(
        ("file_name", "expected"),
        [("large-bench.passport.json", "large-bench"), ("twin.passport.json", "twin")],
    )
    def test_the_manifest_names_the_specimen_after_its_passport_file(self, file_name, expected):
        assert _GENERATOR.specimen_name(Path("x") / file_name) == expected

    def test_run_document_hands_the_document_over_as_one_serialisation(self, tmp_path):
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
        document = {"layers": {"swc": 3}, "contract_version": "8.0.0"}
        outcome = _GENERATOR.run_document([sys.executable, os.fspath(script)], document, tmp_path / "out", "demo")
        assert outcome == _GENERATOR.Rejected(0, None)
        assert seen.read_text(encoding="utf-8") == json.dumps(document, indent=2, sort_keys=True) + "\n"

    def test_run_document_names_the_manifest_after_the_name_given(self, tmp_path, stub):
        source = _generated_project(tmp_path / "generated")
        out = tmp_path / "out"
        outcome = _GENERATOR.run_document(stub_command(stub, "copy", os.fspath(source)), {}, out, "twin")
        assert outcome == _GENERATOR.Built(out)
        assert (out / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text("twin")


# -- the manifest and the digest -------------------------------------------------------------------


class TestManifestAndDigest:
    def test_the_manifest_is_the_template_with_the_name_and_no_toolchain_pin(self):
        text = _GENERATOR.manifest_text("large-bench")
        assert "name: specimen-large-bench\n" in text
        assert "toolchain_version:" not in text.replace("`toolchain_version`", "")
        assert "model_root: model\n" in text

    def test_the_digest_is_the_benchs_on_a_copy_of_the_showcase_bench(self, tmp_path):
        bench = tmp_path / "bench"
        shutil.copytree(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench" / "bench", bench)
        assert _GENERATOR.digest(bench / "model") == agent_task_bench_module().digest(bench)

    def test_the_digest_moves_with_a_byte_and_with_a_name(self, tmp_path):
        source = _generated_project(tmp_path / "a")
        before = _GENERATOR.digest(source / "model")
        (source / "model" / "interfaces" / "If.yaml").write_bytes(b"name: If2\n")
        changed = _GENERATOR.digest(source / "model")
        (source / "model" / "interfaces" / "If.yaml").rename(source / "model" / "interfaces" / "Ig.yaml")
        assert len({before, changed, _GENERATOR.digest(source / "model")}) == 3


# -- configured ------------------------------------------------------------------------------------


def _repo_with_pins(root: Path) -> Path:
    """A repository root holding a copy of the real pin file, and no ``.venv``."""
    target = root / _GENERATOR.PIN_FILE
    target.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / _GENERATOR.PIN_FILE, target)
    return root


def _rewrite_pins(repo: Path, *, top: dict | None = None, **changes: object) -> None:
    """The copied pin file with its generator block changed (``None`` removes a key) and *top*'s keys set."""
    document = json.loads((repo / _GENERATOR.PIN_FILE).read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            del document["generator"][key]
        else:
            document["generator"][key] = value
    document.update(top or {})
    (repo / _GENERATOR.PIN_FILE).write_text(json.dumps(document), encoding="utf-8", newline="\n")


def _install(repo: Path) -> Path:
    """An empty file at ``.venv``'s generator path of *repo*."""
    installed = _GENERATOR.installed_executable(repo)
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    return installed


class TestConfigured:
    def test_unset_and_not_installed_is_refused_naming_the_variable_and_install(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.configured({}, repo)
        assert excinfo.value.reason == "unset"
        assert _GENERATOR.VARIABLE in str(excinfo.value)
        assert "dev.py install" in str(excinfo.value) and ".venv" in str(excinfo.value)

    def test_a_relative_path_is_refused(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.configured({_GENERATOR.VARIABLE: "bin/generator"}, repo)
        assert excinfo.value.reason == "relative"
        assert "absolute" in str(excinfo.value)

    def test_a_path_naming_no_file_is_refused(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.configured({_GENERATOR.VARIABLE: os.fspath(tmp_path / "absent")}, repo)
        assert excinfo.value.reason == "missing"
        assert _GENERATOR.VARIABLE in str(excinfo.value)

    @pytest.mark.parametrize("name", ["generator.cmd", "generator.BAT"])
    def test_a_batch_file_is_refused_even_when_it_exists(self, tmp_path, name):
        repo = _repo_with_pins(tmp_path / "repo")
        batch = tmp_path / name
        batch.write_text("@echo off\n", encoding="utf-8", newline="\n")
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.configured({_GENERATOR.VARIABLE: os.fspath(batch)}, repo)
        assert excinfo.value.reason == "batch-file"
        assert "cmd.exe" in str(excinfo.value)

    def test_the_variables_file_is_taken_spaces_and_all(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        generator = tmp_path / "a generator dir" / "gen"
        generator.parent.mkdir()
        generator.write_bytes(b"")
        assert _GENERATOR.configured({_GENERATOR.VARIABLE: os.fspath(generator)}, repo) == generator

    def test_the_venv_install_is_taken_when_the_variable_is_unset(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        installed = _install(repo)
        assert _GENERATOR.configured({}, repo) == installed

    def test_generator_command_is_the_given_command_else_the_configured_one(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        assert _GENERATOR.generator_command(["a", "b"], {}, repo) == ["a", "b"]
        generator = tmp_path / "gen"
        generator.write_bytes(b"")
        assert _GENERATOR.generator_command(None, {_GENERATOR.VARIABLE: os.fspath(generator)}, repo) == [
            os.fspath(generator)
        ]
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.generator_command(None, {}, repo)
        assert excinfo.value.reason == "unset"

    def test_the_variable_wins_over_the_venv_install(self, tmp_path):
        repo = _repo_with_pins(tmp_path / "repo")
        _install(repo)
        other = tmp_path / "other"
        other.write_bytes(b"")
        assert _GENERATOR.configured({_GENERATOR.VARIABLE: os.fspath(other)}, repo) == other

    @pytest.mark.parametrize(
        ("os_name", "parts"),
        [("posix", ("bin", "{name}")), ("nt", ("Scripts", "{name}.exe"))],
    )
    def test_the_installed_path_is_the_pin_files_executable_in_the_repositorys_venv(self, tmp_path, os_name, parts):
        repo = _repo_with_pins(tmp_path / "repo")
        name = _GENERATOR.load_pins(repo)["generator"]["executable"]
        expected = (repo / ".venv").joinpath(*(part.format(name=name) for part in parts))
        assert _GENERATOR.installed_executable(repo, os_name=os_name) == expected

    def test_path_is_never_searched(self, tmp_path, monkeypatch):
        """A stub under the pin's executable name, first on ``PATH``, and the variable unset: refused."""
        repo = _repo_with_pins(tmp_path / "repo")
        name = _GENERATOR.load_pins(repo)["generator"]["executable"]
        on_path = tmp_path / "on-path"
        on_path.mkdir()
        for candidate in (name, f"{name}.exe"):
            (on_path / candidate).write_bytes(b"")
            (on_path / candidate).chmod(0o755)
        monkeypatch.setenv("PATH", os.pathsep.join([os.fspath(on_path), os.environ.get("PATH", "")]))
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as excinfo:
            _GENERATOR.configured({"PATH": os.environ["PATH"]}, repo)
        assert excinfo.value.reason == "unset"

    @pytest.mark.parametrize("key", ["executable", "bin_variable", "imports"])
    def test_the_pin_file_is_refused_without_a_full_generator_block(self, tmp_path, key):
        repo = _repo_with_pins(tmp_path / "repo")
        _rewrite_pins(repo, **{key: None})
        with pytest.raises(ValueError, match=f"lacks {key}"):
            _GENERATOR.load_pins(repo)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("version", "0.1.0"),
            ("archive", "generator-0.1.0.zip"),
            ("archive_sha256", "ab" * 32),
            ("store", "/releases/public/generator/v0.1.0"),
            ("bundled", "generator"),
            ("requirements", "specimen-generator.requirements.txt"),
            ("contract_major", 12),
        ],
    )
    def test_a_removed_field_is_refused_by_name_pointing_at_the_source(self, tmp_path, key, value):
        """A removed field is refused by name, pointing at the generator's source.

        The generator is ``packages/specimens/``'s source: a pinned artifact, its closure or a recorded
        contract major in the block could only disagree with it.
        """
        repo = _repo_with_pins(tmp_path / "repo")
        _rewrite_pins(repo, **{key: value})
        with pytest.raises(ValueError) as excinfo:
            _GENERATOR.load_pins(repo)
        assert key in str(excinfo.value) and "packages/specimens/" in str(excinfo.value)
        assert "no longer carries" in str(excinfo.value)

    @pytest.mark.parametrize("value", [None, "contract-13"])
    def test_a_migration_is_refused_by_name_set_or_null(self, tmp_path, value):
        """The migration window is retired: a contract major moves the generator and the passports together."""
        repo = _repo_with_pins(tmp_path / "repo")
        _rewrite_pins(repo, top={"migration": value})
        with pytest.raises(ValueError) as excinfo:
            _GENERATOR.load_pins(repo)
        assert "migration" in str(excinfo.value) and "no longer carries" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("key", "value", "sentence"),
        [
            ("bin_variable", "specimens_bin", "upper-case variable name"),
            ("bin_variable", "SPECIMENS BIN", "upper-case variable name"),
            ("executable", "", "must be a file name"),
            ("executable", "bin/swcraft-specimens", "must be a file name"),
            ("executable", 3, "must be a file name"),
            ("imports", [], "list of module names"),
            ("imports", "swcraft_specimens", "list of module names"),
            ("imports", ["swcraft_specimens", ""], "list of module names"),
        ],
    )
    def test_an_unfit_field_is_refused_by_name(self, tmp_path, key, value, sentence):
        repo = _repo_with_pins(tmp_path / "repo")
        _rewrite_pins(repo, **{key: value})
        with pytest.raises(ValueError, match=sentence):
            _GENERATOR.load_pins(repo)

    def test_the_pin_file_alone_is_read(self, tmp_path):
        """No runtime pins: the generator's identity is the source, and ``load_pins`` reads one file."""
        repo = _repo_with_pins(tmp_path / "repo")
        assert not (repo / "scripts").exists()
        assert _GENERATOR.load_pins(repo)["generator"]["executable"]


class TestThePinFile:
    def test_the_committed_generator_block_is_complete(self):
        document = _GENERATOR.load_pins(REPO_ROOT)
        generator = document["generator"]
        assert sorted(generator) == sorted(_GENERATOR.GENERATOR_KEYS)
        assert not set(_GENERATOR.REMOVED_GENERATOR_KEYS) & set(generator)
        assert not set(_GENERATOR.REMOVED_PIN_KEYS) & set(document)
        assert sorted(document) == ["generator", "specimens"]
        assert isinstance(document["specimens"], dict)
        assert generator["bin_variable"] == "SWCRAFT_SPECIMENS_BIN"
        assert generator["imports"][0] == "swcraft_specimens"
        assert len(set(generator["imports"])) == len(generator["imports"])

    def test_the_executable_is_the_generators_console_script(self):
        """The pin's ``executable`` is the one console script ``packages/specimens/pyproject.toml`` declares."""
        project = tomllib.loads((REPO_ROOT / "packages" / "specimens" / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = project["project"]["scripts"]
        executable = _GENERATOR.load_pins(REPO_ROOT)["generator"]["executable"]
        assert list(scripts) == [executable] == ["swcraft-specimens"]
        assert scripts[executable].split(":")[0].startswith("swcraft_specimens")

    @pytest.mark.parametrize(
        ("source", "constant"),
        [
            ("tests/oracle/generator_start.py", "BIN_VARIABLE"),
            ("tests/scenarios/run_scenarios.py", "SPECIMENS_BIN_VARIABLE"),
        ],
        ids=["start-helper", "scenario-runner"],
    )
    def test_the_bin_variable_is_the_one_the_generators_start_sites_read(self, source, constant):
        """The integration suite's one start helper and the scenario runner read the pin's variable."""
        module = REPO_ROOT / "packages" / "specimens" / source
        assigned = {
            node.targets[0].id: node.value.value
            for node in ast.parse(module.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            if isinstance(node.value, ast.Constant)
        }
        assert assigned[constant] == _GENERATOR.load_pins(REPO_ROOT)["generator"]["bin_variable"]

    def test_the_source_directory_is_the_generators_package(self):
        assert (REPO_ROOT / _GENERATOR.SOURCE_DIR / "__init__.py").is_file()
        assert _GENERATOR.SOURCE_DIR.name == _GENERATOR.load_pins(REPO_ROOT)["generator"]["imports"][0]


# -- the interpreter a regeneration runs on -------------------------------------------------------


def _venv_with(root: Path, config: str | None, *, os_name: str = os.name) -> Path:
    """A virtual environment's shape: ``pyvenv.cfg`` holding *config* (none when ``None``) and an empty generator."""
    executable = _GENERATOR.venv_python(root, os_name).parent / "swcraft-specimens"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    if config is not None:
        (root / "pyvenv.cfg").write_text(config, encoding="utf-8", newline="\n")
    return executable


class TestTheInterpreter:
    @pytest.mark.parametrize(
        ("config", "found"),
        [
            ("home = /usr/bin\nversion = 3.11.13\n", (3, 11)),
            ("home = /usr/bin\nversion_info = 3.12.4.final.0\n", (3, 12)),
            ("version = 3.13.0\nversion_info = 3.13.0\n", (3, 13)),
            ("home = /usr/bin\n", None),
            ("version = three\n", None),
            (None, None),
        ],
        ids=["venv-3.11", "uv-3.12", "both-3.13", "no-version", "unreadable", "no-pyvenv-cfg"],
    )
    def test_the_interpreter_is_read_from_pyvenv_cfg(self, tmp_path, config, found):
        assert _GENERATOR.generator_interpreter(_venv_with(tmp_path / "venv", config)) == found

    def test_a_link_is_resolved_to_its_environment_first(self, tmp_path):
        target = _venv_with(tmp_path / "venv", "version = 3.12.1\n")
        link = tmp_path / "bin" / "swcraft-specimens"
        link.parent.mkdir()
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("this volume or account cannot create a symbolic link")
        assert _GENERATOR.generator_interpreter(link) == (3, 12)

    def test_the_pinned_interpreter_is_3_11_and_meets_no_finding(self, tmp_path):
        assert _GENERATOR.GENERATOR_PYTHON == (3, 11)
        assert _GENERATOR.interpreter_finding(_venv_with(tmp_path / "venv", "version = 3.11.13\n")) is None

    @pytest.mark.parametrize(
        ("config", "said"),
        [("version_info = 3.12.4\n", "runs on Python 3.12"), (None, "no pyvenv.cfg beside it states")],
    )
    def test_another_or_an_unknown_interpreter_is_a_finding_naming_the_pinned_one(self, tmp_path, config, said):
        finding = _GENERATOR.interpreter_finding(_venv_with(tmp_path / "venv", config))
        assert finding is not None and said in finding
        assert "pinned on Python 3.11 alone" in finding and ".prototools" in finding
        assert _GENERATOR.VARIABLE in finding and "dev.py install" in finding

    def test_the_repositorys_venv_generator_runs_on_the_pinned_interpreter(self):
        """``.venv`` is made by the bootstrap interpreter ``.prototools`` pins, so its generator meets no finding."""
        installed = _GENERATOR.installed_executable(REPO_ROOT)
        assert installed.is_file(), f"precondition: {installed} is absent; run `python scripts/dev.py install`"
        assert _GENERATOR.interpreter_finding(installed) is None

    def test_the_repositorys_venv_imports_every_module_the_pin_file_names(self):
        """``.venv``'s interpreter, isolated, imports each name of ``generator.imports``.

        In a child process: SWCraft's test trees do not import the generator.
        """
        python = _GENERATOR.venv_python(REPO_ROOT / _GENERATOR.VENV_DIR)
        assert python.is_file(), f"precondition: {python} is absent; run `python scripts/dev.py install`"
        imports = _GENERATOR.load_pins(REPO_ROOT)["generator"]["imports"]
        probe = "import importlib, sys\nfor name in sys.argv[1:]:\n    importlib.import_module(name)\n"
        completed = subprocess.run(
            [os.fspath(python), "-I", "-c", probe, *imports],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=REPO_ROOT,
        )
        assert completed.returncode == 0, completed.stderr


# -- the source digest ----------------------------------------------------------------------------


def _source_tree(root: Path) -> Path:
    """A repository root holding a small generator source under :data:`SOURCE_DIR`."""
    package = root / _GENERATOR.SOURCE_DIR
    (package / "presets").mkdir(parents=True)
    (package / "__init__.py").write_text('"""The package."""\n', encoding="utf-8", newline="\n")
    (package / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8", newline="\n")
    (package / "presets" / "small.yaml").write_text("seed: 1\n", encoding="utf-8", newline="\n")
    return root


class TestTheSourceDigest:
    def test_a_changed_source_file_changes_the_digest(self, tmp_path):
        repo = _source_tree(tmp_path / "repo")
        before = _GENERATOR.source_digest(repo)
        cli = repo / _GENERATOR.SOURCE_DIR / "cli.py"
        cli.write_text("def main():\n    return 1\n", encoding="utf-8", newline="\n")
        assert _GENERATOR.source_digest(repo) != before

    def test_a_changed_package_data_file_changes_the_digest(self, tmp_path):
        repo = _source_tree(tmp_path / "repo")
        before = _GENERATOR.source_digest(repo)
        preset = repo / _GENERATOR.SOURCE_DIR / "presets" / "small.yaml"
        preset.write_text("seed: 2\n", encoding="utf-8", newline="\n")
        assert _GENERATOR.source_digest(repo) != before

    def test_a_new_module_counts_tracked_or_not(self, tmp_path):
        """On disk, not tracked: the fake repository is no git work tree, and the new module counts."""
        repo = _source_tree(tmp_path / "repo")
        before = _GENERATOR.source_digest(repo)
        (repo / _GENERATOR.SOURCE_DIR / "new_module.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
        assert _GENERATOR.source_digest(repo) != before

    def test_a_renamed_file_changes_the_digest(self, tmp_path):
        repo = _source_tree(tmp_path / "repo")
        before = _GENERATOR.source_digest(repo)
        package = repo / _GENERATOR.SOURCE_DIR
        (package / "cli.py").rename(package / "cli2.py")
        assert _GENERATOR.source_digest(repo) != before

    def test_bytecode_does_not_count(self, tmp_path):
        repo = _source_tree(tmp_path / "repo")
        before = _GENERATOR.source_digest(repo)
        package = repo / _GENERATOR.SOURCE_DIR
        (package / "__pycache__").mkdir()
        (package / "__pycache__" / "cli.cpython-311.pyc").write_bytes(b"\x00bytecode")
        (package / "__pycache__" / "notes.txt").write_text("x\n", encoding="utf-8", newline="\n")
        (package / "presets" / "stray.pyc").write_bytes(b"\x00")
        assert _GENERATOR.source_digest(repo) == before

    def test_the_framing_tells_a_moved_boundary_apart(self, tmp_path):
        """``a`` holding ``bc`` and ``ab`` holding ``c`` are two trees, so two digests."""
        one = tmp_path / "one" / _GENERATOR.SOURCE_DIR
        two = tmp_path / "two" / _GENERATOR.SOURCE_DIR
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        (one / "a").write_bytes(b"bc")
        (two / "ab").write_bytes(b"c")
        assert _GENERATOR.source_digest(tmp_path / "one") != _GENERATOR.source_digest(tmp_path / "two")

    def test_an_absent_source_is_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="swcraft_specimens is absent"):
            _GENERATOR.source_digest(tmp_path)

    def test_the_source_key_is_the_digests_first_sixteen_once_per_process(self, tmp_path):
        repo = _source_tree(tmp_path / "repo")
        _GENERATOR.forget_taken()
        try:
            key = _GENERATOR.source_key(repo)
            assert key == _GENERATOR.source_digest(repo)[: _GENERATOR.SOURCE_KEY_LENGTH] and len(key) == 16
            (repo / _GENERATOR.SOURCE_DIR / "cli.py").write_text("changed\n", encoding="utf-8", newline="\n")
            assert _GENERATOR.source_key(repo) == key, "held for the process"
            _GENERATOR.forget_taken()
            assert _GENERATOR.source_key(repo) != key
        finally:
            _GENERATOR.forget_taken()

    def test_an_absent_source_is_a_refusal_naming_it(self, tmp_path):
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _GENERATOR.source_key(tmp_path)
        assert caught.value.reason == "generator-source"
        assert _GENERATOR.SOURCE_DIR.as_posix() in str(caught.value)


# -- the POSIX call site: one executable file ------------------------------------------------------


class TestAnExecutableGenerator:
    def test_a_configured_wrapper_whose_path_has_spaces_runs(self, tmp_path, passport):
        require_executable_bit(tmp_path)
        repo = _repo_with_pins(tmp_path / "repo")
        script = write_stub(tmp_path / "stub")
        source = _generated_project(tmp_path / "generated")
        wrapper = write_sh_wrapper(tmp_path / "a generator dir" / "generator", script, "copy", os.fspath(source))

        command = [os.fspath(_GENERATOR.configured({_GENERATOR.VARIABLE: os.fspath(wrapper)}, repo))]
        outcome = _GENERATOR.run(command, passport, tmp_path / "out")

        assert outcome == _GENERATOR.Built(tmp_path / "out")


# -- the call-site scan ----------------------------------------------------------------------------

SCANNED_TREES = ("packages/tools/tests", "packages/mcp/tests", "packages/agent/tests", "scripts")
DEFINING_MODULE = "scripts/specimen_generator.py"
THIS_MODULE = "scripts/tests/test_specimen_generator.py"

#: The defining module's functions that find or start a generator: what the scan looks for.
GENERATOR_FUNCTIONS = frozenset({"configured", "generator_command", "run", "run_document"})

#: ``(module, function)`` -> the module's callers of :data:`GENERATOR_FUNCTIONS`. Only the defining
#: module calls ``configured`` outside this module; the pin script runs its committed passport files,
#: the bench and the calibration hand theirs over as data (``run_document``).
EXPECTED_CALLERS = {
    (THIS_MODULE, "configured"),
    (THIS_MODULE, "generator_command"),
    (THIS_MODULE, "run"),
    (THIS_MODULE, "run_document"),
    (DEFINING_MODULE, "configured"),
    (DEFINING_MODULE, "generator_command"),
    (DEFINING_MODULE, "run"),
    ("scripts/specimens.py", "generator_command"),
    ("scripts/specimens.py", "run"),
    ("scripts/agent_task_bench.py", "generator_command"),
    ("scripts/agent_task_bench.py", "run_document"),
    ("packages/mcp/tests/functional/real_models.py", "generator_command"),
    ("packages/mcp/tests/functional/real_models.py", "run_document"),
}

#: The modules allowed to spell the variable's value.
VARIABLE_SPELLERS = {DEFINING_MODULE, "scripts/dev.py"}

#: The modules allowed to pass ``keep_stderr=True``: the pin script and this module's test of it.
KEEP_STDERR_CALLERS = {"scripts/specimens.py", THIS_MODULE}


def _python_modules() -> list[tuple[str, ast.Module]]:
    modules = []
    for tree in SCANNED_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py"), key=lambda candidate: candidate.as_posix()):
            relative = path.relative_to(REPO_ROOT).as_posix()
            modules.append((relative, ast.parse(path.read_text(encoding="utf-8"))))
    return modules


def _calls_the_generator(module: str, node: ast.Call) -> str | None:
    """The name in :data:`GENERATOR_FUNCTIONS` when *node* calls the module's function of that name, else ``None``."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in GENERATOR_FUNCTIONS:
        return func.attr if "generator" in ast.unparse(func.value).lower() else None
    if isinstance(func, ast.Name) and func.id in GENERATOR_FUNCTIONS and module == DEFINING_MODULE:
        return func.id
    return None


def _caller_sites() -> set[tuple[str, str]]:
    sites = set()
    for module, tree in _python_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (name := _calls_the_generator(module, node)):
                sites.add((module, name))
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("specimen_generator"):
                sites.update((module, alias.name) for alias in node.names if alias.name in GENERATOR_FUNCTIONS)
    return sites


def _variable_spellers() -> set[str]:
    """The scanned modules whose string constants spell the generator variable's value."""
    return {
        module
        for module, tree in _python_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _GENERATOR.VARIABLE in node.value
    }


def _keep_stderr_callers() -> set[str]:
    """The scanned modules that pass ``keep_stderr=True`` to a call."""
    return {
        module
        for module, tree in _python_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            keyword.arg == "keep_stderr" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            for keyword in node.keywords
        )
    }


class TestTheCallSites:
    def test_the_generator_is_started_from_the_named_sites_alone(self):
        assert _caller_sites() == EXPECTED_CALLERS

    def test_the_variables_value_is_spelled_in_the_defining_module_and_dev_py_alone(self):
        spellers = _variable_spellers()
        assert DEFINING_MODULE in spellers
        assert spellers <= VARIABLE_SPELLERS, sorted(spellers - VARIABLE_SPELLERS)

    def test_keep_stderr_true_is_passed_by_the_pin_script_alone(self):
        callers = _keep_stderr_callers()
        assert THIS_MODULE in callers, "the scan is blind: this module's own keep_stderr=True went unseen"
        assert callers <= KEEP_STDERR_CALLERS, sorted(callers - KEEP_STDERR_CALLERS)

    def test_the_scan_sees_a_planted_caller(self):
        planted = ast.parse(
            "import x\nspecimen_generator.run(cmd, p, o)\n_GENERATOR.configured({}, r)\nsubprocess.run(a)\n"
        )
        found = {
            name
            for node in ast.walk(planted)
            if isinstance(node, ast.Call) and (name := _calls_the_generator("m.py", node))
        }
        assert found == {"run", "configured"}

    def test_the_scan_reads_every_tree(self):
        modules = [module for module, _ in _python_modules()]
        unread = [tree for tree in SCANNED_TREES if not any(module.startswith(tree + "/") for module in modules)]
        assert unread == []
        assert DEFINING_MODULE in modules and THIS_MODULE in modules
