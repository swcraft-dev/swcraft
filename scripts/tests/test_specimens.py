"""The specimens' gate: the committed passports, the pin file, the cache and the pin script (fast).

Git holds the source passports and the pin file, never a generated model; each specimen is generated
on demand by ``.venv``'s ``packages/specimens/`` install into the gitignored cache, keyed by the
generator's source digest, and held to its digest every time it is taken. Held here:

- **the pins** -- they name exactly the committed passports, each ``passport_sha256`` holds, no two
  passports share a cache key, each passport validates at the running contract's major;
- **the contract major** -- the generator's packaged passport schema states the running contract's
  major, so a change of the major moves the generator and the passports in the same commit;
- **the taken specimens** -- each meets its ``model_sha256`` and its passport on every stated leaf;
  paths are within :data:`PATH_BOUND`, distinct under case folding, and within the absolute budget;
- **``take``'s protocol** on a stub generator (never the real cache): cold, warm, tampered, manifest
  rewritten, mismatch, a failed regeneration remembered, a changed source, another interpreter refused,
  the path budget, the rename race, a Windows ``PermissionError``, the sweep, the lock;
- **``scripts/specimens.py pin``** on the stub over ``examples/basic``'s model: a clean run, a new
  passport and a stale pin, a leaf mismatch, a failing passport pass, rejections, a defect, a pin file
  holding a migration, an unconfigured or uninstalled generator, another interpreter, ``--check``.

What this does not reach: the leaves a passport leaves unstated; a model property no passport leaf
carries (data accesses among them, held by the scenarios that read them); a generator whose output
differs only on a platform no runner covers.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
import tools_conftest_bridge
from conftest import REPO_ROOT, pinned_specimen_names, specimen_generator_module, specimens_module, take_specimen
from jsonschema import Draft202012Validator
from passport_support import first_schema_error, leaves_differing, schema_leaf_paths
from stub_generator import stub_command, write_stub
from swcraft_tools.model_registry import current_registry
from swcraft_tools.passport import compute_passport, schema_text

_GENERATOR = specimen_generator_module()
_SPECIMENS = specimens_module()

#: The ``tools`` conftest, which looks ``take_specimen`` up in its own globals: a patch on this root's
#: re-export would reach nothing.
_WARMING = tools_conftest_bridge.load_tools_conftest()

SPECIMENS_DIR = REPO_ROOT / _GENERATOR.SPECIMENS_DIR
PINS = json.loads((REPO_ROOT / _GENERATOR.PIN_FILE).read_text(encoding="utf-8"))
PASSPORT_SUFFIX = _GENERATOR.PASSPORT_SUFFIX
NAMES = pinned_specimen_names()


def _write_source(root: Path, text: str = "") -> None:
    """A fake repository's generator source: one module under ``SOURCE_DIR``, the cache key's input."""
    package = root / _GENERATOR.SOURCE_DIR
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f'"""A stand-in generator."""\n{text}', encoding="utf-8", newline="\n")


def _committed_passport_names() -> list[str]:
    return sorted(path.name[: -len(PASSPORT_SUFFIX)] for path in SPECIMENS_DIR.glob(f"*{PASSPORT_SUFFIX}"))


def duplicate_cache_keys(pins: dict) -> list[str]:
    """The specimens whose ``passport_sha256`` (or its 16-hex cache key) another specimen shares."""
    seen: dict[str, str] = {}
    duplicates = []
    for name, entry in sorted(pins["specimens"].items()):
        key = entry["passport_sha256"][: _GENERATOR.KEY_LENGTH]
        if key in seen:
            duplicates.append(f"{seen[key]} and {name}")
        seen[key] = name
    return duplicates


# -- the pins ---------------------------------------------------------------------------------------


class TestThePins:
    def test_the_pin_file_names_exactly_the_committed_passports(self) -> None:
        assert sorted(PINS["specimens"]) == _committed_passport_names()
        assert len(NAMES) == 18

    @pytest.mark.parametrize("name", NAMES)
    def test_each_passport_digest_holds(self, name: str) -> None:
        passport = _GENERATOR.passport_path(REPO_ROOT, name)
        assert _GENERATOR.file_sha256(passport) == PINS["specimens"][name]["passport_sha256"]

    def test_no_two_passports_share_a_cache_key(self) -> None:
        assert duplicate_cache_keys(PINS) == []

    def test_two_equal_passport_digests_are_refused(self) -> None:
        twin = {"passport_sha256": "ab" * 32, "model_sha256": "cd" * 32}
        assert duplicate_cache_keys({"specimens": {"one": twin, "two": dict(twin)}}) == ["one and two"]

    def test_the_pin_file_carries_no_migration_window(self) -> None:
        assert "migration" not in PINS
        assert not hasattr(_GENERATOR, "MIGRATIONS") and not hasattr(_GENERATOR, "unheld_migration")

    @pytest.mark.parametrize("name", NAMES)
    def test_each_passport_validates_at_the_running_contracts_major(self, name: str) -> None:
        passport = json.loads(_GENERATOR.passport_path(REPO_ROOT, name).read_text(encoding="utf-8"))
        error = first_schema_error(Draft202012Validator(json.loads(schema_text())), passport)
        assert error is None, f"{name}: {error.message if error else ''}"
        running = current_registry().contract_version
        assert passport["contract_version"].split(".")[0] == running.split(".")[0]

    @pytest.mark.parametrize("name", NAMES)
    def test_each_passport_is_canonical_json(self, name: str) -> None:
        text = _GENERATOR.passport_path(REPO_ROOT, name).read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def test_the_leaf_comparison_is_the_pin_scripts_one(self) -> None:
        assert leaves_differing is _SPECIMENS.leaves_differing and schema_leaf_paths is _SPECIMENS.schema_leaf_paths
        assert "contract_version" in schema_leaf_paths() and "layers.swc" in schema_leaf_paths()


# -- the contract major the generator's source serves -----------------------------------------------


def _running_major() -> int:
    return int(current_registry().contract_version.split(".")[0])


def test_the_generators_packaged_passport_schema_states_the_running_contract_major() -> None:
    """The generator's packaged passport schema states the running contract's major.

    The generator keeps its own copy of the passport schema for the oracle's independence, so moving
    the major also moves that copy and the passports.
    """
    schemas = sorted((REPO_ROOT / _GENERATOR.SOURCE_DIR / "passport_schema").glob("*.json"), key=lambda p: p.as_posix())
    majors = {json.loads(path.read_text(encoding="utf-8")).get("x-contract-major") for path in schemas}
    assert schemas and majors == {_running_major()}, majors


# -- the taken specimens ----------------------------------------------------------------------------


class TestTheTakenSpecimens:
    @pytest.mark.parametrize("name", NAMES)
    def test_each_specimen_meets_its_pins_and_its_passport(self, name: str) -> None:
        key = take_specimen(name)
        assert _GENERATOR.digest(key / "model") == PINS["specimens"][name]["model_sha256"]
        assert (key / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text(name)
        stated = json.loads(_GENERATOR.passport_path(REPO_ROOT, name).read_text(encoding="utf-8"))
        assert leaves_differing(stated, compute_passport(key / "model").passport) == []

    @pytest.mark.parametrize("name", NAMES)
    def test_each_specimens_paths_are_within_the_bounds(self, name: str) -> None:
        key = take_specimen(name)
        relative = [path.relative_to(key).as_posix() for path in key.rglob("*")]
        longest = max(relative, key=len)
        assert len(longest) <= _GENERATOR.PATH_BOUND, longest
        assert len(os.fspath(key)) + 1 + len(longest) <= _GENERATOR.ABSOLUTE_BUDGET
        folded: dict[str, str] = {}
        for path in relative:
            assert path.casefold() not in folded, f"{path} and {folded[path.casefold()]} merge under case folding"
            folded[path.casefold()] = path

    def test_the_checkout_is_within_the_cache_budget(self) -> None:
        cache = Path(os.path.abspath(REPO_ROOT / _GENERATOR.SPECIMEN_CACHE))
        assert len(os.fspath(cache)) <= _GENERATOR.cache_root_budget()
        assert _GENERATOR.cache_root_budget() == 109
        assert _GENERATOR.cache_root_budget() - len("/" + _GENERATOR.SPECIMEN_CACHE.as_posix()) == 92


class TestTheWarming:
    """``conftest.warm_specimens``, both trees' controller warming: any failure is left to the first test."""

    @staticmethod
    def _session(**config: object) -> object:
        return types.SimpleNamespace(config=types.SimpleNamespace(**config))

    @pytest.mark.parametrize(
        "error",
        [OSError("a disk error"), PermissionError("a held handle"), ValueError("a pin file")],
        ids=["oserror", "permission", "valueerror"],
    )
    def test_an_unexpected_failure_is_left_to_the_first_test(self, monkeypatch, error) -> None:
        taken = []

        def failing(name: str) -> Path:
            taken.append(name)
            raise error

        monkeypatch.setattr(_WARMING, "take_specimen", failing)
        _WARMING.warm_specimens(self._session(), ["one", "two"])
        assert taken == ["one", "two"]

    def test_no_generator_stops_the_warming_and_a_worker_warms_nothing(self, monkeypatch) -> None:
        taken = []

        def unconfigured(name: str) -> Path:
            taken.append(name)
            raise _GENERATOR.GeneratorNotConfigured("unset", "no generator")

        monkeypatch.setattr(_WARMING, "take_specimen", unconfigured)
        _WARMING.warm_specimens(self._session(), ["one", "two"])
        _WARMING.warm_specimens(self._session(workerinput={}), ["three"])
        assert taken == ["one"]


