"""The `.vscode/settings.json` `swcraft init` scaffolds, and what it must never become.

It answers Red Hat XML's LemMinX marking a valid generated ``model.arxml`` (``cvc-elt.1.a``,
``schema_reference.4`` on ``dist/AUTOSAR_4-3-0.xsd``): it resolves ``xsi:schemaLocation`` relative to the
file, where no licensed schema lies. The fix is right only while both hold:

1. **The setting turns a check off; it never points at a schema** — ``swcraft validate`` stays the sole
   ARXML gate, and a bound schema would put a squiggle in the editor that looks like it.
2. **It is a setting the user could have written** — one key in their own workspace settings, unlike the
   ``contributes.configurationDefaults`` the extension may not declare.

What these tests do **not** reach: whether LemMinX honours the filter (a running-host measurement). The
key, value shape and glob are read from ``redhat.vscode-xml`` 0.29.3's own contributed configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import TEMPLATE_ROOT
from swcraft_tools import __version__
from swcraft_tools.cli import main, toolchain_pin_problem
from swcraft_tools.cli.init_command import _VSCODE_SETTINGS_TEMPLATE, _vscode_settings
from swcraft_tools.project import require_project

#: The Red Hat XML settings that bind a schema; the scaffolded file may name none. Kept in step with
#: ``XSD_BINDING_SETTINGS`` in ``vscode_extension/src/languageAssociation.test.ts`` minus
#: ``xml.validation.filters``, which only the extension manifest may not name.
_BINDING_SETTINGS = (
    "xml.fileAssociations",
    "xml.catalogs",
)

#: The template's copy of the same file.
_TEMPLATE_SETTINGS = TEMPLATE_ROOT / ".vscode" / "settings.json"


def _run_init(monkeypatch, target: Path, *extra: str) -> None:
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *extra])
    main()


class TestScaffoldedSettingsContent:
    """What the two scaffolded keys say."""

    def test_the_keys_are_exactly_the_filter_and_the_cspell_ignore_paths(self) -> None:
        assert list(_VSCODE_SETTINGS_TEMPLATE) == ["xml.validation.filters", "cSpell.ignorePaths"]

    def test_the_defaults_ignore_the_three_default_roots(self) -> None:
        """Code Spell Checker marked every identifier of the model; the three roots are left to it."""
        assert _VSCODE_SETTINGS_TEMPLATE["cSpell.ignorePaths"] == ["model/**", "dist/**", "views/**"]

    def test_the_defaults_are_the_settings_of_the_default_roots(self) -> None:
        assert _vscode_settings("model", "dist", "views") == _VSCODE_SETTINGS_TEMPLATE

    def test_a_root_changes_only_the_cspell_key(self) -> None:
        settings = _vscode_settings("arch/model", "dist", "out/views")
        assert settings["cSpell.ignorePaths"] == ["arch/model/**", "dist/**", "out/views/**"]
        assert settings["xml.validation.filters"] == _VSCODE_SETTINGS_TEMPLATE["xml.validation.filters"]

    @pytest.mark.parametrize("root", ["model[v2]", "mo*del", "m?del", "model{a,b}", "!model", "mod(el)", "a\\b"])
    def test_a_root_holding_a_glob_metacharacter_gets_no_entry(self, root: str) -> None:
        """Pasted into the glob it would match other folders; no entry, and the root is spell-checked as before."""
        assert _vscode_settings(root, "dist", "views")["cSpell.ignorePaths"] == ["dist/**", "views/**"]

    def test_a_root_holding_a_space_needs_no_escape(self) -> None:
        assert _vscode_settings("my model", "dist", "views")["cSpell.ignorePaths"] == [
            "my model/**",
            "dist/**",
            "views/**",
        ]

    def test_the_filter_targets_arxml_and_nothing_else(self) -> None:
        entries = _VSCODE_SETTINGS_TEMPLATE["xml.validation.filters"]
        assert [entry["pattern"] for entry in entries] == ["**/*.arxml"]

    def test_the_filter_turns_checking_off_rather_than_binding_a_schema(self) -> None:
        """`schema.enabled: never` plus `noGrammar: ignore`, and no schema named: one per LemMinX marker."""
        entry = _VSCODE_SETTINGS_TEMPLATE["xml.validation.filters"][0]
        assert entry["noGrammar"] == "ignore"
        assert entry["schema"] == {"enabled": "never"}

    def test_no_schema_path_appears_anywhere_in_the_scaffolded_settings(self) -> None:
        """A binding is a *path* (an ``.xsd`` or a catalog); turning a check off needs none."""
        serialised = json.dumps(_VSCODE_SETTINGS_TEMPLATE)
        assert ".xsd" not in serialised.lower()
        for setting in _BINDING_SETTINGS:
            assert setting not in serialised

    def test_nothing_is_disabled_workspace_wide(self) -> None:
        """No top-level key silences XML checking for every document, the project's hand-written XML included."""
        assert "xml.validation.enabled" not in _VSCODE_SETTINGS_TEMPLATE
        assert "xml.validation.schema.enabled" not in _VSCODE_SETTINGS_TEMPLATE