# -- take on a stub ---------------------------------------------------------------------------------


@pytest.fixture
def short_cache() -> Iterator[Path]:
    """A cache root in the system's temporary directory: short enough for the path budget everywhere."""
    root = Path(tempfile.mkdtemp(prefix="sc-"))
    _GENERATOR.forget_taken()
    yield root / "cache"
    _GENERATOR.forget_taken()
    shutil.rmtree(root, ignore_errors=True)


def _source_project(root: Path, *, extra: str = "") -> Path:
    """What the stub copies to ``--out``: a generator's manifest, a sidecar and a small ``model/``."""
    (root / "model" / "interfaces").mkdir(parents=True)
    (root / "model" / "tasks").mkdir(parents=True)
    (root / "model" / "tasks" / ".gitkeep").write_bytes(b"")
    (root / "model" / "interfaces" / "If.yaml").write_text(f"name: If\n{extra}", encoding="utf-8", newline="\n")
    (root / "swcraft.yaml").write_text('toolchain_version: "0.19.2"\n', encoding="utf-8", newline="\n")
    (root / "generation.json").write_text("{}\n", encoding="utf-8", newline="\n")
    return root


class _Repo:
    """A fake repository: the pin file's generator block and one passport, ``demo``, pinned."""

    def __init__(self, root: Path, *, model_sha256: str) -> None:
        self.root = root
        directory = root / _GENERATOR.SPECIMENS_DIR
        directory.mkdir(parents=True)
        _write_source(root)
        self.passport = directory / "demo.passport.json"
        self.passport.write_text('{"contract_version": "12.0.0"}\n', encoding="utf-8", newline="\n")
        self.pins = {
            "generator": PINS["generator"],
            "specimens": {
                "demo": {"passport_sha256": _GENERATOR.file_sha256(self.passport), "model_sha256": model_sha256}
            },
        }
        self.write()

    def write(self) -> None:
        text = json.dumps(self.pins, indent=2, sort_keys=True) + "\n"
        (self.root / _GENERATOR.PIN_FILE).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    return write_stub(tmp_path / "stub")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return _source_project(tmp_path / "source")


@pytest.fixture
def repo(tmp_path: Path, source: Path) -> _Repo:
    return _Repo(tmp_path / "repo", model_sha256=_GENERATOR.digest(source / "model"))


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.jsonl"
    monkeypatch.setenv("STUB_RECORD", os.fspath(path))
    return path


def _generations(record: Path) -> int:
    return len(record.read_text(encoding="utf-8").splitlines()) if record.is_file() else 0


def _source_key(repo: _Repo) -> str:
    """The fake repository's source key: the first part of every cache key under the cache root."""
    return _GENERATOR.source_digest(repo.root)[: _GENERATOR.SOURCE_KEY_LENGTH]


def _take(repo: _Repo, cache: Path, command: list[str]) -> Path:
    return _GENERATOR.take("demo", repo_root=repo.root, cache_root=cache, command=command)


class TestTake:
    def test_a_cold_cache_generates_and_publishes_the_key(self, repo, stub, source, short_cache, record) -> None:
        key = _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert key == short_cache / _source_key(repo) / repo.pins["specimens"]["demo"]["passport_sha256"][:16]
        assert sorted(entry.name for entry in key.iterdir()) == ["model", "swcraft.yaml"]
        assert (key / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text("demo")
        assert _GENERATOR.digest(key / "model") == repo.pins["specimens"]["demo"]["model_sha256"]
        assert _generations(record) == 1
        assert [entry.name for entry in key.parent.iterdir() if entry.name != key.name] == []

    def test_a_warm_cache_only_digests(self, repo, stub, source, short_cache, record) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        _take(repo, short_cache, command)
        _GENERATOR.forget_taken()
        _take(repo, short_cache, command)
        assert _generations(record) == 1

    def test_a_tampered_tree_is_regenerated(self, repo, stub, source, short_cache, record) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        key = _take(repo, short_cache, command)
        (key / "model" / "interfaces" / "If.yaml").write_text("name: Tampered\n", encoding="utf-8", newline="\n")
        _GENERATOR.forget_taken()
        assert _take(repo, short_cache, command) == key
        assert _generations(record) == 2
        assert _GENERATOR.digest(key / "model") == repo.pins["specimens"]["demo"]["model_sha256"]
        assert not any(entry.name.startswith((".t", ".r")) for entry in key.parent.iterdir())

    def test_a_rewritten_manifest_is_restored_without_a_generation(
        self, repo, stub, source, short_cache, record
    ) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        key = _take(repo, short_cache, command)
        (key / "swcraft.yaml").write_text('toolchain_version: "0.1.0"\n', encoding="utf-8", newline="\n")
        _GENERATOR.forget_taken()
        _take(repo, short_cache, command)
        assert (key / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text("demo")
        assert _generations(record) == 1

    @pytest.mark.parametrize("planted", [b"\xff\xfe", b"\xff\xfe" + b"toolchain_version: x\n"])
    def test_a_manifest_that_does_not_decode_is_restored_without_a_generation(
        self, repo, stub, source, short_cache, record, planted
    ) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        key = _take(repo, short_cache, command)
        (key / "swcraft.yaml").write_bytes(planted)
        _GENERATOR.forget_taken()
        assert _take(repo, short_cache, command) == key
        assert (key / "swcraft.yaml").read_bytes() == _GENERATOR.manifest_text("demo").encode("utf-8")
        assert _generations(record) == 1

    def test_a_pin_file_that_does_not_decode_is_a_refusal_with_a_sentence(self, repo, stub, source, short_cache):
        (repo.root / _GENERATOR.PIN_FILE).write_bytes(b"\xff\xfe")
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert caught.value.reason == "pin-file"
        assert f"{_GENERATOR.PIN_FILE.as_posix()} is not UTF-8 JSON" in str(caught.value)

    def test_a_mismatch_after_a_fresh_generation_names_both_digests_and_the_source(
        self, tmp_path, stub, short_cache
    ) -> None:
        other = _source_project(tmp_path / "other", extra="moved: true\n")
        repo = _Repo(tmp_path / "repo", model_sha256="0" * 64)
        with pytest.raises(_GENERATOR.SpecimenDigestMismatch) as caught:
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(other)))
        error = caught.value
        assert (error.expected, error.actual) == ("0" * 64, _GENERATOR.digest(other / "model"))
        assert error.expected in str(error) and error.actual in str(error)
        assert "the command given" in str(error) and _source_key(repo) in str(error)
        source_dir = short_cache / _source_key(repo)
        assert [entry.name for entry in source_dir.iterdir() if not entry.name.endswith(".lock")] == []

    def test_a_mismatch_from_the_venv_install_says_the_output_moved_and_how_to_re_pin(self) -> None:
        """A mismatch from the venv's install says the output moved and how to re-pin.

        A moved specimen is an intended generator change, re-pinned in the same commit, or a defect; never
        a re-pin inside a test, never a new published version.
        """
        message = str(
            _GENERATOR.SpecimenDigestMismatch(
                "demo",
                expected="0" * 64,
                actual="1" * 64,
                source_key="ab" * 8,
                source=_GENERATOR._generator_source(None, {}),
            )
        )
        assert message.startswith("The specimen generator (the .venv install, source abababababababab) generated")
        assert "0" * 64 in message and "1" * 64 in message
        assert "`scripts/specimens.py pin demo` in the same commit" in message
        assert message.endswith("Never answered by a re-pin of the specimen inside a test.")
        assert "publish" not in message and "runtime-pins" not in message

    def test_a_mismatch_from_a_configured_generator_names_it_and_not_the_venv_install(
        self, tmp_path, stub, short_cache
    ) -> None:
        """A mismatch from a configured generator names it and asks for a run with ``.venv``'s first.

        A generator given by a command or the variable is not known to be this checkout's.
        """
        other = _source_project(tmp_path / "other", extra="moved: true\n")
        repo = _Repo(tmp_path / "repo", model_sha256="0" * 64)
        with pytest.raises(_GENERATOR.SpecimenDigestMismatch) as caught:
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(other)))
        message = str(caught.value)
        assert message.startswith("The specimen generator configured by the command given generated the specimen demo")
        assert "its output moved" not in message
        assert f"not known to be the .venv install of this checkout's source {_source_key(repo)}" in message
        assert "0" * 64 in message and caught.value.actual in message

    def test_a_failed_regeneration_is_raised_again_without_generating_again(
        self, tmp_path, stub, short_cache, record
    ) -> None:
        other = _source_project(tmp_path / "other", extra="moved: true\n")
        repo = _Repo(tmp_path / "repo", model_sha256="0" * 64)
        command = stub_command(stub, "copy", os.fspath(other))
        with pytest.raises(_GENERATOR.SpecimenDigestMismatch) as first:
            _take(repo, short_cache, command)
        with pytest.raises(_GENERATOR.SpecimenDigestMismatch) as again:
            _take(repo, short_cache, command)
        assert again.value is first.value and _generations(record) == 1
        _GENERATOR.forget_taken()
        with pytest.raises(_GENERATOR.SpecimenDigestMismatch):
            _take(repo, short_cache, command)
        assert _generations(record) == 2

    def test_a_rejection_is_raised_again_without_generating_again(self, repo, stub, short_cache, record) -> None:
        command = stub_command(stub, "reject", "1", "zq")
        for _ in range(2):
            with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
                _take(repo, short_cache, command)
            assert caught.value.reason == "rejected"
        assert _generations(record) == 1

    def test_the_generators_source_is_named_by_how_it_was_configured(self) -> None:
        assert _GENERATOR._generator_source(["x"], {}) == "the command given"
        assert _GENERATOR._generator_source(None, {_GENERATOR.VARIABLE: "/g"}) == f"the variable {_GENERATOR.VARIABLE}"
        assert _GENERATOR._generator_source(None, {}) == "the .venv install"

    def test_a_changed_generator_source_is_a_new_key_and_a_fresh_generation(
        self, repo, stub, source, short_cache, record
    ) -> None:
        """A changed generator source is a new cache key and a fresh generation.

        The cache follows the source, not a version: a change of the generator's code under an unchanged
        version misses the cache, and the old key is left beside the new one.
        """
        command = stub_command(stub, "copy", os.fspath(source))
        first = _take(repo, short_cache, command)
        _write_source(repo.root, "CHANGED = True\n")
        _GENERATOR.forget_taken()
        second = _take(repo, short_cache, command)
        assert second.name == first.name and second.parent != first.parent
        assert second.parent.name == _source_key(repo) and first.is_dir()
        assert _generations(record) == 2

    def test_a_take_without_a_generator_source_is_refused_before_generating(
        self, repo, stub, source, short_cache, record
    ) -> None:
        shutil.rmtree(repo.root / _GENERATOR.SOURCE_DIR)
        _GENERATOR.forget_taken()
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert caught.value.reason == "generator-source"
        assert _generations(record) == 0 and not short_cache.exists()

    @pytest.mark.parametrize("config", ["version_info = 3.12.4\n", None], ids=["python-3.12", "no-pyvenv-cfg"])
    def test_a_configured_generator_on_another_interpreter_is_refused_before_generating(
        self, repo, short_cache, record, tmp_path, monkeypatch, config
    ) -> None:
        """A configured generator on another interpreter than CPython 3.11 is refused, naming the pinned one.

        The specimens are pinned on CPython 3.11; nothing is started or published.
        """
        venv = tmp_path / "other-venv"
        generator = _GENERATOR.venv_python(venv).parent / "swcraft-specimens"
        generator.parent.mkdir(parents=True)
        generator.write_bytes(b"")
        if config is not None:
            (venv / "pyvenv.cfg").write_text(config, encoding="utf-8", newline="\n")
        monkeypatch.setenv(_GENERATOR.VARIABLE, os.fspath(generator))
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _GENERATOR.take("demo", repo_root=repo.root, cache_root=short_cache)
        assert caught.value.reason == "interpreter"
        assert "pinned on Python 3.11 alone" in str(caught.value) and "demo" in str(caught.value)
        assert _generations(record) == 0
        source_dir = short_cache / _source_key(repo)
        assert [entry.name for entry in source_dir.iterdir() if not entry.name.endswith(".lock")] == []

    def test_an_unpinned_name_and_an_edited_passport_are_refused(self, repo, stub, source, short_cache) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as unpinned:
            _GENERATOR.take("absent", repo_root=repo.root, cache_root=short_cache, command=command)
        assert unpinned.value.reason == "not-pinned"
        repo.passport.write_text('{"contract_version": "12.0.1"}\n', encoding="utf-8", newline="\n")
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as edited:
            _take(repo, short_cache, command)
        assert edited.value.reason == "passport-changed" and "specimens.py pin demo" in str(edited.value)

    def test_a_cache_root_past_the_budget_is_refused_before_generating(
        self, repo, stub, source, short_cache, record
    ) -> None:
        deep = short_cache / ("d" * (_GENERATOR.cache_root_budget() + 1 - len(os.fspath(short_cache)) - 1))
        assert len(os.fspath(deep)) == _GENERATOR.cache_root_budget() + 1
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _take(repo, deep, stub_command(stub, "copy", os.fspath(source)))
        assert caught.value.reason == "path-budget"
        assert str(_GENERATOR.cache_root_budget() + 1) in str(caught.value)
        assert _generations(record) == 0 and not deep.exists()

    def test_a_rejection_and_a_defect_are_refusals_publishing_nothing(self, repo, stub, short_cache) -> None:
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as rejected:
            _take(repo, short_cache, stub_command(stub, "reject", "3", "zq-sentinel"))
        assert rejected.value.reason == "rejected" and "zq-sentinel" not in str(rejected.value)
        _GENERATOR.forget_taken()
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as defect:
            _take(repo, short_cache, stub_command(stub, "exit", "1"))
        assert defect.value.reason == "defect" and "exit 1" in str(defect.value)
        source_dir = short_cache / _source_key(repo)
        assert [entry.name for entry in source_dir.iterdir() if not entry.name.endswith(".lock")] == []

    @pytest.mark.parametrize("reason", ["unset", "relative", "missing"])
    def test_a_cold_take_with_no_generator_is_refused_publishing_nothing(
        self, repo, short_cache, monkeypatch, tmp_path, reason
    ) -> None:
        """No command and no generator: the take names the variable; nothing is published or left locked."""
        values = {"relative": "zqsentinel/specimen-generator", "missing": os.fspath(tmp_path / "no generator here")}
        if reason == "unset":
            monkeypatch.delenv(_GENERATOR.VARIABLE, raising=False)
        else:
            monkeypatch.setenv(_GENERATOR.VARIABLE, values[reason])
        with pytest.raises(_GENERATOR.GeneratorNotConfigured) as caught:
            _GENERATOR.take("demo", repo_root=repo.root, cache_root=short_cache)
        assert caught.value.reason == reason and _GENERATOR.VARIABLE in str(caught.value)
        source_dir = short_cache / _source_key(repo)
        assert not source_dir.exists() or [entry.name for entry in source_dir.iterdir()] == []

    def test_a_lost_rename_race_takes_the_winners_tree(
        self, repo, stub, source, short_cache, record, monkeypatch
    ) -> None:
        real_rename = os.rename

        planted = []

        def racing_rename(src, dst):
            target = Path(dst)
            if Path(src).name.startswith(".t") and not target.exists():
                winner = target.parent / ".winner"
                shutil.copytree(source / "model", winner / "model")
                (winner / "swcraft.yaml").write_text(_GENERATOR.manifest_text("demo"), encoding="utf-8", newline="\n")
                (winner / "winner-marker").write_bytes(b"")
                real_rename(winner, target)
                planted.append(target)
            return real_rename(src, dst)

        monkeypatch.setattr(_GENERATOR.os, "rename", racing_rename)
        key = _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert planted == [key] and (key / "winner-marker").is_file()
        assert _GENERATOR.digest(key / "model") == repo.pins["specimens"]["demo"]["model_sha256"]
        assert not any(entry.name.startswith((".t", ".r")) for entry in key.parent.iterdir())

    def test_a_failed_publication_puts_the_old_key_back(self, repo, stub, source, short_cache, monkeypatch) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        key = _take(repo, short_cache, command)
        (key / "model" / "interfaces" / "If.yaml").write_text("name: Tampered\n", encoding="utf-8", newline="\n")
        _GENERATOR.forget_taken()
        real_rename = os.rename

        def refusing_publication(src, dst):
            if Path(src).name.startswith(".t"):
                raise PermissionError("a scanner holds a handle")
            return real_rename(src, dst)

        monkeypatch.setattr(_GENERATOR, "RENAME_PAUSE_SECONDS", 0.0)
        monkeypatch.setattr(_GENERATOR.os, "rename", refusing_publication)
        with pytest.raises(PermissionError):
            _take(repo, short_cache, command)
        assert (key / "model" / "interfaces" / "If.yaml").read_text(encoding="utf-8") == "name: Tampered\n"
        assert not any(entry.name.startswith((".t", ".r")) for entry in key.parent.iterdir())

    def test_a_permission_error_on_a_rename_is_retried_then_raised(
        self, repo, stub, source, short_cache, monkeypatch
    ) -> None:
        calls = []

        def refusing_rename(src, dst):
            calls.append(src)
            raise PermissionError("a scanner holds a handle")

        monkeypatch.setattr(_GENERATOR, "RENAME_PAUSE_SECONDS", 0.0)
        monkeypatch.setattr(_GENERATOR.os, "rename", refusing_rename)
        with pytest.raises(PermissionError):
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert len(calls) == _GENERATOR.RENAME_ATTEMPTS == 5

    def test_a_permission_error_that_clears_is_retried_to_success(
        self, repo, stub, source, short_cache, monkeypatch
    ) -> None:
        real_rename = os.rename
        refusals = [PermissionError("held"), PermissionError("held")]

        def flaky_rename(src, dst):
            if refusals:
                raise refusals.pop()
            return real_rename(src, dst)

        monkeypatch.setattr(_GENERATOR, "RENAME_PAUSE_SECONDS", 0.0)
        monkeypatch.setattr(_GENERATOR.os, "rename", flaky_rename)
        key = _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert refusals == [] and (key / "model").is_dir()

    def test_old_transient_and_retired_siblings_are_swept_young_ones_kept(
        self, repo, stub, source, short_cache
    ) -> None:
        source_dir = short_cache / _source_key(repo)
        old_fresh, old_retired, young = (
            source_dir / ".t0000dead",
            source_dir / ".r0000dead",
            source_dir / ".t0000live",
        )
        for directory in (old_fresh, old_retired, young):
            directory.mkdir(parents=True)
        an_hour_ago = time.time() - _GENERATOR.SWEEP_AGE_SECONDS - 60
        old_record, young_record = source_dir / ".l0000dead", source_dir / ".l0000live"
        for record in (old_record, young_record):
            record.write_text("1 1.0 x\n", encoding="utf-8", newline="\n")
        for entry in (old_fresh, old_retired, old_record):
            os.utime(entry, (an_hour_ago, an_hour_ago))
        _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert not old_fresh.exists() and not old_retired.exists() and young.exists()
        assert not old_record.exists() and young_record.exists()

    def test_a_held_lock_is_waited_for_and_the_holders_tree_only_digested(
        self, repo, stub, source, short_cache, record
    ) -> None:
        source_dir = short_cache / _source_key(repo)
        source_dir.mkdir(parents=True)
        key = source_dir / repo.pins["specimens"]["demo"]["passport_sha256"][:16]
        lock = source_dir / f"{key.name}.lock"
        lock.write_text(f"{os.getpid()} {time.time()}\n", encoding="utf-8", newline="\n")

        def holder() -> None:
            time.sleep(0.5)
            shutil.copytree(source / "model", key / "model")
            (key / "swcraft.yaml").write_text(_GENERATOR.manifest_text("demo"), encoding="utf-8", newline="\n")
            lock.unlink()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source))) == key
        finally:
            thread.join()
        assert _generations(record) == 0

    def test_a_stale_lock_is_removed(self, repo, stub, source, short_cache, record) -> None:
        source_dir = short_cache / _source_key(repo)
        source_dir.mkdir(parents=True)
        key_name = repo.pins["specimens"]["demo"]["passport_sha256"][:16]
        stale = time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60
        (source_dir / f"{key_name}.lock").write_text(f"99999 {stale}\n", encoding="utf-8", newline="\n")
        key = _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert (key / "model").is_dir() and _generations(record) == 1
        assert not (source_dir / f"{key_name}.lock").exists()

    def test_a_lock_held_past_the_ceiling_is_a_refusal_naming_the_key(
        self, repo, stub, source, short_cache, monkeypatch
    ) -> None:
        source_dir = short_cache / _source_key(repo)
        source_dir.mkdir(parents=True)
        key_name = repo.pins["specimens"]["demo"]["passport_sha256"][:16]
        (source_dir / f"{key_name}.lock").write_text(f"1 {time.time()}\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(_GENERATOR, "LOCK_WAIT_SECONDS", 0.5)
        with pytest.raises(_GENERATOR.SpecimenUnavailable) as caught:
            _take(repo, short_cache, stub_command(stub, "copy", os.fspath(source)))
        assert caught.value.reason == "lock-timeout" and key_name in str(caught.value)

    def test_the_lock_outlives_the_generation_and_a_waiter_outlives_the_lock(self) -> None:
        assert _GENERATOR.LOCK_STALE_SECONDS > _GENERATOR.TIMEOUT_SECONDS
        assert _GENERATOR.LOCK_WAIT_SECONDS > _GENERATOR.LOCK_STALE_SECONDS

    def test_two_waiters_on_one_stale_lock_break_it_once(self, tmp_path) -> None:
        """The second waiter on one stale lock finds the replacement and never removes it.

        It observed the stale lock before the first waiter broke and took it.
        """
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        observed_by_second = lock.read_bytes()
        assert _GENERATOR._break_stale(lock, lock.read_bytes()) and not lock.exists()
        with _GENERATOR._KeyLock(lock) as first:
            assert _GENERATOR._break_stale(lock, observed_by_second)
            assert lock.read_text(encoding="utf-8") == first.line
        assert not lock.exists()
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_an_empty_stale_lock_broken_by_two_waiters_leaves_the_owners_replacement(self, tmp_path) -> None:
        """An empty stale lock broken by two waiters leaves the owner's replacement.

        Both waiters observed the empty lock; the first breaks it and takes the key: the second finds a
        replacement that already holds its owner's record, so the owner's lock survives.
        """
        lock = tmp_path / "key.lock"
        lock.write_bytes(b"")
        stale = time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60
        os.utime(lock, (stale, stale))
        first_saw, second_saw = lock.read_bytes(), lock.read_bytes()
        assert first_saw == second_saw == b""
        assert _GENERATOR._lock_age(lock, first_saw) > _GENERATOR.LOCK_STALE_SECONDS
        assert _GENERATOR._break_stale(lock, first_saw)
        with _GENERATOR._KeyLock(lock) as owner:
            assert _GENERATOR._break_stale(lock, second_saw)
            assert lock.read_text(encoding="utf-8") == owner.line
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_lock_holds_its_record_the_instant_it_exists(self, tmp_path, monkeypatch) -> None:
        lock = tmp_path / "key.lock"
        seen: list[bytes] = []
        real_link = os.link

        def link(source, target, *args, **kwargs):
            real_link(source, target, *args, **kwargs)
            seen.append(Path(target).read_bytes())

        monkeypatch.setattr(_GENERATOR.os, "link", link)
        with _GENERATOR._KeyLock(lock) as owner:
            assert owner.line and seen == [owner.line.encode("utf-8")]
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_waiter_that_loses_the_breaker_leaves_the_lock(self, tmp_path) -> None:
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        _GENERATOR._breaker_path(lock, stale).write_bytes(b"")
        assert not _GENERATOR._break_stale(lock, stale)
        assert lock.read_bytes() == stale

    def test_a_breaker_left_by_a_killed_waiter_blocks_the_next_take_no_longer_than_its_grace(
        self, tmp_path, monkeypatch
    ) -> None:
        """A breaker left by a killed waiter is broken past its grace, and the next take holds the key.

        The waiter was killed between publishing the breaker and removing the stale lock: its breaker,
        past :data:`BREAKER_STALE_SECONDS`, is broken by the same rule.
        """
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        breaker = _GENERATOR._breaker_path(lock, stale)
        killed = f"99998 {time.time() - _GENERATOR.BREAKER_STALE_SECONDS - 5} killed\n"
        breaker.write_text(killed, encoding="utf-8", newline="\n")
        # A breaker still within its grace makes the take wait out LOCK_WAIT_SECONDS and raise
        # SpecimenUnavailable("lock-timeout"), so the take succeeding is the check: no clock.
        monkeypatch.setattr(_GENERATOR, "LOCK_WAIT_SECONDS", 2.0)
        with _GENERATOR._KeyLock(lock) as owner:
            assert lock.read_text(encoding="utf-8") == owner.line
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_breaker_within_its_grace_is_left_and_the_lock_with_it(self, tmp_path) -> None:
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        breaker = _GENERATOR._breaker_path(lock, stale)
        live = f"99998 {time.time() - _GENERATOR.BREAKER_STALE_SECONDS + 10} live\n"
        breaker.write_text(live, encoding="utf-8", newline="\n")
        assert not _GENERATOR._break_stale(lock, stale)
        assert lock.read_bytes() == stale and breaker.read_text(encoding="utf-8") == live

    def test_a_breaker_another_took_over_is_left_to_it(self, tmp_path, monkeypatch) -> None:
        """The step releases its breaker only while it holds the step's own record."""
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        breaker = _GENERATOR._breaker_path(lock, stale)
        real_read = Path.read_bytes

        def read_bytes(self: Path) -> bytes:
            if self == lock:
                breaker.write_text("4242 1.0 another\n", encoding="utf-8", newline="\n")
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        assert _GENERATOR._break_stale(lock, stale) and not lock.exists()
        assert breaker.read_text(encoding="utf-8") == "4242 1.0 another\n"

    def test_an_old_orphan_breaker_is_swept_a_young_one_kept(self, tmp_path) -> None:
        an_hour_ago = time.time() - _GENERATOR.SWEEP_AGE_SECONDS - 60
        old = tmp_path / "0123456789abcdef.lock.b0000000000000dead"
        young = tmp_path / "0123456789abcdef.lock.b0000000000000live"
        old.write_text(f"99998 {an_hour_ago} killed\n", encoding="utf-8", newline="\n")
        young.write_text(f"99997 {time.time()} live\n", encoding="utf-8", newline="\n")
        _GENERATOR._sweep(tmp_path)
        assert not old.exists() and young.exists()
        assert sorted(entry.name for entry in tmp_path.iterdir()) == [young.name]

    def test_waiters_racing_on_one_stale_lock_hold_it_one_at_a_time(self, tmp_path) -> None:
        lock = tmp_path / "key.lock"
        lock.write_text(f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60}\n", encoding="utf-8", newline="\n")
        waiters = 8
        barrier = threading.Barrier(waiters)
        guard = threading.Lock()
        held: list[int] = []
        peak: list[int] = [0]
        errors: list[BaseException] = []

        def waiter() -> None:
            barrier.wait()
            try:
                with _GENERATOR._KeyLock(lock):
                    with guard:
                        held.append(1)
                        peak[0] = max(peak[0], len(held))
                    time.sleep(0.05)
                    with guard:
                        held.pop()
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=waiter) for _ in range(waiters)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [] and peak[0] == 1
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_lock_broken_and_taken_by_another_is_left_to_it(self, tmp_path) -> None:
        lock = tmp_path / "key.lock"
        with _GENERATOR._KeyLock(lock):
            lock.write_text("4242 1.0 another\n", encoding="utf-8", newline="\n")
        assert lock.read_text(encoding="utf-8") == "4242 1.0 another\n"
        with _GENERATOR._KeyLock(tmp_path / "own.lock"):
            pass
        assert not (tmp_path / "own.lock").exists()

    @staticmethod
    def _refuse(monkeypatch, method: str, path: Path, times: int, *, meanwhile=None) -> list[int]:
        """Make ``Path.<method>`` on *path* raise ``PermissionError`` *times* times; return the call counter.

        The refusal stands for a Windows sharing violation; *meanwhile* runs before the last refusal.
        """
        real = getattr(Path, method)
        calls = [0]

        def refused(self: Path, *args, **kwargs):
            if self == path:
                calls[0] += 1
                if calls[0] <= times:
                    if calls[0] == times and meanwhile is not None:
                        meanwhile()
                    raise PermissionError(13, "The process cannot access the file", os.fspath(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, refused)
        monkeypatch.setattr(_GENERATOR, "RENAME_PAUSE_SECONDS", 0.01)
        return calls

    def test_a_release_refused_by_a_sharing_violation_is_retried(self, tmp_path, monkeypatch) -> None:
        lock = tmp_path / "key.lock"
        calls = self._refuse(monkeypatch, "unlink", lock, 2)
        with _GENERATOR._KeyLock(lock):
            pass
        assert not lock.exists() and calls[0] == 3
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_release_checks_the_record_again_before_each_retry(self, tmp_path, monkeypatch) -> None:
        """Between a refused unlink and its retry the lock was broken and taken by another: left to it."""
        lock = tmp_path / "key.lock"

        def taken_by_another() -> None:
            lock.write_text("4242 1.0 another\n", encoding="utf-8", newline="\n")

        self._refuse(monkeypatch, "unlink", lock, 1, meanwhile=taken_by_another)
        with _GENERATOR._KeyLock(lock):
            pass
        assert lock.read_text(encoding="utf-8") == "4242 1.0 another\n"

    def test_a_release_still_refused_raises_naming_the_lock_and_leaves_it(self, tmp_path, monkeypatch) -> None:
        lock = tmp_path / "key.lock"
        calls = self._refuse(monkeypatch, "unlink", lock, 1000)
        with pytest.raises(PermissionError, match=r"key\.lock could not be removed"):
            with _GENERATOR._KeyLock(lock) as owner:
                line = owner.line
        assert calls[0] == _GENERATOR.RENAME_ATTEMPTS
        assert lock.read_text(encoding="utf-8") == line

    def test_a_waiter_whose_read_is_refused_keeps_waiting(self, tmp_path, monkeypatch) -> None:
        """A waiter whose read is refused past every retry keeps waiting, and takes the lock once released.

        The holder removing its lock refuses the waiter's read: the waiter waits and looks again.
        """
        lock = tmp_path / "key.lock"
        lock.write_text(f"4242 {time.time()} holder\n", encoding="utf-8", newline="\n")
        refusals = _GENERATOR.RENAME_ATTEMPTS + 2
        self._refuse(monkeypatch, "read_bytes", lock, refusals, meanwhile=lock.unlink)
        monkeypatch.setattr(_GENERATOR, "LOCK_POLL_SECONDS", 0.01)
        monkeypatch.setattr(_GENERATOR, "LOCK_WAIT_SECONDS", 5.0)
        with _GENERATOR._KeyLock(lock) as owner:
            assert lock.read_text(encoding="utf-8") == owner.line
        assert not lock.exists()

    def test_a_stale_lock_whose_removal_is_refused_is_removed_on_a_retry(self, tmp_path, monkeypatch) -> None:
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        self._refuse(monkeypatch, "unlink", lock, 2)
        assert _GENERATOR._break_stale(lock, stale) and not lock.exists()
        assert [entry.name for entry in tmp_path.iterdir()] == []

    def test_a_stale_lock_whose_removal_stays_refused_is_not_counted_gone(self, tmp_path, monkeypatch) -> None:
        lock = tmp_path / "key.lock"
        stale = f"99999 {time.time() - _GENERATOR.LOCK_STALE_SECONDS - 60} dead\n".encode()
        lock.write_bytes(stale)
        self._refuse(monkeypatch, "unlink", lock, 1000)
        assert not _GENERATOR._break_stale(lock, stale)
        assert lock.read_bytes() == stale
        assert [entry.name for entry in tmp_path.iterdir()] == [lock.name]

    def test_two_takers_of_one_key_generate_it_once(self, repo, stub, source, short_cache, record) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        barrier = threading.Barrier(2)
        keys: list[Path] = []
        errors: list[BaseException] = []

        def taker() -> None:
            barrier.wait()
            try:
                keys.append(_take(repo, short_cache, command))
            except BaseException as error:  # noqa: BLE001 -- reported by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=taker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [] and len(set(keys)) == 1
        assert _generations(record) == 1

    def test_a_key_retired_mid_digest_is_invalid_and_taken_again_under_the_lock(
        self, repo, stub, source, short_cache, record, monkeypatch
    ) -> None:
        command = stub_command(stub, "copy", os.fspath(source))
        key = _take(repo, short_cache, command)
        _GENERATOR.forget_taken()
        real_rglob = Path.rglob
        retired: list[Path] = []

        def retiring_walk(self: Path, pattern: str, *args, **kwargs):
            yield from real_rglob(self, pattern, *args, **kwargs)
            if self == key / "model" and not retired:
                # Another caller's repair renames the stale key away after the walk listed its files
                # and before they are read.
                aside = key.parent / ".rretired"
                os.rename(key, aside)
                retired.append(aside)

        monkeypatch.setattr(Path, "rglob", retiring_walk)
        assert _take(repo, short_cache, command) == key
        assert retired and _generations(record) == 2
        assert _GENERATOR.digest(key / "model") == repo.pins["specimens"]["demo"]["model_sha256"]
        assert (key / "swcraft.yaml").read_text(encoding="utf-8") == _GENERATOR.manifest_text("demo")

    def test_a_vanished_or_unreadable_key_is_invalid_never_raised(self, tmp_path, monkeypatch) -> None:
        assert _GENERATOR._is_valid(tmp_path / "gone", "demo", "0" * 64) is False
        (tmp_path / "key" / "model").mkdir(parents=True)

        def refused(_model_root: Path) -> str:
            raise PermissionError("a scanner holds a handle")

        monkeypatch.setattr(_GENERATOR, "digest", refused)
        assert _GENERATOR._is_valid(tmp_path / "key", "demo", "0" * 64, repair=True) is False


# -- the pin script on a stub -----------------------------------------------------------------------

BASIC_MODEL = REPO_ROOT / "examples" / "basic" / "model"


@pytest.fixture
def basic_source(tmp_path: Path) -> Path:
    """What the stub copies: ``examples/basic``'s model, the model ``basic-twin``'s passport was computed from."""
    root = tmp_path / "basic-source"
    shutil.copytree(BASIC_MODEL, root / "model")
    (root / "generation.json").write_text("{}\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def pin_repo(tmp_path: Path, basic_source: Path) -> Path:
    """A fake repository pinning ``twin`` (``basic-twin``'s passport) at its true digests."""
    root = tmp_path / "pin-repo"
    directory = root / _GENERATOR.SPECIMENS_DIR
    directory.mkdir(parents=True)
    passport = directory / "twin.passport.json"
    shutil.copyfile(_GENERATOR.passport_path(REPO_ROOT, "basic-twin"), passport)
    pins = {
        "generator": PINS["generator"],
        "specimens": {
            "twin": {
                "passport_sha256": _GENERATOR.file_sha256(passport),
                "model_sha256": _GENERATOR.digest(basic_source / "model"),
            }
        },
    }
    text = json.dumps(pins, indent=2, sort_keys=True) + "\n"
    (root / _GENERATOR.PIN_FILE).write_text(text, encoding="utf-8", newline="\n")
    return root


def _pin(repo_root: Path, command: list[str] | None, **options) -> tuple[int, str]:
    """The exit code and both streams' lines, interleaved as printed."""
    stream = io.StringIO()
    code = _SPECIMENS.pin(
        options.pop("names", None), command=command, repo_root=repo_root, stream=stream, err=stream, **options
    )
    return code, stream.getvalue()


def _pin_text(repo_root: Path) -> bytes:
    return (repo_root / _GENERATOR.PIN_FILE).read_bytes()


def _add_passport(repo_root: Path, name: str) -> Path:
    """A second committed passport: ``twin``'s document, written in another layout (so another digest)."""
    document = json.loads(_GENERATOR.passport_path(repo_root, "twin").read_text(encoding="utf-8"))
    path = _GENERATOR.passport_path(repo_root, name)
    path.write_text(json.dumps(document, indent=4, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


class TestThePinScript:
    def test_a_clean_run_leaves_the_pins_unchanged(self, pin_repo, stub, basic_source) -> None:
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 0, output
        assert _pin_text(pin_repo) == before
        assert "twin: built and met" in output

    def test_a_pin_file_that_does_not_decode_exits_1_with_a_sentence(self, pin_repo, stub, basic_source) -> None:
        (pin_repo / _GENERATOR.PIN_FILE).write_bytes(b"\xff\xfe")
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 1
        assert f"pin: {_GENERATOR.PIN_FILE.as_posix()} is not UTF-8 JSON" in output

    def test_a_new_passport_is_pinned_and_a_stale_pin_pruned_by_a_full_run_alone(
        self, pin_repo, stub, basic_source
    ) -> None:
        second = _add_passport(pin_repo, "twin2")
        pins = json.loads(_pin_text(pin_repo))
        pins["specimens"]["gone"] = {"passport_sha256": "1" * 64, "model_sha256": "2" * 64}
        (pin_repo / _GENERATOR.PIN_FILE).write_text(json.dumps(pins), encoding="utf-8", newline="\n")
        command = stub_command(stub, "copy", os.fspath(basic_source))
        code, output = _pin(pin_repo, command, names=["twin"])
        assert code == 0, output
        named = json.loads(_pin_text(pin_repo))["specimens"]
        assert sorted(named) == ["gone", "twin"] and named["gone"] == pins["specimens"]["gone"]
        code, output = _pin(pin_repo, command)
        assert code == 0, output
        full = json.loads(_pin_text(pin_repo))["specimens"]
        assert sorted(full) == ["twin", "twin2"]
        assert full["twin2"] == {
            "passport_sha256": _GENERATOR.file_sha256(second),
            "model_sha256": _GENERATOR.digest(basic_source / "model"),
        }

    def test_a_moved_digest_is_rewritten(self, pin_repo, stub, basic_source) -> None:
        pins = json.loads(_pin_text(pin_repo))
        pins["specimens"]["twin"]["model_sha256"] = "0" * 64
        (pin_repo / _GENERATOR.PIN_FILE).write_text(json.dumps(pins), encoding="utf-8", newline="\n")
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 0, output
        written = json.loads(_pin_text(pin_repo))
        assert written["specimens"]["twin"]["model_sha256"] == _GENERATOR.digest(basic_source / "model")
        assert _pin_text(pin_repo).endswith(b"}\n")

    def test_a_leaf_mismatch_refuses_naming_the_leaf_and_both_values(self, pin_repo, stub, basic_source) -> None:
        passport = _GENERATOR.passport_path(pin_repo, "twin")
        document = json.loads(passport.read_text(encoding="utf-8"))
        stated = document["layers"]["swc"] + 1
        document["layers"]["swc"] = stated
        passport.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 1
        assert f"twin: layers.swc: the passport states {stated}, the generated model has {stated - 1}" in output
        assert _pin_text(pin_repo) == before

    def test_a_rejection_prints_the_generators_lines_continues_and_exits_2(self, pin_repo, stub) -> None:
        before = json.loads(_pin_text(pin_repo))
        code, output = _pin(pin_repo, stub_command(stub, "reject", "3", "zq-pin-sentinel"))
        assert code == 2
        assert output.count("zq-pin-sentinel") == 3
        assert "rejected by the generator: twin" in output
        assert json.loads(_pin_text(pin_repo)) == before

    def test_one_rejected_of_several_exits_2_and_the_others_are_pinned(self, pin_repo, stub, basic_source) -> None:
        _add_passport(pin_repo, "twin2")
        code, output = _pin(pin_repo, stub_command(stub, "copy-unless", "twin2", os.fspath(basic_source)))
        assert code == 2, output
        assert "rejected by the generator: twin2 (the others are pinned)" in output
        assert sorted(json.loads(_pin_text(pin_repo))["specimens"]) == ["twin"]

    def test_progress_goes_to_stdout_and_every_failure_to_stderr(self, pin_repo, stub, basic_source) -> None:
        _add_passport(pin_repo, "twin2")
        out, err = io.StringIO(), io.StringIO()
        command = stub_command(stub, "copy-unless", "twin2", os.fspath(basic_source))
        assert _SPECIMENS.pin(None, command=command, repo_root=pin_repo, stream=out, err=err) == 2
        assert out.getvalue().startswith("twin: built and met in ") and out.getvalue().count("\n") == 1
        assert "twin2: rejected by the generator" in err.getvalue()
        assert err.getvalue().endswith("pin: rejected by the generator: twin2 (the others are pinned)\n")
        assert "built and met" not in err.getvalue()

    def test_check_with_a_rejected_specimen_exits_2_and_writes_nothing(self, pin_repo, stub) -> None:
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "reject", "2", "zq-check"), check=True)
        assert code == 2 and "twin: rejected by the generator" in output
        assert _pin_text(pin_repo) == before

    def test_a_failing_passport_pass_refuses_the_specimen(self, pin_repo, stub, basic_source) -> None:
        broken = basic_source / "model" / "interfaces" / "Broken.yaml"
        broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 1, output
        assert "twin: SWCraft's passport of the generated model failed" in output and "refused twin" in output
        assert _pin_text(pin_repo) == before

    def test_a_pin_file_holding_a_migration_is_refused_before_generating(self, pin_repo, stub, basic_source) -> None:
        pins = json.loads(_pin_text(pin_repo))
        pins["migration"] = None
        (pin_repo / _GENERATOR.PIN_FILE).write_text(json.dumps(pins), encoding="utf-8", newline="\n")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)))
        assert code == 1 and "migration" in output and "no longer carries" in output and "built" not in output
        assert _pin_text(pin_repo) == before

    def test_a_defect_stops_with_exit_1(self, pin_repo, stub) -> None:
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "exit", "1"))
        assert code == 1 and "exit 1" in output
        assert _pin_text(pin_repo) == before

    def test_an_unconfigured_generator_writes_nothing(self, pin_repo) -> None:
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, None, environ={})
        assert code == 1 and _GENERATOR.VARIABLE in output
        assert _pin_text(pin_repo) == before

    @pytest.mark.parametrize("reason", ["relative", "missing"])
    def test_a_relative_or_missing_generator_writes_nothing(self, pin_repo, tmp_path, reason) -> None:
        value = "zqsentinel/specimen-generator" if reason == "relative" else os.fspath(tmp_path / "no generator here")
        before = _pin_text(pin_repo)
        for check in (False, True):
            code, output = _pin(pin_repo, None, environ={_GENERATOR.VARIABLE: value}, check=check)
            assert code == 1 and _GENERATOR.VARIABLE in output
        assert _pin_text(pin_repo) == before

    def test_a_configured_generator_that_is_not_the_venvs_is_refused(self, pin_repo, tmp_path) -> None:
        candidate = tmp_path / "candidate-generator"
        candidate.write_bytes(b"")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, None, environ={_GENERATOR.VARIABLE: os.fspath(candidate)})
        assert code == 1 and "not the one .venv holds" in output
        assert _pin_text(pin_repo) == before

    def test_the_venvs_generator_on_another_interpreter_is_refused(self, pin_repo) -> None:
        """``pin`` writes the pins every machine meets, so it re-pins on CPython 3.11 alone."""
        installed = _GENERATOR.installed_executable(pin_repo)
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"")
        (pin_repo / ".venv" / "pyvenv.cfg").write_text("version_info = 3.13.0\n", encoding="utf-8", newline="\n")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, None, environ={})
        assert code == 1 and "runs on Python 3.13" in output and "pinned on Python 3.11 alone" in output
        assert _pin_text(pin_repo) == before

    def test_check_names_a_specimen_whose_digest_differs_and_writes_nothing(self, pin_repo, stub, tmp_path) -> None:
        moved = tmp_path / "moved"
        shutil.copytree(BASIC_MODEL, moved / "model")
        first = sorted((moved / "model").rglob("*.yaml"), key=lambda path: path.as_posix())[0]
        first.write_bytes(first.read_bytes() + b"\n")
        before = _pin_text(pin_repo)
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(moved)), check=True)
        assert code == 1 and "twin differ from the pins" in output
        assert _pin_text(pin_repo) == before

    def test_check_on_a_clean_tree_exits_0(self, pin_repo, stub, basic_source) -> None:
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)), check=True)
        assert code == 0, output

    def test_a_name_with_no_passport_is_refused(self, pin_repo, stub, basic_source) -> None:
        code, output = _pin(pin_repo, stub_command(stub, "copy", os.fspath(basic_source)), names=["absent"])
        assert code == 1 and "absent" in output