class TestScaffoldedSettingsAreWritten:
    """Both `init` shapes write it, and neither overwrites the user's own."""

    @pytest.mark.parametrize("extra", [(), ("--integration", "moon")], ids=["default", "moon"])
    def test_init_writes_the_settings_file(self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]) -> None:
        """Both profiles generate `.arxml`, so both meet the squiggle."""
        target = tmp_path / "project"
        _run_init(monkeypatch, target, *extra)

        written = target / ".vscode" / "settings.json"
        assert written.is_file()
        settings = json.loads(written.read_text(encoding="utf-8"))
        assert settings == _VSCODE_SETTINGS_TEMPLATE
        assert settings["cSpell.ignorePaths"] == ["model/**", "dist/**", "views/**"]

    def test_the_template_file_is_the_defaults_rendering_byte_for_byte(self) -> None:
        """The static template file is what `init` would render for the default roots, to the byte."""
        rendering = json.dumps(_vscode_settings("model", "dist", "views"), indent=2)
        assert _TEMPLATE_SETTINGS.read_bytes() == rendering.encode("utf-8")

    def test_the_template_settings_carry_the_three_roots(self) -> None:
        settings = json.loads(_TEMPLATE_SETTINGS.read_text(encoding="utf-8"))
        assert settings["cSpell.ignorePaths"] == ["model/**", "dist/**", "views/**"]

    def test_the_template_copy_matches_the_default_plan(self) -> None:
        """The packaged template does not drift from `cli/init_command.py`: moon copies it verbatim."""
        assert json.loads(_TEMPLATE_SETTINGS.read_text(encoding="utf-8")) == _VSCODE_SETTINGS_TEMPLATE

    @pytest.mark.parametrize("extra", [(), ("--integration", "moon")], ids=["default", "moon"])
    def test_an_existing_settings_file_is_kept(self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]) -> None:
        """`init` never overwrites — a developer's settings file carries their editor preferences."""
        target = tmp_path / "project"
        (target / ".vscode").mkdir(parents=True)
        original = '{"editor.tabSize": 4}'
        (target / ".vscode" / "settings.json").write_text(original, encoding="utf-8")

        _run_init(monkeypatch, target, *extra)

        assert (target / ".vscode" / "settings.json").read_text(encoding="utf-8") == original


def _existing_manifest(target: Path, body: str) -> Path:
    """A ``swcraft.yaml`` already in *target* before ``init`` runs."""
    target.mkdir(parents=True)
    manifest = target / "swcraft.yaml"
    manifest.write_text(body, encoding="utf-8", newline="\n")
    return manifest


def _written_ignore_paths(target: Path) -> list[str]:
    return json.loads((target / ".vscode" / "settings.json").read_text(encoding="utf-8"))["cSpell.ignorePaths"]


#: A manifest's ``outputs`` still naming the retired ``rst`` output: ``check-model``'s
#: ``manifest.unknown-key`` error, no load refusal, so the manifest is read.
_RST_OUTPUTS = "outputs:\n  rst: dist/requirements.rst\n"

_SHAPES = pytest.mark.parametrize("extra", [(), ("--integration", "moon")], ids=["default", "moon"])


class TestIgnorePathsFollowTheManifest:
    """The cSpell ignore paths are the roots of the manifest in place after the ``swcraft.yaml`` step."""

    @_SHAPES
    def test_an_existing_manifests_roots_are_ignored(self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]) -> None:
        target = tmp_path / "project"
        _existing_manifest(
            target,
            "name: project\nmodel_root: arch/model\nviews_root: out/views\n",
        )

        _run_init(monkeypatch, target, *extra)

        paths = _written_ignore_paths(target)
        assert paths == ["arch/model/**", "dist/**", "out/views/**"]
        settings = json.loads((target / ".vscode" / "settings.json").read_text(encoding="utf-8"))
        assert settings["xml.validation.filters"] == _VSCODE_SETTINGS_TEMPLATE["xml.validation.filters"]

    @_SHAPES
    def test_no_path_is_absolute_or_climbs(self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]) -> None:
        target = tmp_path / "project"
        _existing_manifest(
            target,
            "name: project\nmodel_root: ./arch/model/\ndist_root: build/../out/dist\n",
        )

        _run_init(monkeypatch, target, *extra)

        paths = _written_ignore_paths(target)
        assert paths == ["arch/model/**", "out/dist/**", "views/**"]
        for path in paths:
            assert not path.startswith(("/", "..")) and ":" not in path and "\\" not in path

    @_SHAPES
    @pytest.mark.parametrize(
        "body",
        ["name: [unclosed\n", "name: project\nmodel_root: ../outside\n", "- not a mapping\n"],
        ids=["yaml-error", "root-outside", "not-a-mapping"],
    )
    def test_an_unreadable_existing_manifest_gives_the_defaults(
        self, tmp_path: Path, monkeypatch, body: str, extra: tuple[str, ...]
    ) -> None:
        target = tmp_path / "project"
        _existing_manifest(target, body)

        _run_init(monkeypatch, target, *extra)

        assert _written_ignore_paths(target) == ["model/**", "dist/**", "views/**"]
        assert (target / "swcraft.yaml").read_text(encoding="utf-8") == body

    @_SHAPES
    def test_a_programming_error_while_reading_the_manifest_is_not_the_defaults(
        self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]
    ) -> None:
        """Only the loader's refusals give the default roots; anything else propagates."""
        target = tmp_path / "project"
        _existing_manifest(target, "name: project\n")

        def broken(path: str) -> None:
            raise TypeError("injected programming error")

        monkeypatch.setattr("swcraft_tools.cli.init_command.require_project", broken)
        with pytest.raises(TypeError, match="injected programming error"):
            _run_init(monkeypatch, target, *extra)

    @_SHAPES
    def test_a_manifest_too_deep_to_validate_gives_the_defaults(
        self, tmp_path: Path, monkeypatch, extra: tuple[str, ...]
    ) -> None:
        """A validator's ``RecursionError`` on the kept manifest is a refusal to read it, not a crash."""
        target = tmp_path / "project"
        _existing_manifest(target, "name: project\n")

        reached: list[str] = []

        def too_deep(path: str) -> None:
            reached.append(path)
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr("swcraft_tools.cli.init_command.require_project", too_deep)
        _run_init(monkeypatch, target, *extra)

        assert reached, "the stand-in was not reached, so the defaults prove nothing about the refusal"
        assert _written_ignore_paths(target) == ["model/**", "dist/**", "views/**"]


class TestInitChecksTheExistingManifestsPin:
    """``init`` reads an existing manifest, so it checks that manifest's ``toolchain_version`` first."""

    @_SHAPES
    def test_a_manifest_pinned_to_another_release_refuses_init(
        self, tmp_path: Path, monkeypatch, capsys, extra: tuple[str, ...]
    ) -> None:
        target = tmp_path / "project"
        manifest = _existing_manifest(target, 'name: project\ntoolchain_version: "0.0.1"\n')

        with pytest.raises(SystemExit) as exit_info:
            _run_init(monkeypatch, target, *extra)

        assert exit_info.value.code == 1
        out, err = capsys.readouterr()
        problem = toolchain_pin_problem(require_project(str(manifest)))
        assert problem is not None
        assert err == f"Error: {problem}\n"
        assert out == f"Skipped {target / 'swcraft.yaml'} (already exists)\n"
        assert [path.name for path in target.iterdir()] == ["swcraft.yaml"]

    @_SHAPES
    def test_a_manifest_still_naming_the_retired_rst_output_and_pinned_to_another_release_refuses_init(
        self, tmp_path: Path, monkeypatch, capsys, extra: tuple[str, ...]
    ) -> None:
        """A manifest still naming ``outputs.rst`` is read, so its pin is checked like any other's."""
        target = tmp_path / "project"
        _existing_manifest(target, f'name: project\ntoolchain_version: "0.0.1"\n{_RST_OUTPUTS}')

        with pytest.raises(SystemExit) as exit_info:
            _run_init(monkeypatch, target, *extra)

        assert exit_info.value.code == 1
        out, err = capsys.readouterr()
        assert err.startswith("Error: ") and "0.0.1" in err
        assert out == f"Skipped {target / 'swcraft.yaml'} (already exists)\n"
        assert [path.name for path in target.iterdir()] == ["swcraft.yaml"]

    @_SHAPES
    def test_a_manifest_still_naming_the_retired_rst_output_gives_its_own_roots(
        self, tmp_path: Path, monkeypatch, capsys, extra: tuple[str, ...]
    ) -> None:
        target = tmp_path / "project"
        _existing_manifest(target, f"name: project\nmodel_root: arch/model\n{_RST_OUTPUTS}")

        _run_init(monkeypatch, target, *extra)

        assert "Error:" not in capsys.readouterr().err
        # The loader reads the manifest (the retired key is check-model's error), so its roots are used.
        assert _written_ignore_paths(target) == ["arch/model/**", "dist/**", "views/**"]

    @_SHAPES
    @pytest.mark.parametrize("pin", ["", f'toolchain_version: "{__version__}"\n'], ids=["no-pin", "this-release"])
    def test_a_manifest_without_another_pin_is_scaffolded_as_before(
        self, tmp_path: Path, monkeypatch, capsys, pin: str, extra: tuple[str, ...]
    ) -> None:
        target = tmp_path / "project"
        _existing_manifest(target, f"name: project\n{pin}")

        _run_init(monkeypatch, target, *extra)

        err = capsys.readouterr().err
        assert "Error:" not in err and "toolchain_version" not in err
        assert (target / "model" / "hardware" / "CPU.yaml").is_file()
        assert _written_ignore_paths(target) == ["model/**", "dist/**", "views/**"]
