"""Tests for the cli package.

``validate`` calls only ``check_cross_layer_consistency``: it already runs the connector,
data-type reference and data-type cycle checks, so calling all four would duplicate output.
"""

from __future__ import annotations

import ast
import codecs
import hashlib
import importlib
import inspect
import io
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import editing_vector_fixtures
import pytest
import swcraft_tools.env as env_module
import swcraft_tools.resource_report as resource_report_module
import yaml
from build_report_support import PASSED, SCHEMA_PATH, report_section
from cli_projects import dump_yaml, write_query_project
from conftest import (
    REPO_ROOT,
    TEMPLATE_ROOT,
    _root_composition,
    _swc,
    _write_manifest,
    commit_link_out_of_the_tree,
    model_element_path,
    parser_subcommand_options,
    partition_on,
    write_model_element,
)
from guide_fixtures import help_links_constant
from mode_bit_support import read_denied, write_denied
from node_support import requires_node
from scratch_git import git as _git
from swcraft_tools import arxml_validation, build_report, cli, markdown_cells, toolchain_pin
from swcraft_tools._version import __version__
from swcraft_tools.agent_clients import load_client_table
from swcraft_tools.arxml_validation import MODE_XSD, ValidationOutcome
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.cli import (
    build_commands,
    handover,
    init_command,
    main,
    project_args,
    report_files,
)
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.cli.init_command import (
    _HARDWARE_ELEMENT_PLAN_FILES,
    _SWCRAFT_YAML_TEMPLATE,
    _VSCODE_SETTINGS_TEMPLATE,
    _default_file_plan,
    _template_file_plan,
    _withheld_hardware_elements,
)
from swcraft_tools.component_exchange import ComponentExportReport, export_component
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import subject_address
from swcraft_tools.guide_reference import cli as guide_reference_cli
from swcraft_tools.model_registry import production_registry
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    ConsistencyIssue,
    ConsistencyResult,
    check_cross_layer_consistency,
    check_hardware_topology,
)
from swcraft_tools.writer import DiagramGenerationError, DiagramLayoutError, DiagramsResult, generate_arxml
from symlink_support import symlink_or_skip
from windows_newline_support import assert_no_crlf, windows_newline_translation
from xsd_support import run_without_a_checkout


@pytest.fixture
def _no_local_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin structural mode, so "the note says skipped" is not a statement about the developer's machine.

    On a machine holding the licensed XSDs ``cmd_validate`` would take the XSD branch and never
    print the note, or keep the file, these tests are about.
    """
    import swcraft_tools.arxml_validation as _validation

    monkeypatch.delenv("SWCRAFT_AUTOSAR_XSD_PATH", raising=False)
    monkeypatch.setattr(
        _validation,
        "DEFAULT_SCHEMA_PATHS",
        {version: f"/nonexistent/{version.schema_filename}" for version in ArxmlVersion},
    )


def _required_layer_directories() -> tuple[str, ...]:
    """Every non-optional layer's storage directory, in contract order, read off the kernel registry.

    No shipped layer is optional, so this is every layer's directory (`_optional_layer_directories()`
    is empty for the production registry).
    """
    return tuple(facts.storage.directory for facts in production_registry().layers if not facts.optional)


def _optional_layer_directories() -> tuple[str, ...]:
    """The directories `swcraft init` deliberately does **not** create: the optional layers', none shipped."""
    return tuple(facts.storage.directory for facts in production_registry().layers if facts.optional)


def _cli_modules() -> list[ModuleType]:
    """Every module of the ``swcraft_tools.cli`` package at any depth, the package itself first."""
    walked = pkgutil.walk_packages(cli.__path__, prefix=f"{cli.__name__}.")
    return [cli] + [importlib.import_module(info.name) for info in walked]


def _stub_generated_arxml(tmp_path: Path) -> str:
    """A real, structurally valid ARXML file for tests that mock ``generate_arxml``.

    ``cmd_validate`` validates the file the generator says it wrote, so a ``MagicMock``
    return value is no usable stand-in.
    """
    path = tmp_path / "dist" / "model.arxml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0"/>\n',
        encoding="utf-8",
    )
    return str(path)


def _port(name: str) -> dict:
    return {"name": name, "direction": "provide", "interface": "ISpeed"}


def _write_swc(
    model_root: Path,
    name: str,
    description: str = "test swc",
    ports: list[dict] | None = None,
    handle_termination_and_restart: str | None = "no_support",
) -> None:
    """Write one application component.

    It declares ``no_support`` unless a test passes ``None`` to leave the field absent (a component without it
    is refused).
    """
    swc_dir = model_root / "swc"
    swc_dir.mkdir(parents=True, exist_ok=True)
    element: dict = {"name": name, "type": "application", "description": description}
    if ports is not None:
        element["ports"] = ports
    if handle_termination_and_restart is not None:
        element["handle_termination_and_restart"] = handle_termination_and_restart
    (swc_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(element, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


# swcraft init


class TestCliInit:
    def test_cli_init_creates_swcraft_yaml(self, tmp_path: Path, monkeypatch) -> None:
        """swcraft init <path> → swcraft.yaml with name, schema_version, model_root, etc."""
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        manifest = target / "swcraft.yaml"
        assert manifest.is_file()
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert "name" in data
        assert data["schema_version"] == "1.0"
        assert data["model_root"] == "model"
        assert data["dist_root"] == "dist"
        assert data["views_root"] == "views"
        # Contract 6.0.0: the diagrams go to `views_root`, so `outputs` names the two files alone.
        assert data["outputs"] == {"arxml": "dist/model.arxml", "needs": "dist/needs.json"}

    def test_cli_init_scaffolds_lf_even_where_the_platform_would_translate(self, tmp_path: Path, monkeypatch) -> None:
        """`swcraft init` writes files the architect commits, so they are LF on every host.

        On Linux the check would be vacuous, so :func:`windows_newline_translation` supplies
        the translating platform at the ``io.open`` boundary.
        """
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        with windows_newline_translation():
            main()

        for relative in ("swcraft.yaml", ".gitignore", ".vscode/settings.json"):
            assert_no_crlf(target / relative, what=f"the scaffolded {relative}")

    def test_cli_init_creates_a_directory_for_every_required_layer(self, tmp_path: Path, monkeypatch) -> None:
        """`swcraft init` creates one directory per non-optional layer, and no more.

        The "optional layers get none" half is asserted in `test_cli_scaffold_generic.py`,
        on a merged registry that has an optional layer.
        """
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        model_root = target / "model"
        required = _required_layer_directories()
        assert _optional_layer_directories() == (), "an optional layer is back; restore the second half"
        assert required, "the assertion is vacuous unless the contract declares a layer"
        # Compared as model-root-relative POSIX paths: the data description layers'
        # directories are nested, so their common parent `data_types` is created too,
        # as a container only.
        parents = {
            "/".join(directory.split("/")[:depth])
            for directory in required
            for depth in range(1, directory.count("/") + 1)
        }
        assert "data_types" in parents and "data_types" not in required
        assert {path.relative_to(model_root).as_posix() for path in model_root.rglob("*") if path.is_dir()} == set(
            required
        ) | parents

    def test_cli_init_writes_a_processor_a_core_and_a_partition(self, tmp_path: Path, monkeypatch) -> None:
        """The three hardware elements a fresh project needs: a processor, a core and a partition.

        `hardware.no-processor` refuses a project without a processor, and a task names a
        partition, so a fresh project's first task needs no hardware edit. No memory is
        scaffolded: a `size` is a hardware fact that cannot be invented.
        """
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        hardware = target / "model" / "hardware"
        assert sorted(path.name for path in hardware.iterdir()) == ["CORE0.yaml", "CPU.yaml", "PARTITION0.yaml"]
        processor = yaml.safe_load((hardware / "CPU.yaml").read_text(encoding="utf-8"))
        core = yaml.safe_load((hardware / "CORE0.yaml").read_text(encoding="utf-8"))
        partition = yaml.safe_load((hardware / "PARTITION0.yaml").read_text(encoding="utf-8"))
        assert processor["name"] == "CPU" and processor["kind"] == "processor"
        assert core["name"] == "CORE0" and core["kind"] == "core"
        # Honest placeholder values: uncapped and report-only until the project
        # sets a real figure, so the scaffold constrains nothing.
        assert core["threshold"] == 1.0
        assert core["enforcement"] == "advisory"
        # The partition sits on the scaffolded core and claims no level: a level
        # is a safety statement only the project can make, and an unrated
        # partition is compared against nothing.
        assert partition["name"] == "PARTITION0" and partition["kind"] == "partition"
        assert partition["core"] == "CORE0"
        assert "asil" not in partition and "coexistence" not in partition
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_never_overwrites_an_existing_hardware_element(self, tmp_path: Path, monkeypatch) -> None:
        """Re-running `init` keeps a hardware element the architect has replaced."""
        target = tmp_path / "new-project"
        (target / "model" / "hardware").mkdir(parents=True)
        (target / "model" / "hardware" / "CPU.yaml").write_text(
            "name: CPU\nkind: processor\ndescription: Infineon AURIX TC397\n", encoding="utf-8"
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        kept = (target / "model" / "hardware" / "CPU.yaml").read_text(encoding="utf-8")
        assert kept == "name: CPU\nkind: processor\ndescription: Infineon AURIX TC397\n"
        # The file that was not there is still written.
        assert (target / "model" / "hardware" / "CORE0.yaml").is_file()

    def test_cli_init_over_a_project_whose_processor_has_another_filename(self, tmp_path: Path, monkeypatch) -> None:
        """The per-path guard is not enough: "exactly one processor" spans files.

        Re-running `init` over a project whose processor is `hardware/TC397.yaml` is a
        supported flow; writing `hardware/CPU.yaml` beside it would report
        `hardware.multiple-processors`.
        """
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "TC397.yaml").write_text(
            "name: TC397\nkind: processor\ndescription: Infineon AURIX TC397\n", encoding="utf-8"
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert not (hardware / "CPU.yaml").exists(), "a second processor was scaffolded"
        # The core is not a processor and its name is free, so it is still written
        # -- and so is the partition on it, because this run writes its core.
        assert (hardware / "CORE0.yaml").is_file()
        assert (hardware / "PARTITION0.yaml").is_file()
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_withholds_an_element_whose_name_is_already_declared(self, tmp_path: Path, monkeypatch) -> None:
        """`hardware.duplicate-name` spans files too: a `CORE0` in another file withholds the core."""
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "TC397.yaml").write_text("name: TC397\nkind: processor\n", encoding="utf-8")
        (hardware / "ApplicationCore.yaml").write_text(
            "name: CORE0\nkind: core\nthreshold: 0.7\nenforcement: strict\n", encoding="utf-8"
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert sorted(path.name for path in hardware.iterdir()) == ["ApplicationCore.yaml", "TC397.yaml"]
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_withholds_the_partition_with_the_core_it_would_sit_on(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`init` never writes a partition onto a core it did not write.

        The project's own `CORE0` withholds the core for its name, and the partition with it;
        the reason printed is the core's, not a name clash.
        """
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "TC397.yaml").write_text("name: TC397\nkind: processor\n", encoding="utf-8")
        (hardware / "ApplicationCore.yaml").write_text("name: CORE0\nkind: core\nthreshold: 0.7\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert sorted(path.name for path in hardware.iterdir()) == ["ApplicationCore.yaml", "TC397.yaml"]
        out = capsys.readouterr().out
        assert "PARTITION0.yaml (its core 'CORE0' is not one this run writes)" in out
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_withholds_the_partition_when_the_core_file_is_already_there(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A kept `hardware/CORE0.yaml` that declares another core withholds the partition.

        `CORE0` is then declared by nothing, so a scaffolded `PARTITION0` on it would be a
        broken reference.
        """
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "CORE0.yaml").write_text("name: MainCore\nkind: core\nthreshold: 0.7\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert sorted(path.name for path in hardware.iterdir()) == ["CORE0.yaml", "CPU.yaml"]
        assert (hardware / "CORE0.yaml").read_text(encoding="utf-8") == "name: MainCore\nkind: core\nthreshold: 0.7\n"
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_withholds_the_partition_whose_name_is_already_declared(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A partition named `PARTITION0` in another file withholds the scaffold's partition alone."""
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "SafetyCore.yaml").write_text("name: SafetyCore\nkind: core\nthreshold: 0.7\n", encoding="utf-8")
        (hardware / "Safety.yaml").write_text("name: PARTITION0\nkind: partition\ncore: SafetyCore\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert sorted(path.name for path in hardware.iterdir()) == [
            "CORE0.yaml",
            "CPU.yaml",
            "Safety.yaml",
            "SafetyCore.yaml",
        ]
        assert "PARTITION0.yaml (hardware/ already declares 'PARTITION0')" in capsys.readouterr().out
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_withholds_both_elements_when_the_hardware_layer_does_not_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A `hardware/` that does not parse withholds both elements; everything else is still written."""
        target = tmp_path / "existing-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()  # must not raise

        assert sorted(path.name for path in hardware.iterdir()) == ["Broken.yaml"]
        assert (target / "swcraft.yaml").is_file()
        assert (target / "model" / "swc").is_dir()

    def test_cli_init_withholds_both_elements_when_hardware_cannot_be_read_at_all(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An unreadable element file (`PermissionError`) withholds both elements like a parse error.

        Guards a defect: a guard that caught only `ValueError` let it out as a traceback after
        the manifest and the directories were already on disk.
        """
        target = tmp_path / "unreadable-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        unreadable = hardware / "Real.yaml"
        unreadable.write_text("name: TC397\nkind: processor\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        with read_denied(unreadable):
            main()  # must not raise

        assert sorted(path.name for path in hardware.iterdir()) == ["Real.yaml"]
        assert (target / "swcraft.yaml").is_file()
        assert (target / "model" / "swc").is_dir()
        assert "hardware/ does not read" in capsys.readouterr().out

    def test_cli_init_withholds_both_elements_when_a_hardware_entry_is_a_directory(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A `hardware/Foo.yaml` that is a directory (`IsADirectoryError`) withholds both elements.

        The one OSError shape that reproduces on every platform and for every user, root included.
        """
        target = tmp_path / "directory-shaped-project"
        hardware = target / "model" / "hardware"
        (hardware / "Foo.yaml").mkdir(parents=True)
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()  # must not raise

        assert not (hardware / "CPU.yaml").exists()
        assert not (hardware / "CORE0.yaml").exists()
        assert not (hardware / "PARTITION0.yaml").exists()
        assert (target / "swcraft.yaml").is_file()
        assert "hardware/ does not read" in capsys.readouterr().out

    def test_cli_init_reports_a_write_refusal_the_way_check_model_does(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A read-only `model/hardware/` exits 1 with `check-model`'s `Error: <exc>`, never a traceback.

        Files already created stay created: `init` has never been all-or-nothing.
        """
        target = tmp_path / "read-only-hardware"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        with write_denied(hardware), pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("Error: ")
        assert "Permission denied" in captured.err
        assert "Traceback" not in captured.err
        assert (target / "swcraft.yaml").is_file()

    def test_cli_init_integration_moon_withholds_the_same_two_elements(self, tmp_path: Path, monkeypatch) -> None:
        """The profile path withholds the same two elements, though another function builds its plan."""
        target = tmp_path / "existing-moon-project"
        hardware = target / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "TC397.yaml").write_text("name: TC397\nkind: processor\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), "--integration", "moon"])

        main()

        assert not (hardware / "CPU.yaml").exists(), "a second processor was scaffolded"
        assert check_hardware_topology(str(target / "model")).consistent

    def test_every_withholdable_path_is_a_key_of_both_plans(self, tmp_path: Path) -> None:
        """Every withholdable path is a key of both plans, so the guard's candidate set is never empty.

        `_withheld_hardware_elements` filters by path; a path spelled differently in a plan
        would silence the guard, and the behavioural tests above would then pass for the wrong
        reason. `_template_file_plan` walks the template tree, where a renamed file drifts silently.
        """
        assert _HARDWARE_ELEMENT_PLAN_FILES, "the guard has nothing to guard"
        for label, plan in (
            ("the default plan", _default_file_plan("proj")),
            ("the moon profile plan", _template_file_plan("moon", "proj")),
        ):
            unreachable = [path for path in _HARDWARE_ELEMENT_PLAN_FILES if path not in plan]
            assert unreachable == [], f"{label} spells none of {unreachable}, so the guard cannot reach them"

        hardware = tmp_path / "model" / "hardware"
        hardware.mkdir(parents=True)
        (hardware / "TC397.yaml").write_text("name: CPU\nkind: processor\n", encoding="utf-8")
        (hardware / "Zero.yaml").write_text("name: CORE0\nkind: core\nthreshold: 1.0\n", encoding="utf-8")

        withheld = _withheld_hardware_elements(tmp_path / "model", _default_file_plan("proj"))

        assert [relative for relative, _ in withheld] == list(_HARDWARE_ELEMENT_PLAN_FILES)

    def test_cli_init_scaffolds_a_hardware_layer_that_check_model_accepts(self, tmp_path: Path, monkeypatch) -> None:
        """A fresh project passes the hardware layer's own rules, with no edit by the user."""
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()

        result = check_hardware_topology(str(target / "model"))

        assert result.consistent, result.issues
        assert result.records == ()

    def test_cli_init_withholds_the_root_composition_a_component_already_claims(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`init` withholds `compositions/Root.yaml` when a component already declares `Root`.

        `swc` and `compositions` share one `/ComponentTypes` `SHORT-NAME` namespace. Guards a
        defect: the composition was written beside the component and `check-model` then refused
        the project. Asserted end to end because the withholding is useful only if `check-model`
        is then green.
        """
        target = tmp_path / "existing-project"
        swc = target / "model" / "swc" / "app"
        swc.mkdir(parents=True)
        (swc / "Root.yaml").write_text("name: Root\ntype: application\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert not (target / "model" / "compositions" / "Root.yaml").exists(), (
            "a composition was scaffolded over a component of that name"
        )

    def test_cli_init_withholds_the_root_composition_a_generated_implementation_claims(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The namespace includes the name the writer derives: `{name}_Implementation`.

        An `external: true` stub is not a claimant: it emits no implementation.
        """
        target = tmp_path / "existing-project"
        swc = target / "model" / "swc" / "app"
        swc.mkdir(parents=True)
        (swc / "RootBase.yaml").write_text("name: RootBase\ntype: application\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        # `RootBase` generates `RootBase_Implementation`, not `Root`: nothing to withhold.
        assert (target / "model" / "compositions" / "Root.yaml").is_file()

    def test_cli_init_then_check_model_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        """The real `check-model` over the real `swcraft init` output exits 0, unmocked.

        Does **not** reach `swcraft validate`, which generates ARXML from an empty model.
        """
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(target / "swcraft.yaml")])
        main()  # must not raise SystemExit

        assert not (target / "dist").exists(), "check-model wrote an artifact"

    def test_a_first_task_on_a_fresh_init_passes_check_model_without_a_hardware_edit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A first task names the scaffold's partition and passes `check-model` with no hardware edit."""
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()
        model_root = target / "model"
        hardware_before = {path.name: path.read_bytes() for path in (model_root / "hardware").iterdir()}

        # The component satisfies the requirement; the link lives in the component.
        _swc(
            model_root,
            "FirstSWC",
            runnables=[{"name": "FirstRun", "events": [{"type": "timing", "period": 0.01}]}],
            satisfies=["REQ_FIRST@1"],
        )
        model_element_path(model_root, "REQ_FIRST", layer="requirements").write_text(
            "id: REQ_FIRST\ntitle: First\ndescription: The first component.\nreq_type: req\n"
            "version: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        write_model_element(
            model_root,
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "FirstSWC", "runnable": "FirstRun"}],
            },
            layer="tasks",
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(target / "swcraft.yaml")])
        main()  # must not raise SystemExit

        assert {path.name: path.read_bytes() for path in (model_root / "hardware").iterdir()} == hardware_before

    @pytest.mark.parametrize(
        ("retired", "sentence"),
        [
            (
                "links:\n- FirstSWC\n",
                "'links' moved to the elements by contract 11.0.0: write 'satisfies: [REQ_OLD@N]'",
            ),
            ("status: open\n", "'status' is retired by contract 11.0.0: an external tool owns the lifecycle"),
        ],
        ids=["links", "status"],
    )
    def test_check_model_refuses_a_requirement_file_of_the_retired_shape(
        self, tmp_path: Path, monkeypatch, capsys, retired: str, sentence: str
    ) -> None:
        """The retired requirement shape is refused at the CLI, naming the current one, never converted."""
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()
        capsys.readouterr()
        model_element_path(target / "model", "REQ_OLD", layer="requirements").write_text(
            "id: REQ_OLD\ntitle: Old\ndescription: The old shape.\nversion: 1\nsource: architecture\n" + retired,
            encoding="utf-8",
            newline="\n",
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(target / "swcraft.yaml")])
        with pytest.raises(SystemExit) as exit_:
            main()

        assert exit_.value.code not in (0, None)
        assert sentence in capsys.readouterr().err

    def test_cli_init_integration_moon_scaffolds_the_same_hardware_layer(self, tmp_path: Path, monkeypatch) -> None:
        """The profile path really writes the three hardware elements.

        `test_template_contents.py` compares the bytes, which alone does not say they are written.
        """
        target = tmp_path / "moon-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), "--integration", "moon"])
        main()

        hardware = target / "model" / "hardware"
        assert sorted(path.name for path in hardware.iterdir()) == ["CORE0.yaml", "CPU.yaml", "PARTITION0.yaml"]
        assert check_hardware_topology(str(target / "model")).consistent

    def test_cli_init_writes_no_editor_mcp_configuration_by_any_route(self, tmp_path: Path, monkeypatch) -> None:
        """Neither profile scaffolds an MCP configuration under `.vscode/`.

        The SWCraft extension registers the server with VS Code itself, so such a file would
        be a second, stale server definition. Asserted as "no file whose name says MCP", so a
        differently named file fails too. The template's one such file is Claude Code's
        ``.mcp.json`` at the project root, written only when ``claude`` is chosen.
        """
        for extra, expected in (
            ([], []),
            (["--integration", "moon"], []),
            (["--integration", "moon", "--client", "claude"], [".mcp.json"]),
        ):
            target = tmp_path / f"project{len(extra)}"
            monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *extra])

            main()

            written = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
            assert [name for name in written if "mcp" in Path(name).name.lower()] == expected, (
                f"`swcraft init {' '.join(extra)}` scaffolded MCP configuration: {written}"
            )
            assert [name for name in written if name.startswith(".vscode/")] == [".vscode/settings.json"]

    def test_cli_init_skips_existing_swcraft_yaml(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft init on existing project → swcraft.yaml not overwritten."""
        target = tmp_path / "existing-project"
        target.mkdir()
        manifest = target / "swcraft.yaml"
        original_content = "name: original\nschema_version: '1.0'\n"
        manifest.write_text(original_content, encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert manifest.read_text(encoding="utf-8") == original_content
        captured = capsys.readouterr()
        assert "Skipped" in captured.out
        assert "swcraft.yaml" in captured.out

    def test_cli_init_skips_existing_vscode_settings(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft init over an existing project → .vscode/settings.json not overwritten."""
        target = tmp_path / "existing-project"
        vscode = target / ".vscode"
        vscode.mkdir(parents=True)
        settings = vscode / "settings.json"
        original = '{"custom": true}'
        settings.write_text(original, encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        assert settings.read_text(encoding="utf-8") == original
        captured = capsys.readouterr()
        assert "Skipped" in captured.out
        assert "settings.json" in captured.out

    def test_cli_init_writes_a_gitignore_that_ignores_dot_env(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Every init keeps ``.env`` -- the Azure keys -- out of the repository."""
        target = tmp_path / "new-project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        gitignore = target / ".gitignore"
        assert ".env" in [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]
        assert f"Created {gitignore}" in capsys.readouterr().out

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    @pytest.mark.parametrize("final_newline", [True, False])
    def test_cli_init_appends_dot_env_to_a_users_gitignore_and_keeps_every_byte(
        self, tmp_path: Path, monkeypatch, capsys, newline: str, final_newline: bool
    ) -> None:
        """The user's lines stay byte for byte; ``.env`` is added once, in the file's own line ending."""
        target = tmp_path / "existing-project"
        target.mkdir()
        gitignore = target / ".gitignore"
        original = newline.join(["# mine", "build/", "*.log"]) + (newline if final_newline else "")
        gitignore.write_bytes(original.encode("utf-8"))
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])

        main()

        expected = original + ("" if final_newline else newline) + ".env" + newline
        assert gitignore.read_bytes() == expected.encode("utf-8")
        assert f"Updated {gitignore} (added .env)" in capsys.readouterr().out

    @pytest.mark.parametrize("profile", [(), ("--integration", "moon")])
    def test_cli_init_leaves_a_gitignore_that_already_ignores_dot_env_unchanged(
        self, tmp_path: Path, monkeypatch, capsys, profile: tuple[str, ...]
    ) -> None:
        target = tmp_path / "existing-project"
        target.mkdir()
        gitignore = target / ".gitignore"
        original = b"dist/\n  .env  \n"
        gitignore.write_bytes(original)
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *profile])

        main()

        assert gitignore.read_bytes() == original
        assert f"Skipped {gitignore} (already exists)" in capsys.readouterr().out

    def test_cli_init_integration_moon_adds_dot_env_to_a_users_gitignore(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The moon form keeps the user's file too and adds only the ``.env`` line, never the template's others."""
        target = tmp_path / "existing-project"
        target.mkdir()
        gitignore = target / ".gitignore"
        gitignore.write_bytes(b"node_modules/\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), "--integration", "moon"])

        main()

        assert gitignore.read_bytes() == b"node_modules/\n.env\n"
        assert f"Updated {gitignore} (added .env)" in capsys.readouterr().out


# swcraft init --integration moon


#: Every file a profile-less ``swcraft init`` writes, pinned as a set so a leaked or a
#: missing file fails alike. The hardware elements and ``Root.yaml`` are there because
#: ``hardware/`` and ``compositions/`` are mandatory, so a fresh project must pass the
#: ``check-model`` it is told to run.
_DEFAULT_FILES = frozenset(
    {
        "swcraft.yaml",
        ".gitignore",
        ".vscode/settings.json",
        "model/hardware/CPU.yaml",
        "model/hardware/CORE0.yaml",
        "model/hardware/PARTITION0.yaml",
        "model/compositions/Root.yaml",
    }
)


#: The element files ``swcraft init`` writes. "The importer wrote nothing" is stated
#: against this exact scaffold.
_SCAFFOLDED_ELEMENT_FILES = [
    "compositions/Root.yaml",
    "hardware/CORE0.yaml",
    "hardware/CPU.yaml",
    "hardware/PARTITION0.yaml",
]


def _written_element_files(model_root: Path) -> list[str]:
    """Every element file under *model_root*, POSIX-relative to it and sorted."""
    return sorted(path.relative_to(model_root).as_posix() for path in model_root.rglob("*.yaml"))


def _template_sources() -> dict[str, str]:
    """``{relative posix path: text}`` for every file the template ships."""
    return {
        path.relative_to(TEMPLATE_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATE_ROOT.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file() and "__pycache__" not in path.parts
    }


def _scaffolded_files(target: Path) -> set[str]:
    return {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}


def _run_init(monkeypatch, target: Path, *extra: str) -> None:
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *extra])
    main()


class TestCliInitIntegrationMoon:
    """``--integration moon`` scaffolds the packaged vehicle-architecture template.

    1. **Without the flag nothing changes**: the profile is opt-in.
    2. **Existing user files are never overwritten**, in either profile.
    3. **The template is read through ``importlib.resources``**, so the command works from
       an installed wheel with no checkout on disk.
    """

    def test_init_without_the_flag_writes_the_manifest_the_settings_and_the_hardware(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The profile-less scaffold: exactly ``_DEFAULT_FILES`` and one directory per non-optional layer."""
        target = tmp_path / "plain-project"
        _run_init(monkeypatch, target)

        assert _scaffolded_files(target) == set(_DEFAULT_FILES)
        assert (target / "swcraft.yaml").read_text(encoding="utf-8") == _SWCRAFT_YAML_TEMPLATE.format(
            name="plain-project"
        )
        assert json.loads((target / ".vscode" / "settings.json").read_text(encoding="utf-8")) == (
            _VSCODE_SETTINGS_TEMPLATE
        )
        for subdir in _required_layer_directories():
            assert (target / "model" / subdir).is_dir()

    def test_integration_moon_writes_every_template_file(self, tmp_path: Path, monkeypatch) -> None:
        """Every client chosen: the scaffold is the whole template, verbatim but one file.

        `.vscode/settings.json` is derived; with Copilot and another client chosen it also
        carries the client table's companion keys.
        """
        target = tmp_path / "vehicle-arch"
        table = load_client_table()
        _run_init(
            monkeypatch,
            target,
            "--integration",
            "moon",
            *(word for name in table.names() for word in ("--client", name)),
        )

        sources = _template_sources()
        assert sources, "the packaged template must not be empty"
        assert _scaffolded_files(target) == set(sources), (
            "the scaffold is exactly the template's files -- the one derived write, `.vscode/settings.json`'s "
            "cSpell ignore paths from the manifest's roots, replaces a template file and adds none; another "
            "derived write is a decision"
        )
        settings = sources.pop(".vscode/settings.json")
        assert json.loads((target / ".vscode" / "settings.json").read_text(encoding="utf-8")) == {
            **json.loads(settings),
            **table.companion.settings,
        }
        for relative, source in sources.items():
            expected = source.replace("{{PROJECT_NAME}}", "vehicle-arch")
            assert (target / relative).read_text(encoding="utf-8") == expected, f"{relative} was not copied verbatim"

    def test_integration_moon_scaffolds_the_model_subdirectories_too(self, tmp_path: Path, monkeypatch) -> None:
        """The layer directories come from the contract's non-optional layers, as in the default profile."""
        target = tmp_path / "vehicle-arch"
        _run_init(monkeypatch, target, "--integration", "moon")

        for subdir in _required_layer_directories():
            assert (target / "model" / subdir).is_dir()
        assert (target / "model" / ".gitkeep").is_file()

    def test_project_name_comes_from_the_directory_name(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "e-drive-platform"
        _run_init(monkeypatch, target, "--integration", "moon")

        manifest = yaml.safe_load((target / "swcraft.yaml").read_text(encoding="utf-8"))
        assert manifest["name"] == "e-drive-platform"
        assert "{{PROJECT_NAME}}" not in (target / "README.md").read_text(encoding="utf-8")
        assert "e-drive-platform" in (target / "README.md").read_text(encoding="utf-8")

    def test_name_flag_overrides_the_directory_name(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "checkout-dir"
        _run_init(monkeypatch, target, "--integration", "moon", "--name", "VehicleArchitecture")

        manifest = yaml.safe_load((target / "swcraft.yaml").read_text(encoding="utf-8"))
        assert manifest["name"] == "VehicleArchitecture"

    @pytest.mark.parametrize(
        "name",
        [
            "bad: name",  # YAML reads a mapping
            "#comment",  # YAML reads a comment: the value becomes null
            "- item",  # YAML reads a sequence
            "{a: b}",  # YAML reads a flow mapping
            "2024",  # YAML reads an int; the manifest schema wants a string
            "   ",  # nothing at all
        ],
    )
    def test_a_name_yaml_would_not_read_back_is_rejected_before_anything_is_written(
        self, tmp_path: Path, monkeypatch, capsys, name: str
    ) -> None:
        """A name YAML would not read back is rejected before anything is written.

        The name is interpolated into swcraft.yaml as a plain scalar; unchecked, the next
        command would fail to parse the manifest.
        """
        target = tmp_path / "checkout-dir"

        with pytest.raises(SystemExit) as excinfo:
            _run_init(monkeypatch, target, "--integration", "moon", "--name", name)

        assert excinfo.value.code == 1
        assert "--name" in capsys.readouterr().err
        assert not target.exists(), "a rejected name must leave no scaffolding behind"

    def test_a_name_holding_an_undecodable_byte_is_rejected_cleanly(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A non-UTF-8 argv byte (a lone surrogate in ``--name``) exits 1 cleanly, nothing written.

        The surrogate is spelled directly: Windows decodes with ``surrogatepass``, so
        ``os.fsdecode(b"bad\\xff")`` raises there.
        """
        target = tmp_path / "checkout-dir"

        with pytest.raises(SystemExit) as excinfo:
            _run_init(monkeypatch, target, "--integration", "moon", "--name", "bad\udcff")

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: ")
        assert "cannot be written into swcraft.yaml" in err
        assert not target.exists()

    @pytest.mark.parametrize("name", ["vehicle-arch", "Vehicle Architecture", "e_drive.v2", "arch+x", "arch (v2)"])
    def test_a_plain_name_is_accepted_and_round_trips_through_the_manifest(
        self, tmp_path: Path, monkeypatch, name: str
    ) -> None:
        """The rule is a YAML round trip, not a character allow-list: `+` or parentheses still work."""
        target = tmp_path / "checkout-dir"
        _run_init(monkeypatch, target, "--integration", "moon", "--name", name)

        assert yaml.safe_load((target / "swcraft.yaml").read_text(encoding="utf-8"))["name"] == name

    def test_a_directory_name_yaml_would_not_read_back_is_rejected_too(self, tmp_path: Path, monkeypatch) -> None:
        """The default name comes from the directory, and has the same hole."""
        target = tmp_path / "bad: dir"

        with pytest.raises(SystemExit) as excinfo:
            _run_init(monkeypatch, target)

        assert excinfo.value.code == 1

    def test_the_integration_profile_scaffolds_no_editor_mcp_configuration(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The integration profile writes nothing MCP-shaped for the editor, by any route.

        The SWCraft extension registers the server itself. The one MCP-named file is Claude
        Code's portable `.mcp.json` at the project root, written when `claude` is chosen.
        """
        target = tmp_path / "vehicle-arch"
        _run_init(monkeypatch, target, "--integration", "moon", "--client", "claude")
        captured = capsys.readouterr()

        assert not (target / ".vscode" / "mcp.json").exists()
        assert not (target / ".vscode" / "mcp.json.example").exists()
        assert [line for line in captured.out.splitlines() if "mcp" in line.lower()] == [
            f"Created {target / '.mcp.json'}"
        ]

    def test_init_derives_no_file_from_any_other(self) -> None:
        """The `.example` -> generated derivation mechanism is removed, not left empty."""
        # Asked of the module, not its source text: a substring scan would fire on a
        # comment that mentions the identifier.
        assert not [module for module in _cli_modules() if hasattr(module, "_TEMPLATE_DERIVED_FILES")], (
            "the cli package declares a derived-file mapping again -- adding one back is a decision "
            "that belongs in a plan, not in a template edit"
        )

    def test_existing_files_are_reported_as_skipped_and_never_overwritten(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        target = tmp_path / "existing-project"
        (target / ".vscode").mkdir(parents=True)
        (target / "swcraft.yaml").write_text("name: hand-written\n", encoding="utf-8")
        (target / "moon.yml").write_text("# hand-written build graph\n", encoding="utf-8")
        (target / ".vscode" / "settings.json").write_text('{"custom": true}', encoding="utf-8")

        _run_init(monkeypatch, target, "--integration", "moon")
        captured = capsys.readouterr()

        assert (target / "swcraft.yaml").read_text(encoding="utf-8") == "name: hand-written\n"
        assert (target / "moon.yml").read_text(encoding="utf-8") == "# hand-written build graph\n"
        assert (target / ".vscode" / "settings.json").read_text(encoding="utf-8") == '{"custom": true}'
        for name in ("swcraft.yaml", "moon.yml", "settings.json"):
            assert any(line.startswith("Skipped") and name in line for line in captured.out.splitlines()), name
        # …and the rest of the template still arrived: a skip is not an abort.
        assert (target / "README.md").is_file()
        assert (target / ".prototools").is_file()

    def test_a_skipped_profile_file_is_warned_about_on_stderr(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`--integration moon` over a plain-initialised directory warns on stderr about each skipped file.

        The kept `swcraft.yaml` is exactly the file that makes the two shapes disagree.
        """
        target = tmp_path / "existing-project"
        _run_init(monkeypatch, target)  # the default profile first
        capsys.readouterr()

        _run_init(monkeypatch, target, "--integration", "moon")
        captured = capsys.readouterr()

        assert "Warning:" in captured.err
        assert "swcraft.yaml" in captured.err
        assert "moon.yml" not in captured.err, "only the files that were actually kept may be named"

    def test_a_clean_integration_scaffold_warns_about_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _run_init(monkeypatch, tmp_path / "vehicle-arch", "--integration", "moon")
        assert "Warning:" not in capsys.readouterr().err

    def test_the_default_profile_never_warns_about_skipped_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A plain re-init is a documented no-op and stays silent on stderr."""
        target = tmp_path / "plain-project"
        _run_init(monkeypatch, target)
        capsys.readouterr()

        _run_init(monkeypatch, target)

        assert capsys.readouterr().err == ""

    def test_unknown_integration_value_exits_non_zero_listing_the_valid_ones(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        target = tmp_path / "vehicle-arch"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), "--integration", "azure"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code != 0
        message = capsys.readouterr().err
        assert "--integration" in message
        assert "moon" in message
        assert not (target / "swcraft.yaml").exists(), "an invalid profile must scaffold nothing"

    def test_template_is_read_through_importlib_resources(self, tmp_path: Path, monkeypatch) -> None:
        """The template is read through the resources API, the only way that works from a wheel."""
        calls: list[str] = []
        real = init_command.resource_files

        def spy(package: str):
            calls.append(package)
            return real(package)

        monkeypatch.setattr(init_command, "resource_files", spy)
        _run_init(monkeypatch, tmp_path / "vehicle-arch", "--integration", "moon")

        assert calls == ["swcraft_tools"]

    def test_cli_never_locates_the_template_through_dunder_file(self) -> None:
        """The cli never locates the template through `__file__`; ast-based, so prose does not trip it."""
        sources = sorted(Path(cli.__file__).parent.rglob("*.py"), key=lambda path: path.as_posix())
        assert len(sources) > 1, "the scan reads every module of the cli package"
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            assert not [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "__file__"], (
                source.name
            )


# swcraft generate


class TestCliGenerate:
    def test_cli_generate_arxml_requires_project(self, monkeypatch) -> None:
        """No --project → SystemExit(1) with a descriptive message."""
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_generate_arxml_calls_writer(self, tmp_path: Path, monkeypatch) -> None:
        """swcraft generate arxml --project <yaml> → generate_arxml called with resolved paths."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        with patch("swcraft_tools.cli.build_commands.generate_arxml") as mock_generate:
            main()

        mock_generate.assert_called_once()
        call_args = mock_generate.call_args[0]
        assert call_args[0] == str(tmp_path / "model")  # resolved model_root
        assert call_args[1] == str(tmp_path / "dist" / "model.arxml")  # resolved arxml path

    def test_cli_generate_diagrams_calls_the_generator(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft generate diagrams --project <yaml> → generate_diagrams(model_root, views_root, name)."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "diagrams", "--project", str(manifest)])
        result = DiagramsResult(
            views=("ibd-Root", "overview-Root", "nesting"),
            written=("ibd-Root.svg", "ibd-Root.html", "report.txt", "index.html"),
            kept=("overview-Root.svg", "overview-Root.html", "nesting.svg", "nesting.html"),
            removed=("sequence-gone.svg", "sequence-gone.html"),
            not_drawn=(),
            layout_seconds=0.25,
        )

        with patch("swcraft_tools.cli.build_commands.generate_diagrams", return_value=result) as mock_generate:
            main()

        mock_generate.assert_called_once_with(str(tmp_path / "model"), str(tmp_path / "views"), "test-project")
        out = capsys.readouterr().out
        views = tmp_path / "views"
        assert out.splitlines() == [
            "Diagram ibd-Root: written",
            "Diagram overview-Root: kept",
            "Diagram nesting: kept",
            "Removed: sequence-gone.svg",
            "Removed: sequence-gone.html",
            "Layout: 0.25 s",
            f"Report: {views / 'report.txt'}",
            f"Generated diagrams: {views} (3 views)",
        ]

    @pytest.mark.parametrize(
        "refusal",
        [
            DiagramLayoutError("Node.js was not found: no node on PATH."),
            DiagramGenerationError("no diagram was written: the view 'a b' cannot be a file name."),
        ],
        ids=["layout", "file-name"],
    )
    def test_cli_generate_diagrams_names_a_refusal_and_exits_non_zero(
        self, tmp_path: Path, monkeypatch, capsys, refusal: Exception
    ) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "diagrams", "--project", str(manifest)])

        with (
            patch("swcraft_tools.cli.build_commands.generate_diagrams", side_effect=refusal),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err == f"Error: {refusal}\n"
        assert "Generated diagrams" not in captured.out

    def test_cli_generate_diagrams_draws_a_real_model(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The real generator, a real Node: every view's two files, the report and the index."""
        requires_node()
        project = tmp_path / "basic"
        shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", project / "model")
        manifest = _write_manifest(project)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "diagrams", "--project", str(manifest)])

        main()

        views = project / "views"
        golden = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams_golden" / "basic"
        assert sorted(entry.name for entry in views.iterdir()) == sorted(entry.name for entry in golden.iterdir())
        out = capsys.readouterr().out
        assert "Diagram ibd-Root: written" in out
        assert f"Generated diagrams: {views} (5 views)" in out

    def test_cli_generate_c4_is_refused_naming_the_formats(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`generate c4` is refused by argparse with the accepted list."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "c4", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 2
        expected = "invalid choice: 'c4' (choose from 'arxml', 'diagrams', 'needs', 'test-tables')"
        assert expected in capsys.readouterr().err
        assert not (tmp_path / "views").exists()

    def test_cli_generate_rst_is_a_usage_error_naming_the_formats(self, tmp_path: Path) -> None:
        """`generate rst` is argparse's usage error, exit 2, nothing written; through a subprocess."""
        manifest = _write_manifest(tmp_path)

        result = subprocess.run(
            [sys.executable, "-m", "swcraft_tools.cli", "generate", "rst", "--project", str(manifest)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        assert result.returncode == 2
        assert result.stdout == ""
        assert "invalid choice: 'rst' (choose from 'arxml', 'diagrams', 'needs', 'test-tables')" in result.stderr
        assert not (tmp_path / "dist").exists()

    def test_cli_generate_needs_calls_writer(self, tmp_path: Path, monkeypatch) -> None:
        """swcraft generate needs --project <yaml> -> generate_needs with the resolved paths and the manifest's name."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "needs", "--project", str(manifest)])

        with patch("swcraft_tools.cli.build_commands.generate_needs") as mock_generate:
            main()

        mock_generate.assert_called_once_with(
            str(tmp_path / "model"), str(tmp_path / "dist" / "needs.json"), "test-project"
        )

    def test_cli_generate_needs_writes_the_file_and_exits_0(self, tmp_path: Path) -> None:
        """Through a subprocess: exit 0, the one line on stdout, the file the reference build captured.

        A change detector, not a correctness proof: ``test_needs_export_consumer.py`` is the
        independent evidence.
        """
        shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", tmp_path / "model")
        shutil.copy(REPO_ROOT / "examples" / "basic" / "swcraft.yaml", tmp_path / "swcraft.yaml")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "swcraft_tools.cli",
                "generate",
                "needs",
                "--project",
                str(tmp_path / "swcraft.yaml"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        output = tmp_path / "dist" / "needs.json"
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"Generated needs.json: {output}\n"
        assert result.stderr == ""
        captured = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "reference_build" / "needs.json"
        assert output.read_bytes() == captured.read_bytes()

    def test_cli_generate_needs_refuses_a_collision_with_one_error_line_and_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A name collision: exit 1, one `Error:` line naming both sides, nothing written."""
        requirements = tmp_path / "model" / "requirements"
        requirements.mkdir(parents=True)
        for stem in ("A", "B"):
            document = {
                "id": "AR-1",
                "title": "AR-1",
                "description": "",
                "req_type": "req",
                "version": 1,
                "source": "architecture",
            }
            (requirements / f"{stem}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8", newline="\n")
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "needs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err == "Error: the id AR-1 would be taken by more than one need: requirement AR-1, requirement AR-1\n"
        assert str(tmp_path) not in err
        assert not (tmp_path / "dist").exists()

    def test_a_refused_generate_needs_removes_a_previous_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A refusal writes no new file and removes the previous one; stderr is one line."""
        requirements = tmp_path / "model" / "requirements"
        requirements.mkdir(parents=True)
        document = {
            "id": "AR-1",
            "title": "[[copy('id')]]",
            "description": "",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        }
        (requirements / "AR-1.yaml").write_text(yaml.safe_dump(document), encoding="utf-8", newline="\n")
        previous = tmp_path / "dist" / "needs.json"
        previous.parent.mkdir()
        previous.write_bytes(b"{}\n")
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "needs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not previous.exists()
        assert capsys.readouterr().err == (
            "Error: requirement AR-1 cannot be published: its title holds [[ once its backslashes, whitespace and "
            "non-printing characters are removed, which Sphinx-Needs reads as a dynamic function in a need's title "
            "or content\n"
        )

    def test_a_needs_output_naming_an_existing_directory_exits_1_and_leaves_it(self, tmp_path: Path) -> None:
        """``outputs.needs`` naming an existing directory: exit 1, one ``Error:`` line, the folder untouched."""
        requirements = tmp_path / "model" / "requirements"
        requirements.mkdir(parents=True)
        document = {
            "id": "AR-1",
            "title": "AR-1",
            "description": "",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        }
        (requirements / "AR-1.yaml").write_text(yaml.safe_dump(document), encoding="utf-8", newline="\n")
        folder = tmp_path / "dist" / "needs"
        folder.mkdir(parents=True)
        (folder / "index.rst").write_text("kept\n", encoding="utf-8", newline="\n")
        manifest = _write_manifest(
            tmp_path,
            {
                "name": "p",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"needs": "dist/needs"},
            },
        )

        result = subprocess.run(
            [sys.executable, "-m", "swcraft_tools.cli", "generate", "needs", "--project", str(manifest)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        assert result.returncode == 1, result.stdout
        assert result.stdout == ""
        # The OS's own error: ``[Errno 21]`` (EISDIR) on POSIX, ``[WinError 5]`` (access denied) on
        # Windows, where ``os.replace`` onto a directory is refused that way.
        assert result.stderr.startswith(("Error: [Errno ", "Error: [WinError ")), result.stderr
        assert result.stderr.count("\n") == 1, result.stderr
        assert sorted(path.name for path in folder.iterdir()) == ["index.rst"]
        assert sorted(path.name for path in folder.parent.iterdir()) == ["needs"]
        assert (folder / "index.rst").read_text(encoding="utf-8") == "kept\n"

    def test_a_needs_output_naming_the_arxml_is_refused_at_load_and_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Rule 4 of the loader: ``generate needs`` never reaches the ARXML it would overwrite."""
        (tmp_path / "model").mkdir()
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir()
        arxml.write_bytes(b"<AUTOSAR/>\n")
        manifest = _write_manifest(
            tmp_path,
            {
                "name": "p",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"needs": "dist/model.arxml"},
            },
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "needs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: Manifest field 'outputs.needs' resolves to ") and err.count("\n") == 1, err
        assert "which is the file 'outputs.arxml' names" in err
        assert arxml.read_bytes() == b"<AUTOSAR/>\n"

    @pytest.mark.parametrize(
        ("needs", "command", "what"),
        [
            pytest.param(
                "dist/build-report.md",
                ["validate", "--report"],
                "the build report in 'dist_root'",
                id="the-build-report",
            ),
            pytest.param(
                "views/index.html",
                ["generate", "diagrams"],
                "a file `swcraft generate diagrams` writes in 'views_root'",
                id="the-diagram-index",
            ),
            pytest.param(
                "dist/metrics/metrics.md",
                ["metrics", "--report"],
                "a file in the report directory of `swcraft metrics --report` in 'dist_root'",
                id="the-metrics-report",
            ),
        ],
    )
    def test_a_needs_output_naming_another_generators_file_is_refused_by_that_generator_too(
        self, tmp_path: Path, monkeypatch, capsys, needs: str, command: list[str], what: str
    ) -> None:
        """Rule 4 reaches the build report and the diagrams: the generator that owns the file refuses at load."""
        (tmp_path / "model").mkdir()
        kept = tmp_path / needs
        kept.parent.mkdir(parents=True)
        kept.write_text("kept\n", encoding="utf-8", newline="\n")
        manifest = _write_manifest(
            tmp_path,
            {
                "name": "p",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"needs": needs},
            },
        )
        monkeypatch.setattr("sys.argv", ["swcraft", *command, "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: Manifest field 'outputs.needs' resolves to ") and err.count("\n") == 1, err
        assert f"which is {what}" in err
        assert kept.read_text(encoding="utf-8") == "kept\n"

    def test_the_default_needs_output_resolves_to_dist_needs_json(self, tmp_path: Path) -> None:
        manifest = _write_manifest(
            tmp_path,
            {"name": "p", "schema_version": "1.0", "model_root": "model", "dist_root": "dist", "views_root": "views"},
        )

        assert require_project(str(manifest)).resolved_needs_path == str(tmp_path / "dist" / "needs.json")


# swcraft validate


class TestCliValidate:
    def test_cli_validate_requires_project(self, monkeypatch) -> None:
        """swcraft validate with no --project → SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "validate"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_validate_all_checks_pass(self, tmp_path: Path, monkeypatch) -> None:
        """Mock cross_layer consistency returning success → exits 0, generate_arxml called."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])

        success = ConsistencyResult(consistent=True, issues=[])

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch(
                "swcraft_tools.cli.build_commands.generate_arxml", return_value=_stub_generated_arxml(tmp_path)
            ) as mock_gen,
        ):
            main()  # must not raise SystemExit

        mock_gen.assert_called_once()

    def test_cli_validate_exits_1_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Mock validator returning failure → SystemExit(1), error printed."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])

        failure = ConsistencyResult(consistent=False, issues=["Task 'T' references unknown SWC 'Foo'."])

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=failure):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1

    def test_an_installed_layout_without_a_schema_prints_the_skip_note(self, tmp_path: Path) -> None:
        """An installed package has no default schema: with none configured, ``validate`` says XSD is skipped."""
        project_dir = tmp_path / "project"
        shutil.copytree(REPO_ROOT / "examples" / "basic", project_dir, ignore=shutil.ignore_patterns("dist", "views"))
        manifest = project_dir / "swcraft.yaml"

        result = run_without_a_checkout(
            "import sys\nfrom swcraft_tools.cli import main\n"
            f"sys.argv = ['swcraft', 'validate', '--project', {str(manifest)!r}]\nmain()\n",
            project_dir,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "XSD validation is skipped" in result.stdout
        assert "AUTOSAR_4-2-2.xsd" in result.stdout


# The target AUTOSAR version on the CLI


def _schema_location(arxml_path: Path) -> str:
    """The ``xsi:schemaLocation`` the generated file declares."""
    root = ET.parse(arxml_path).getroot()
    return root.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}schemaLocation", "")


def _version_project(tmp_path: Path, arxml_version: str | None) -> Path:
    """A one-SWC project, optionally declaring *arxml_version*. Returns the manifest."""
    data: dict[str, Any] = {
        "name": "versioned",
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
    }
    if arxml_version is not None:
        data["arxml_version"] = arxml_version
    manifest = _write_manifest(tmp_path, data)
    _write_swc(tmp_path / "model", "SpeedSensorSWC")
    return manifest


class TestCliArxmlVersion:
    """``swcraft generate arxml`` emits the project's declared version; ``--arxml-version`` overrides it for one run.

    Asserted on the produced file's ``xsi:schemaLocation``, which is what a downstream
    toolchain reads.
    """

    def test_manifest_version_is_used_when_the_flag_is_absent(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        main()

        assert _schema_location(tmp_path / "dist" / "model.arxml").endswith("AUTOSAR_4-3-0.xsd")

    def test_a_manifest_without_the_field_still_generates_4_2_2(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        main()

        assert _schema_location(tmp_path / "dist" / "model.arxml").endswith("AUTOSAR_4-2-2.xsd")

    def test_flag_overrides_the_manifest_for_one_run(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.2.2")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.3.0"],
        )

        main()

        assert _schema_location(tmp_path / "dist" / "model.arxml").endswith("AUTOSAR_4-3-0.xsd")
        # "for one run": the manifest is never rewritten by a generate.
        assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["arxml_version"] == "4.2.2"

    def test_overriding_the_manifest_version_warns_that_the_artifact_now_disagrees(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A whole-model ``--arxml-version`` run warns that ``dist/model.arxml`` now disagrees with the manifest."""
        manifest = _version_project(tmp_path, "4.2.2")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.3.0"],
        )

        main()

        err = capsys.readouterr().err
        assert "4.3.0" in err
        assert "4.2.2" in err
        assert "dist/model.arxml" in err
        assert str(tmp_path) not in err
        assert "arxml_version" in err

    def test_no_warning_when_the_flag_agrees_with_the_manifest(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.3.0"],
        )

        main()

        assert capsys.readouterr().err == ""

    def test_no_warning_for_a_component_export_at_another_version(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A component export writes its own file, never the manifest's: no warning."""
        manifest = _version_project(tmp_path, "4.2.2")
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--component",
                "SpeedSensorSWC",
                "--arxml-version",
                "4.3.0",
            ],
        )

        main()

        assert capsys.readouterr().err == ""

    def test_flag_overrides_downwards_too(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.2.2"],
        )

        main()

        assert _schema_location(tmp_path / "dist" / "model.arxml").endswith("AUTOSAR_4-2-2.xsd")

    def test_an_unsupported_flag_value_is_rejected(self, tmp_path: Path, monkeypatch) -> None:
        # argparse `choices`, so the rejection costs no generation attempt and
        # the usage line names every supported version.
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.5.0"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 2
        assert not (tmp_path / "dist" / "model.arxml").exists()

    def test_an_unsupported_manifest_version_fails_before_generating(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.5.0")
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not (tmp_path / "dist" / "model.arxml").exists()

    @pytest.mark.parametrize("fmt", ["diagrams", "needs"])
    def test_the_flag_applies_to_arxml_only(self, tmp_path: Path, monkeypatch, fmt: str) -> None:
        # The diagrams and the needs.json have no AUTOSAR version; accepting the flag there
        # would claim an effect the run cannot have.
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", fmt, "--project", str(manifest), "--arxml-version", "4.3.0"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not (tmp_path / "dist").exists()


class TestCliComponentExportArxmlVersion:
    """Component-scoped export targets the same version the whole-model build does.

    Asserted on the exported file's ``xsi:schemaLocation``.
    """

    def test_component_export_uses_the_manifest_version(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSensorSWC"],
        )

        main()

        exported = tmp_path / "dist" / "components" / "SpeedSensorSWC.arxml"
        assert _schema_location(exported).endswith("AUTOSAR_4-3-0.xsd")

    def test_component_export_without_the_field_still_emits_4_2_2(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSensorSWC"],
        )

        main()

        exported = tmp_path / "dist" / "components" / "SpeedSensorSWC.arxml"
        assert _schema_location(exported).endswith("AUTOSAR_4-2-2.xsd")

    def test_the_flag_overrides_the_manifest_for_a_component_export(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.2.2")
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--component",
                "SpeedSensorSWC",
                "--arxml-version",
                "4.3.0",
            ],
        )

        main()  # must not raise SystemExit

        exported = tmp_path / "dist" / "components" / "SpeedSensorSWC.arxml"
        assert _schema_location(exported).endswith("AUTOSAR_4-3-0.xsd")
        assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["arxml_version"] == "4.2.2"
        assert '<SD GID="DATA-TYPES">' in exported.read_text(encoding="utf-8")

    def test_batch_export_uses_the_manifest_version_for_every_file(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        _write_swc(tmp_path / "model", "SecondSWC")
        out_dir = tmp_path / "batch"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--all-components",
                "--output-dir",
                str(out_dir),
            ],
        )

        main()

        exported = sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix())
        assert [p.name for p in exported] == ["SecondSWC.arxml", "SpeedSensorSWC.arxml"]
        assert all(_schema_location(p).endswith("AUTOSAR_4-3-0.xsd") for p in exported)
        assert all('<SD GID="DATA-TYPES">' in p.read_text(encoding="utf-8") for p in exported)

    def test_the_flag_overrides_the_manifest_for_a_batch_export(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.2.2")
        out_dir = tmp_path / "batch"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--all-components",
                "--output-dir",
                str(out_dir),
                "--arxml-version",
                "4.3.0",
            ],
        )

        main()  # must not raise SystemExit

        assert _schema_location(out_dir / "SpeedSensorSWC.arxml").endswith("AUTOSAR_4-3-0.xsd")

    def test_the_version_is_named_in_the_output(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The exported version is named in the output: with two targets a log must say which one."""
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSensorSWC"],
        )

        main()

        assert "AUTOSAR 4.3.0" in capsys.readouterr().out

    def test_an_unsupported_manifest_version_fails_before_exporting(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.5.0")
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSensorSWC"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not (tmp_path / "dist" / "components").exists()


@pytest.mark.usefixtures("_no_local_schema")
class TestCliValidateArxmlVersion:
    """``swcraft validate`` generates at the project's declared version.

    Its XSD-skip note names *that* version's schema file.
    """

    def test_validate_generates_at_the_manifest_version(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success):
            main()

        assert _schema_location(tmp_path / "dist" / "model.arxml").endswith("AUTOSAR_4-3-0.xsd")

    def test_the_xsd_skip_note_names_the_project_s_schema_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success):
            main()

        out = capsys.readouterr().out
        # Never AUTOSAR_4-2-2.xsd here: naming the wrong schema is worse than naming none,
        # since the reader is told which licensed file to obtain.
        assert "AUTOSAR_4-3-0.xsd" in out
        assert "AUTOSAR_4-2-2.xsd" not in out
        assert "skipped" in out

    def test_the_note_still_names_4_2_2_for_a_default_project(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success):
            main()

        assert "AUTOSAR_4-2-2.xsd" in capsys.readouterr().out

    def test_validate_really_validates_the_arxml_it_generated(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``validate`` checks the file it generated, rather than only announcing such a check."""
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", wraps=arxml_validation.check_arxml) as mock_check,
        ):
            main()

        mock_check.assert_called_once_with(str(tmp_path / "dist" / "model.arxml"))
        assert "Validation passed." in capsys.readouterr().out

    def test_the_note_is_replaced_by_the_schema_name_when_xsd_validation_really_ran(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        xsd_outcome = ValidationOutcome(
            valid=True,
            mode=MODE_XSD,
            version=ArxmlVersion.V4_2_2,
            schema_path="/schemas/AUTOSAR_4-2-2.xsd",
        )

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=xsd_outcome),
        ):
            main()

        out = capsys.readouterr().out
        assert "/schemas/AUTOSAR_4-2-2.xsd" in out
        assert "skipped" not in out
        assert "Validation passed." in out

    def test_an_invalid_generated_arxml_fails_the_gate(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        failed = ValidationOutcome(valid=False, mode=MODE_XSD, schema_path="/schemas/AUTOSAR_4-2-2.xsd")

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=failed),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "failed XSD validation" in captured.err
        assert "Validation passed." not in captured.out

    def test_a_failed_schema_check_leaves_the_generated_arxml_on_disk(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A failed schema check exits 1 with the refused ARXML still on disk.

        "A failed build leaves no outputs" is a rule about the writer, not a promise that
        ``validate`` cleans up: it generates, announces the file, and only then checks it. The
        guide and the generated checks page describe this; if the command starts removing the
        file, this fails and those sentences are rewritten deliberately.
        """
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        failed = ValidationOutcome(valid=False, mode=MODE_XSD, schema_path="/schemas/AUTOSAR_4-2-2.xsd")

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=failed),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert arxml.is_file(), "the refused ARXML is expected to survive the failure, and did not"
        assert str(arxml) in capsys.readouterr().out, "the run must name the file it left behind"

    def test_a_failing_model_check_generates_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """A model finding stops ``validate`` before ``generate_arxml``, so nothing new is on disk."""
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        failure = ConsistencyResult(consistent=False, issues=["Task 'T' references unknown SWC 'X'."])

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=failure),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert not arxml.exists(), "a model finding must stop the run before anything is generated"

    def test_an_unusable_schema_is_reported_as_a_toolchain_problem(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A schema that will not compile is reported as a toolchain problem, not a broken model.

        The realistic cause is ``schemas/AUTOSAR_4-2-2.xsd`` holding an SSO login page that a
        download saved.
        """
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        unusable = ValidationOutcome(
            valid=False,
            mode=MODE_XSD,
            schema_path="/schemas/AUTOSAR_4-2-2.xsd",
            messages=("Error: the XML schema /schemas/AUTOSAR_4-2-2.xsd could not be compiled",),
            schema_error=True,
        )

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=unusable),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "/schemas/AUTOSAR_4-2-2.xsd is unusable" in err
        assert "not a problem with the model" in err
        assert "failed XSD validation" not in err

    def test_validate_names_the_file_and_version_it_produced(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``validate`` names the file and version it produced.

        It can overwrite an ARXML an earlier ``--arxml-version`` run wrote at another version,
        so a log must say which file at which version.
        """
        manifest = _version_project(tmp_path, "4.3.0")
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success):
            main()

        out = capsys.readouterr().out
        assert f"ARXML generated: {tmp_path / 'dist' / 'model.arxml'} (AUTOSAR 4.3.0)" in out

    def test_the_generated_line_precedes_the_validation_verdict(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """It is printed before the check, so a failing gate still names the artifact."""
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        failed = ValidationOutcome(valid=False, mode=MODE_XSD, schema_path="/schemas/AUTOSAR_4-2-2.xsd")

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=failed),
            pytest.raises(SystemExit),
        ):
            main()

        out = capsys.readouterr().out
        assert "ARXML generated:" in out
        assert out.index("ARXML generated:") < out.index("/schemas/AUTOSAR_4-2-2.xsd")


@pytest.mark.usefixtures("_no_local_schema")
class TestCliKeepsOnePreviousArxml:
    """``generate arxml`` and ``validate`` keep **one** previous ARXML.

    A refused model touches nothing, a successful run over a *different* file leaves it as
    ``model_old.arxml``, and a run that would write the same bytes moves nothing -- which keeps
    the ``_old`` file the previous **build's** when ``validate`` regenerates what ``generate
    arxml`` just wrote, the last step of both build graphs.
    """

    @staticmethod
    def _run(monkeypatch, manifest: Path, *command: str) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", *command, "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])
        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success):
            main()

    @staticmethod
    def _fresh_build(tmp_path: Path) -> bytes:
        """The bytes a build of the model as it stands writes, made outside ``dist/``."""
        from swcraft_tools.writer import generate_arxml

        expected = tmp_path / "expected" / "model.arxml"
        generate_arxml(str(tmp_path / "model"), str(expected))
        return expected.read_bytes()

    def test_a_successful_generate_over_a_different_arxml_keeps_it_as_old(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")

        self._run(monkeypatch, manifest, "generate", "arxml")

        old = tmp_path / "dist" / "model_old.arxml"
        assert old.read_bytes() == b"<previous build/>\n"
        assert arxml.read_bytes() == self._fresh_build(tmp_path)
        out = capsys.readouterr().out
        assert out.count("Previous ARXML kept:") == 1
        assert f"Previous ARXML kept: {old}" in out

    def test_a_second_run_with_the_same_model_moves_nothing_and_says_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        self._run(monkeypatch, manifest, "generate", "arxml")
        first = (tmp_path / "dist" / "model.arxml").read_bytes()
        capsys.readouterr()

        self._run(monkeypatch, manifest, "generate", "arxml")

        assert (tmp_path / "dist" / "model.arxml").read_bytes() == first
        assert not (tmp_path / "dist" / "model_old.arxml").exists()
        assert "Previous ARXML kept" not in capsys.readouterr().out

    def test_validate_after_generate_leaves_the_old_file_the_previous_build(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``generate arxml`` then ``validate``: byte-identical output moves nothing aside."""
        manifest = _version_project(tmp_path, None)
        self._run(monkeypatch, manifest, "generate", "arxml")
        first_build = (tmp_path / "dist" / "model.arxml").read_bytes()
        _write_swc(tmp_path / "model", "BrakeSWC")
        self._run(monkeypatch, manifest, "generate", "arxml")
        old = tmp_path / "dist" / "model_old.arxml"
        assert old.read_bytes() == first_build
        capsys.readouterr()

        self._run(monkeypatch, manifest, "validate")

        assert old.read_bytes() == first_build
        assert (tmp_path / "dist" / "model.arxml").read_bytes() == self._fresh_build(tmp_path)
        assert "Previous ARXML kept" not in capsys.readouterr().out

    def test_validate_over_a_different_arxml_announces_the_move_once(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")

        self._run(monkeypatch, manifest, "validate")

        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<previous build/>\n"
        assert capsys.readouterr().out.count("Previous ARXML kept:") == 1

    def test_a_third_run_after_a_model_change_replaces_the_old_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """An existing ``_old`` file is replaced and announced once.

        The state compared carries the inode, so a replaced file is a changed one whatever its mtime.
        """
        manifest = _version_project(tmp_path, None)
        self._run(monkeypatch, manifest, "generate", "arxml")
        _write_swc(tmp_path / "model", "BrakeSWC")
        self._run(monkeypatch, manifest, "generate", "arxml")
        second_build = (tmp_path / "dist" / "model.arxml").read_bytes()
        _write_swc(tmp_path / "model", "SteeringSWC")
        capsys.readouterr()

        self._run(monkeypatch, manifest, "generate", "arxml")

        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == second_build
        assert sorted(entry.name for entry in (tmp_path / "dist").iterdir()) == ["model.arxml", "model_old.arxml"]
        assert capsys.readouterr().out.count("Previous ARXML kept:") == 1

    def test_a_refused_model_touches_nothing_in_dist(self, tmp_path: Path, monkeypatch) -> None:
        """A refused model with an earlier ARXML: the refusal comes before the move, nothing changes."""
        manifest = _version_project(tmp_path, None)
        _write_swc(tmp_path / "model", "SilentSWC", handle_termination_and_restart=None)
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")

        for command in (("generate", "arxml"), ("validate",)):
            monkeypatch.setattr("sys.argv", ["swcraft", *command, "--project", str(manifest)])
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

        assert arxml.read_bytes() == b"<previous build/>\n"
        assert sorted(entry.name for entry in (tmp_path / "dist").iterdir()) == ["model.arxml"]

    def test_an_unusual_arxml_name_derives_its_old_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_manifest(
            tmp_path,
            {
                "name": "unusual",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/Vehicle.System.arxml", "needs": "dist/needs.json"},
            },
        )
        _write_swc(tmp_path / "model", "SpeedSensorSWC")
        arxml = tmp_path / "dist" / "Vehicle.System.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")

        self._run(monkeypatch, manifest, "generate", "arxml")

        old = tmp_path / "dist" / "Vehicle.System_old.arxml"
        assert old.read_bytes() == b"<previous build/>\n"
        assert f"Previous ARXML kept: {old}" in capsys.readouterr().out

    def test_a_component_export_written_twice_keeps_no_old_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Only the whole-model ARXML is moved aside; a per-component export never is."""
        manifest = _version_project(tmp_path, None)
        exported = tmp_path / "dist" / "components" / "SpeedSensorSWC.arxml"
        exported.parent.mkdir(parents=True)
        exported.write_bytes(b"<previous export/>\n")

        for _ in range(2):
            self._run(monkeypatch, manifest, "generate", "arxml", "--component", "SpeedSensorSWC")

        assert sorted(entry.name for entry in exported.parent.iterdir()) == ["SpeedSensorSWC.arxml"]
        assert "Previous ARXML kept" not in capsys.readouterr().out

    def test_a_failing_move_leaves_the_old_file_and_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A move the filesystem refuses raises before the write: the old file keeps its name and bytes."""
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")

        def refuse(self: Path, target: Any) -> Path:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "replace", refuse)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert arxml.read_bytes() == b"<previous build/>\n"
        assert sorted(entry.name for entry in (tmp_path / "dist").iterdir()) == ["model.arxml"]
        captured = capsys.readouterr()
        assert "Permission denied" in captured.err
        assert "Previous ARXML kept" not in captured.out


# swcraft check-model


class TestCliCheckModel:
    """swcraft check-model — Python-level checks only, no artifact generation.

    ``validate`` cannot serve the moon build graph: it generates ``dist/model.arxml``, which
    would race with ``tools:generate-arxml`` writing it in parallel.
    """

    def test_cli_check_model_requires_project(self, monkeypatch) -> None:
        """swcraft check-model with no --project → SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_check_model_exits_0_on_success_and_writes_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """Consistent model → exit 0; no dist/ or views/ artifacts are created."""
        manifest = _write_manifest(tmp_path)
        (tmp_path / "model").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        success = ConsistencyResult(consistent=True, issues=[])
        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success) as mock_check,
            patch("swcraft_tools.cli.build_commands.generate_arxml") as mock_gen,
        ):
            main()  # must not raise SystemExit

        mock_check.assert_called_once()
        mock_gen.assert_not_called()
        assert not (tmp_path / "dist").exists()
        assert not (tmp_path / "views").exists()

    def test_cli_check_model_exits_1_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        """Inconsistent model → SystemExit(1), issues printed, nothing written."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        failure = ConsistencyResult(
            consistent=False,
            issues=["Task 'Task_10ms' references unknown SWC 'Missing'."],
        )
        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=failure),
            patch("swcraft_tools.cli.build_commands.generate_arxml") as mock_gen,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_gen.assert_not_called()
        assert not (tmp_path / "dist").exists()

    def test_cli_check_model_real_broken_task_exits_1_end_to_end(self, tmp_path: Path, monkeypatch) -> None:
        """Unmocked: a real project with a task naming an unknown SWC fails the real check."""
        manifest = _write_manifest(tmp_path)
        tasks_dir = tmp_path / "model" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "Broken_Task.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "Broken_Task",
                    "period": 0.01,
                    "priority": 5,
                    "runnables": [{"swc": "MissingSWC", "runnable": "SomeRun"}],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not (tmp_path / "dist").exists()

    def test_cli_check_model_healthy_example_model_exits_0(self, monkeypatch) -> None:
        """The real examples/basic model passes check-model with no artifact writes."""
        repo_root = Path(__file__).resolve().parents[3]
        manifest = repo_root / "examples" / "basic" / "swcraft.yaml"
        if not manifest.exists():
            pytest.skip("examples/basic/swcraft.yaml not present")
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        try:
            main()  # must not raise SystemExit
        except SystemExit as exc:
            pytest.fail(f"check-model exited with code {exc.code} on healthy example model")


# The warning class on the two gates


#: The two messages a warnings-only result carries below. The result is built by hand and the composer
#: patched to return it, so these cases hold whatever the shipped `WARNING_RULES` names (gated by
#: `test_model_contract_conformance.py`); the CLI reads `issues` and `warnings`, never `severity`.
_WARNING_RULE = "trace.requirement-unallocated"
_WARNING_MESSAGES = (
    "Requirement 'REQ_FIRST' is satisfied by no element: add `satisfies: [REQ_FIRST@1]` on the component, "
    "port, runnable, composition, interface, task or hardware element that realizes it.",
    "Requirement 'REQ_SECOND' is satisfied by no element: add `satisfies: [REQ_SECOND@1]` on the component, "
    "port, runnable, composition, interface, task or hardware element that realizes it.",
)
_ERROR_MESSAGE = "Task 'Task_10ms' references unknown SWC 'Missing'."


def _warning_findings() -> tuple[ConsistencyIssue, ...]:
    return tuple(ConsistencyIssue(rule=_WARNING_RULE, message=message) for message in _WARNING_MESSAGES)


def _warnings_only_result() -> ConsistencyResult:
    """A consistent result (no error finding) that carries two warnings."""
    return ConsistencyResult(consistent=True, issues=[], records=(), warnings=_warning_findings())


def _errors_and_warnings_result() -> ConsistencyResult:
    """One error beside the two warnings: `consistent`/`issues`/`records` are the error half."""
    error = ConsistencyIssue(rule="task.unknown-swc", message=_ERROR_MESSAGE)
    return ConsistencyResult(consistent=False, issues=[error.message], records=(error,), warnings=_warning_findings())


_WARNING_LINES = "".join(f"  WARNING: {message}\n" for message in _WARNING_MESSAGES)
#: The total line after the findings: both classes counted, for the two-warning result
#: and for the one-error-two-warning result.
_WARNINGS_TOTAL = "0 error(s), 2 warning(s).\n"
_ERRORS_AND_WARNINGS_TOTAL = "1 error(s), 2 warning(s).\n"
#: Each gate's unchanged success line, keyed by command so a swapped line cannot pass.
_SUCCESS_LINE = {"validate": "Validation passed.", "check-model": "Validation passed (no artifacts generated)."}


class TestTheWarningClassOnTheTwoGates:
    """`check-model` and `validate` list warnings and refuse on errors alone.

    Both classes go to stderr, every `ERROR:` line before every `WARNING:` line; a
    warnings-only run prints the command's success line and exits 0, and `validate` then
    generates; one total line counts both classes after the findings, only when there is
    one; `--warnings-as-errors` keeps the `WARNING:` prefix, adds one `Error:` summary line
    and exits 1 before `validate` generates anything, and is declared on these two commands
    only. Every result comes from a patched composer.
    """

    def test_check_model_lists_warnings_on_stderr_and_exits_0_with_the_success_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_warnings_only_result()
        ):
            main()  # must not raise SystemExit

        captured = capsys.readouterr()
        assert captured.err == _WARNING_LINES + _WARNINGS_TOTAL
        assert captured.out == _SUCCESS_LINE["check-model"] + "\n"
        assert not (tmp_path / "dist").exists()

    def test_validate_generates_and_checks_the_arxml_on_a_warnings_only_result(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        arxml = tmp_path / "dist" / "model.arxml"

        with (
            patch(
                "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_warnings_only_result()
            ),
            patch("swcraft_tools.cli.build_commands.check_arxml", wraps=arxml_validation.check_arxml) as mock_check,
        ):
            main()  # must not raise SystemExit

        mock_check.assert_called_once_with(str(arxml))
        assert arxml.exists()
        captured = capsys.readouterr()
        assert captured.err == _WARNING_LINES + _WARNINGS_TOTAL
        lines = captured.out.splitlines()
        assert lines[0].startswith(f"ARXML generated: {arxml} ")
        assert lines[-1] == _SUCCESS_LINE["validate"]
        assert "Error" not in captured.out

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_errors_and_warnings_are_refused_for_the_errors_with_both_lists_errors_first(
        self, command: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", command, "--project", str(manifest)])

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_errors_and_warnings_result()
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err == f"  ERROR: {_ERROR_MESSAGE}\n" + _WARNING_LINES + _ERRORS_AND_WARNINGS_TOTAL
        assert captured.out == ""
        assert not (tmp_path / "dist").exists()

    def test_check_model_with_the_flag_refuses_a_warnings_only_result_with_the_summary_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest), "--warnings-as-errors"])

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_warnings_only_result()
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        # The prefix states the rule's class, the summary line the verdict.
        assert captured.err == (
            _WARNING_LINES + _WARNINGS_TOTAL + "Error: 2 warning(s) treated as errors (--warnings-as-errors).\n"
        )
        assert captured.out == ""
        assert not (tmp_path / "dist").exists()

    def test_validate_with_the_flag_generates_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest), "--warnings-as-errors"])

        with (
            patch(
                "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_warnings_only_result()
            ),
            patch("swcraft_tools.cli.build_commands.generate_arxml", wraps=generate_arxml) as mock_gen,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_gen.assert_not_called()
        assert not (tmp_path / "dist").exists()
        captured = capsys.readouterr()
        assert captured.err == (
            _WARNING_LINES + _WARNINGS_TOTAL + "Error: 2 warning(s) treated as errors (--warnings-as-errors).\n"
        )
        assert captured.out == ""

    def test_validate_with_the_flag_moves_no_previous_arxml_aside(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The refusal is before generation, so the move aside never starts."""
        manifest = _version_project(tmp_path, None)
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_bytes(b"<previous build/>\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest), "--warnings-as-errors"])

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_warnings_only_result()
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["model.arxml"]
        assert arxml.read_bytes() == b"<previous build/>\n"
        assert "Previous ARXML kept" not in capsys.readouterr().out

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_the_flag_changes_nothing_without_a_warning(
        self, command: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """No finding, no total line and no summary line: the flag is inert on a clean result."""
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", command, "--project", str(manifest), "--warnings-as-errors"])

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency",
            return_value=ConsistencyResult(consistent=True, issues=[]),
        ):
            main()  # must not raise SystemExit

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.splitlines()[-1] == _SUCCESS_LINE[command]

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_errors_beside_warnings_under_the_flag_carry_no_summary_line(
        self, command: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The errors already refuse; the summary line names a verdict the flag made, and here it made none."""
        manifest = _version_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", command, "--project", str(manifest), "--warnings-as-errors"])

        with (
            patch(
                "swcraft_tools.cli.build_commands.check_cross_layer_consistency",
                return_value=_errors_and_warnings_result(),
            ),
            patch("swcraft_tools.cli.build_commands.generate_arxml", wraps=generate_arxml) as mock_gen,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_gen.assert_not_called()
        assert not (tmp_path / "dist").exists()
        captured = capsys.readouterr()
        assert captured.err == f"  ERROR: {_ERROR_MESSAGE}\n" + _WARNING_LINES + _ERRORS_AND_WARNINGS_TOTAL
        assert captured.out == ""

    def test_the_flag_on_generate_is_argparse_s_usage_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest), "--warnings-as-errors"]
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 2
        assert "unrecognized arguments: --warnings-as-errors" in capsys.readouterr().err
        assert not (tmp_path / "dist").exists()

    def test_the_flag_is_declared_on_the_two_gates_and_no_other_command(self) -> None:
        """The flag is declared on `validate` and `check-model` only, with one help sentence.

        Read off each subcommand's rendered help (whitespace-normalised) through the same
        `commands()` the reference page is built from, so no argparse private decides the list.
        """
        parser = build_parser()
        sentence = " ".join(build_commands._WARNINGS_AS_ERRORS_HELP.split())
        declaring: list[str] = []
        for name, subparser, _summary in guide_reference_cli.commands(parser):
            rendered = " ".join(subparser.format_help().split())
            if "--warnings-as-errors" in rendered:
                declaring.append(name)
                assert sentence in rendered, f"{name} renders another sentence for the flag"
        assert declaring == ["validate", "check-model"]
        for name in declaring:
            assert parser.parse_args([name, "--project", "x", "--warnings-as-errors"]).warnings_as_errors is True
            assert parser.parse_args([name, "--project", "x"]).warnings_as_errors is False
        # One sentence, true read from either command's help: the run that refuses
        # generates nothing whichever gate it is (`check-model` never generates).
        assert build_commands._WARNINGS_AS_ERRORS_HELP == (
            "treat every warning as an error: the warnings are still listed as warnings, the run then exits 1 "
            "and generates nothing — for a release gate"
        )


# swcraft validate --report -- the build report beside the ARXML

_GENERATED_REPORT = "Generated report: "
_CONTROL_CHARACTER_WARNING = (
    "Warning: the report was written, but its path holds a control character, so it is not printed"
)


def _run_validate(
    manifest: Path,
    monkeypatch,
    capsys,
    *extra: str,
    findings: ConsistencyResult | None = None,
    outcome: ValidationOutcome | None = None,
) -> tuple[int, str, str]:
    """``swcraft validate`` over *manifest* with *extra*; the exit code, stdout and stderr.

    The model check is patched to *findings* (no finding by default), as the other ``validate``
    classes patch it; the ARXML check is the real one unless *outcome* stands in for it.
    """
    monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest), *extra])
    result = findings if findings is not None else ConsistencyResult(consistent=True, issues=[])
    check = {"return_value": outcome} if outcome is not None else {"wraps": arxml_validation.check_arxml}
    code = 0
    with (
        patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=result),
        patch("swcraft_tools.cli.build_commands.check_arxml", **check),
    ):
        try:
            main()
        except SystemExit as exc:
            code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


#: The failing outcomes a run can be given (the passing one is ``build_report_support.PASSED``).
_INVALID = ValidationOutcome(valid=False, mode=MODE_XSD, schema_path=SCHEMA_PATH)
_UNUSABLE = ValidationOutcome(
    valid=False, mode=MODE_XSD, schema_path=SCHEMA_PATH, messages=("broken",), schema_error=True
)


@pytest.mark.usefixtures("_no_local_schema")
class TestValidateWritesTheBuildReport:
    """``validate --report`` writes ``<dist_root>/build-report.md`` exactly when the run generated the ARXML.

    The report is written after the ARXML check returns, in every outcome, and named by one
    ``Generated report:`` line before the run's final line; a run refused before generation
    writes none and leaves an earlier report as it is; the report never moves the exit code --
    a report that cannot be computed or written is one ``Warning:`` line naming no path.
    Without the flag the run is unchanged.
    """

    def test_a_passing_run_writes_the_report_and_still_ends_in_the_success_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)

        code, out, _ = _run_validate(manifest, monkeypatch, capsys, "--report")

        report = tmp_path / "dist" / build_report.BUILD_REPORT_FILE
        assert code == 0
        assert report.is_file()
        lines = out.splitlines()
        assert lines[-1] == _SUCCESS_LINE["validate"]
        assert [line for line in lines if line.startswith(_GENERATED_REPORT)] == [f"{_GENERATED_REPORT}{report}"]
        # Written once the check has returned: after the ARXML is named, before the check's own lines.
        assert lines[0].startswith("ARXML generated: ")
        assert lines.index(f"{_GENERATED_REPORT}{report}") < lines.index(next(line for line in lines if "XSD" in line))

    def test_the_structural_check_s_sentence_when_no_licensed_schema_is_configured(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The degraded mode is named, never implied."""
        manifest = _version_project(tmp_path, None)

        _run_validate(manifest, monkeypatch, capsys, "--report")

        text = (tmp_path / "dist" / build_report.BUILD_REPORT_FILE).read_text(encoding="utf-8")
        structural = build_report.VERDICT_STRUCTURAL_PASSED.format(schema="`AUTOSAR_4-2-2.xsd`")
        assert report_section(text, build_report.HEADING_VERDICT) == [f"{structural} {build_report.NO_WARNING}"]

    def test_the_arxml_bytes_are_those_of_the_same_run_without_the_flag(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        plain, flagged = tmp_path / "plain", tmp_path / "flagged"
        _run_validate(_version_project(plain, None), monkeypatch, capsys)
        _run_validate(_version_project(flagged, None), monkeypatch, capsys, "--report")

        assert (plain / "dist" / "model.arxml").read_bytes() == (flagged / "dist" / "model.arxml").read_bytes()
        assert not (plain / "dist" / build_report.BUILD_REPORT_FILE).exists()

    @pytest.mark.parametrize(
        ("findings", "outcome", "expected_code"),
        [
            pytest.param(ConsistencyResult(consistent=True, issues=[]), None, 0, id="passing"),
            pytest.param(_warnings_only_result(), None, 0, id="warnings"),
            pytest.param(ConsistencyResult(consistent=True, issues=[]), _INVALID, 1, id="invalid"),
        ],
    )
    def test_the_flag_adds_one_line_and_removes_none(
        self, tmp_path: Path, monkeypatch, capsys, findings, outcome, expected_code
    ) -> None:
        manifest = _version_project(tmp_path, None)
        runs = {}
        for extra in ((), ("--report",)):
            shutil.rmtree(tmp_path / "dist", ignore_errors=True)
            runs[extra] = _run_validate(manifest, monkeypatch, capsys, *extra, findings=findings, outcome=outcome)
            if not extra:
                assert not (tmp_path / "dist" / build_report.BUILD_REPORT_FILE).exists()

        plain_code, plain_out, plain_err = runs[()]
        code, out, err = runs[("--report",)]
        assert plain_code == code == expected_code
        assert err == plain_err
        report_line = f"{_GENERATED_REPORT}{tmp_path / 'dist' / build_report.BUILD_REPORT_FILE}\n"
        assert report_line in out
        assert out.replace(report_line, "", 1) == plain_out

    def test_the_warnings_are_listed_in_the_report(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)

        code, _, _ = _run_validate(manifest, monkeypatch, capsys, "--report", findings=_warnings_only_result())

        assert code == 0
        text = (tmp_path / "dist" / build_report.BUILD_REPORT_FILE).read_text(encoding="utf-8")
        assert report_section(text, build_report.HEADING_FINDINGS) == [
            "| Rule | Finding |",
            "| --- | --- |",
            *(f"| `{_WARNING_RULE}` | {markdown_cells.inline_literal(message)} |" for message in _WARNING_MESSAGES),
        ]
        assert report_section(text, build_report.HEADING_VERDICT)[0].endswith("2 warning(s).")

    def test_warnings_as_errors_with_a_warning_writes_no_report(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)

        code, out, _ = _run_validate(
            manifest, monkeypatch, capsys, "--report", "--warnings-as-errors", findings=_warnings_only_result()
        )

        assert code == 1
        assert not (tmp_path / "dist").exists()
        assert _GENERATED_REPORT not in out

    def test_an_error_refuses_with_no_report_and_leaves_an_earlier_one_untouched(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        earlier = tmp_path / "dist" / build_report.BUILD_REPORT_FILE
        earlier.parent.mkdir(parents=True)
        earlier.write_bytes(b"# An earlier report\n")

        code, out, _ = _run_validate(manifest, monkeypatch, capsys, "--report", findings=_errors_and_warnings_result())

        assert code == 1
        assert earlier.read_bytes() == b"# An earlier report\n"
        assert sorted(path.name for path in earlier.parent.iterdir()) == [build_report.BUILD_REPORT_FILE]
        assert out == ""

    @pytest.mark.parametrize(
        ("outcome", "error", "verdict"),
        [
            pytest.param(
                _INVALID,
                "failed XSD validation",
                build_report.VERDICT_FAILED.format(mode=MODE_XSD, arxml="`dist/model.arxml`"),
                id="invalid",
            ),
            pytest.param(
                _UNUSABLE,
                "is unusable",
                build_report.VERDICT_NOT_VALIDATED.format(mode=MODE_XSD, schema="`AUTOSAR_4-2-2.xsd`"),
                id="schema-unusable",
            ),
        ],
    )
    def test_a_failed_check_writes_the_report_and_keeps_its_error_line(
        self, tmp_path: Path, monkeypatch, capsys, outcome, error, verdict
    ) -> None:
        """The refused ARXML stays on disk, and the report says so."""
        manifest = _version_project(tmp_path, None)

        code, out, err = _run_validate(manifest, monkeypatch, capsys, "--report", outcome=outcome)

        assert code == 1
        assert (tmp_path / "dist" / "model.arxml").is_file()
        assert error in err and err.splitlines()[-1].startswith("Error: ")
        assert _SUCCESS_LINE["validate"] not in out
        text = (tmp_path / "dist" / build_report.BUILD_REPORT_FILE).read_text(encoding="utf-8")
        assert report_section(text, build_report.HEADING_VERDICT) == [f"{verdict} {build_report.NO_WARNING}"]

    @pytest.mark.parametrize(
        ("target", "raised", "reason"),
        [
            pytest.param(
                "write_build_report",
                lambda tmp: OSError(13, "Permission denied", str(tmp / "dist" / "build-report.md.tmp")),
                "Permission denied",
                id="write",
            ),
            pytest.param("write_build_report", lambda tmp: OSError(), "OSError", id="write-without-strerror"),
            pytest.param(
                "compute_build_report",
                lambda tmp: ValueError(f"cannot read {tmp / 'model' / 'hardware' / 'CPU.yaml'}"),
                "ValueError",
                id="compute",
            ),
        ],
    )
    @pytest.mark.parametrize(
        ("outcome", "expected_code"),
        [pytest.param(PASSED, 0, id="passing"), pytest.param(_INVALID, 1, id="invalid")],
    )
    def test_a_report_that_fails_is_a_warning_line_and_never_moves_the_exit_code(
        self, tmp_path: Path, monkeypatch, capsys, target, raised, reason, outcome, expected_code
    ) -> None:
        manifest = _version_project(tmp_path, None)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise raised(tmp_path)

        monkeypatch.setattr(build_report, target, fail)
        code, out, err = _run_validate(manifest, monkeypatch, capsys, "--report", outcome=outcome)

        assert code == expected_code
        warning = f"Warning: the build report was not written: {reason}"
        assert warning in err.splitlines()
        assert str(tmp_path) not in warning and ".tmp" not in warning
        assert "Traceback" not in err
        assert _GENERATED_REPORT not in out
        assert not (tmp_path / "dist" / build_report.BUILD_REPORT_FILE).exists()

    @pytest.mark.parametrize(
        ("outcome", "expected_code"),
        [pytest.param(PASSED, 0, id="passing"), pytest.param(_INVALID, 1, id="invalid")],
    )
    def test_a_report_path_with_a_newline_prints_no_report_line_and_never_moves_the_exit_code(
        self, tmp_path: Path, monkeypatch, capsys, outcome, expected_code
    ) -> None:
        """A ``dist_root`` holding a newline would print a second, forged ``Generated report:`` line."""
        manifest = _version_project(tmp_path, None)
        forged = tmp_path / "dist\nGenerated report: /elsewhere" / build_report.BUILD_REPORT_FILE
        monkeypatch.setattr(build_report, "write_build_report", lambda *_args: forged)

        code, out, err = _run_validate(manifest, monkeypatch, capsys, "--report", outcome=outcome)

        assert code == expected_code
        assert _GENERATED_REPORT not in out
        assert _CONTROL_CHARACTER_WARNING in err.splitlines()

    @pytest.mark.parametrize("character", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85"])
    def test_the_shared_report_line_refuses_every_control_character(self, capsys, character: str) -> None:
        report_files.print_generated_report(f"/p/dist{character}x/report.md")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"{_CONTROL_CHARACTER_WARNING}\n"

    def test_the_shared_report_line_prints_a_plain_path_as_it_is(self, capsys) -> None:
        plain = Path("/p/Bericht-été x/report.md")
        report_files.print_generated_report(plain)
        assert capsys.readouterr().out == f"{_GENERATED_REPORT}{plain}\n"

    def test_a_report_path_that_cannot_be_replaced_is_a_warning_line_and_leaves_no_temporary_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The real write, not a stub: ``dist/build-report.md`` is a directory, so the replace fails."""
        manifest = _version_project(tmp_path, None)
        blocker = tmp_path / "dist" / build_report.BUILD_REPORT_FILE
        (blocker / "inside").mkdir(parents=True)

        code, out, err = _run_validate(manifest, monkeypatch, capsys, "--report", outcome=PASSED)

        assert code == 0
        assert out.splitlines()[-1] == _SUCCESS_LINE["validate"]
        assert _GENERATED_REPORT not in out
        warnings = [line for line in err.splitlines() if line.startswith("Warning: the build report was not written: ")]
        assert len(warnings) == 1
        assert str(tmp_path) not in warnings[0] and ".tmp" not in warnings[0]
        assert blocker.is_dir()
        assert sorted(path.name for path in blocker.parent.iterdir()) == sorted(
            [build_report.BUILD_REPORT_FILE, "model.arxml"]
        )

    def test_a_second_run_after_a_component_is_added_names_its_elements(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """End to end: the first run has no previous ARXML; the second compares against the first's."""
        manifest = _version_project(tmp_path, None)
        report = tmp_path / "dist" / build_report.BUILD_REPORT_FILE

        assert _run_validate(manifest, monkeypatch, capsys, "--report", outcome=PASSED)[0] == 0
        first = report.read_text(encoding="utf-8")
        assert report_section(first, build_report.HEADING_VERDICT) == [
            f"{build_report.VERDICT_XSD_PASSED.format(schema='`AUTOSAR_4-2-2.xsd`')} {build_report.NO_WARNING}"
        ]
        assert report_section(first, build_report.HEADING_ARXML) == [
            build_report.ARXML_NO_PREVIOUS.format(old="`dist/model_old.arxml`")
        ]

        _write_swc(tmp_path / "model", "BrakeSWC")
        assert _run_validate(manifest, monkeypatch, capsys, "--report", outcome=PASSED)[0] == 0
        section = report_section(report.read_text(encoding="utf-8"), build_report.HEADING_ARXML)
        rows = section[3:]
        assert section[:3] == [
            build_report.ARXML_CHANGED.format(
                count=len(rows), old="`dist/model_old.arxml`", added=len(rows), removed=0, changed=0
            ),
            "| Element | Type | Change |",
            "| --- | --- | --- |",
        ]
        assert "| `ComponentTypes/BrakeSWC` | APPLICATION-SW-COMPONENT-TYPE | added |" in rows
        assert all(row.endswith(" | added |") for row in rows)

    def test_a_pin_mismatch_refuses_as_before_and_writes_no_report(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = "9.9.9"
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")

        code, out, err = _run_validate(manifest, monkeypatch, capsys, "--report")

        assert code == 1
        _assert_the_pin_refusal(err, "validate")
        assert out == ""
        assert not (tmp_path / "dist").exists()

    def test_the_flag_is_declared_on_validate_alone_and_off_by_default(self) -> None:
        parser = build_parser()
        assert parser.parse_args(["validate", "--project", "x"]).report is False
        assert parser.parse_args(["validate", "--project", "x", "--report"]).report is True
        with pytest.raises(SystemExit):
            parser.parse_args(["check-model", "--project", "x", "--report"])


# check-model --format json


def _seeded_trace_project(tmp_path: Path, *, with_error: bool) -> Path:
    """The reference model with trace findings at every level a link can sit. Returns the manifest.

    The requirement moves to version 2, so the two components' `@1` links need
    confirmation (component level); `ExampleSWC`'s port states no version and
    its runnable links `@1` (the nested `ports/*` and `runnables/*` subjects);
    *with_error* adds a link to a requirement nobody declares, an error.
    """
    project = editing_vector_fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    model = project / "model"
    swc = model / "swc" / "example" / "ExampleSWC.yaml"
    document = yaml.safe_load(swc.read_text(encoding="utf-8"))
    document["ports"][0]["satisfies"] = ["REQ_EXAMPLE_001"]
    document["runnables"][0]["satisfies"] = ["REQ_EXAMPLE_001@1"]
    if with_error:
        document["satisfies"] = [*document["satisfies"], "REQ_NOPE@1"]
    swc.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    requirement = model / "requirements" / "REQ_EXAMPLE_001.yaml"
    document = yaml.safe_load(requirement.read_text(encoding="utf-8"))
    document["version"] = 2
    requirement.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return project / "swcraft.yaml"


def _run_check_model(manifest: Path, monkeypatch, capsys, *extra: str) -> tuple[int, str, str]:
    """One in-process `check-model` run: its exit code, its stdout, its stderr."""
    monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest), *extra])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _finding_lines(stderr: str) -> list[tuple[str, str]]:
    """The text form's `ERROR:` / `WARNING:` lines as (severity, message), in order."""
    prefixes = {"  ERROR: ": "error", "  WARNING: ": "warning"}
    findings = []
    for line in stderr.splitlines():
        for prefix, severity in prefixes.items():
            if line.startswith(prefix):
                findings.append((severity, line[len(prefix) :]))
    return findings


#: The subject addresses of the seeded warnings, by the level the link sits at.
_COMPONENT_LEVEL = {
    "layer": "swc",
    "element": "ExampleSWC",
    "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
}
_PORT_LEVEL = {
    "layer": "swc",
    "element": "ExampleSWC",
    "path": [{"field": "ports"}, {"member": "pOut"}, {"field": "satisfies"}, {"member": "REQ_EXAMPLE_001"}],
}
_RUNNABLE_LEVEL = {
    "layer": "swc",
    "element": "ExampleSWC",
    "path": [{"field": "runnables"}, {"member": "ExampleRun"}, {"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
}


class TestCheckModelJsonForm:
    """`check-model --format json`: the run's findings as one document on stdout.

    The document is `{schemaVersion, findings}` alone; errors before warnings, each in the
    text form's order; a finding carries rule, severity, message, subjects (never a storage
    path) and target; stdout holds the document alone and stderr every line the text form prints
    there; the exit code is the text form's; a refused run prints no document. The findings come
    from a real gate over a copy of the reference model.

    The nested `ports/*` and `runnables/*` link subjects are held equal to the editing
    protocol's address here: `test_editing_validator_messages.py`'s corpus emits none.

    Does not reach: the encoder guard's `ValueError` half (a non-finite number), since the
    loader refuses NaN and infinity, so no model reaches it.
    """

    def test_the_document_lists_errors_then_warnings_with_their_subjects(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=True)

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        document = json.loads(out)
        assert set(document) == {"schemaVersion", "findings"}
        assert document["schemaVersion"] == cli.CHECK_MODEL_SCHEMA_VERSION == 1
        findings = document["findings"]
        for finding in findings:
            assert set(finding) == {"rule", "severity", "message", "subjects", "target"}
        severities = [finding["severity"] for finding in findings]
        assert severities == sorted(severities, key=["error", "warning"].index)
        assert severities.count("error") == 1
        error = findings[0]
        assert error["rule"] == "trace.link-unknown-requirement"
        assert error["target"] == "REQ_NOPE"
        assert error["subjects"] == [
            {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "satisfies"}, {"member": "REQ_NOPE@1"}]}
        ]
        by_subject = {json.dumps(finding["subjects"], sort_keys=True): finding for finding in findings[1:]}
        component = by_subject[json.dumps([_COMPONENT_LEVEL], sort_keys=True)]
        port = by_subject[json.dumps([_PORT_LEVEL], sort_keys=True)]
        runnable = by_subject[json.dumps([_RUNNABLE_LEVEL], sort_keys=True)]
        assert component["rule"] == runnable["rule"] == "trace.link-needs-confirmation"
        assert port["rule"] == "trace.link-version-not-stated"
        assert {component["target"], port["target"], runnable["target"]} == {"REQ_EXAMPLE_001"}
        assert "(port 'pOut')" in port["message"]
        assert "(runnable 'ExampleRun')" in runnable["message"]

    def test_the_nested_link_subjects_are_the_protocol_address_s_json(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The nested link subjects: the validator's own, the CLI's JSON and the protocol's address agree."""
        manifest = _seeded_trace_project(tmp_path, with_error=False)

        result = check_cross_layer_consistency(str(manifest.parent / "model"))
        subjects = [subject for record in (*result.records, *result.warnings) for subject in record.subjects]
        _, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        printed = [subject for finding in json.loads(out)["findings"] for subject in finding["subjects"]]
        assert printed == [subject.to_json() for subject in subjects]
        assert _PORT_LEVEL in printed and _RUNNABLE_LEVEL in printed
        for subject in subjects:
            assert subject.to_json() == subject_address(subject).to_json(), subject

    def test_a_clean_model_prints_an_empty_document_and_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The run that clears the Problems panel: no finding, the document alone, no success line."""
        project = editing_vector_fixtures.build_fixture(tmp_path / "project", "read-swc-element")

        code, out, _ = _run_check_model(project / "swcraft.yaml", monkeypatch, capsys, "--format", "json")

        assert code == 0
        assert out == json.dumps({"schemaVersion": 1, "findings": []}, indent=2) + "\n"

    def test_a_subject_json_cannot_encode_is_the_clean_error_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _version_project(tmp_path, None)
        issue = ConsistencyIssue(
            rule="task.unknown-swc",
            message=_ERROR_MESSAGE,
            subjects=(Subject("swc", "X", (field_step("ports"), member_step(object()))),),
        )
        result = ConsistencyResult(consistent=False, issues=[issue.message], records=(issue,))

        with patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=result):
            code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        assert out == ""
        assert err.splitlines()[-1].startswith("Error: ")

    def test_stdout_is_the_document_alone_and_stderr_is_the_text_form_s(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=True)

        _, text_out, text_err = _run_check_model(manifest, monkeypatch, capsys)
        _, json_out, json_err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert text_out == ""
        assert json_out == json.dumps(json.loads(json_out), indent=2) + "\n"
        assert json_err == text_err
        assert "  ERROR: " in json_err and "  WARNING: " in json_err

    def test_a_warnings_only_run_prints_the_document_without_the_success_line_and_exits_0(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=False)

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 0
        assert "Validation passed" not in out
        findings = json.loads(out)["findings"]
        assert findings and {finding["severity"] for finding in findings} == {"warning"}

    def test_warnings_as_errors_carries_the_document_and_exits_1(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=False)

        code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json", "--warnings-as-errors")

        assert code == 1
        findings = json.loads(out)["findings"]
        assert {finding["severity"] for finding in findings} == {"warning"}
        assert err.endswith(f"Error: {len(findings)} warning(s) treated as errors (--warnings-as-errors).\n")

    def test_a_finding_without_a_subject_carries_an_empty_list_and_a_null_target(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)

        with patch(
            "swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=_errors_and_warnings_result()
        ):
            code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        assert json.loads(out) == {
            "schemaVersion": 1,
            "findings": [
                {
                    "rule": "task.unknown-swc",
                    "severity": "error",
                    "message": _ERROR_MESSAGE,
                    "subjects": [],
                    "target": None,
                },
                *(
                    {
                        "rule": _WARNING_RULE,
                        "severity": "warning",
                        "message": message,
                        "subjects": [],
                        "target": None,
                    }
                    for message in _WARNING_MESSAGES
                ),
            ],
        }

    def test_an_unreadable_project_prints_no_document(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=False)
        (manifest.parent / "model" / "swc" / "example" / "ExampleSWC.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8", newline="\n"
        )

        code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        assert out == ""
        assert err.startswith("Error:")

    def test_a_pin_mismatch_prints_no_document(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _seeded_trace_project(tmp_path, with_error=False)
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = "9.9.9"
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")

        code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        assert out == ""
        assert "9.9.9" in err and __version__ in err

    @pytest.mark.parametrize("with_error", [True, False], ids=["errors", "warnings-only"])
    def test_the_text_default_is_unchanged(self, with_error: bool, tmp_path: Path, monkeypatch, capsys) -> None:
        """No flag and `--format text` are one run, byte for byte, with the success line on a clean verdict."""
        manifest = _seeded_trace_project(tmp_path, with_error=with_error)

        default = _run_check_model(manifest, monkeypatch, capsys)
        explicit = _run_check_model(manifest, monkeypatch, capsys, "--format", "text")

        assert default == explicit
        assert default[1] == ("" if with_error else _SUCCESS_LINE["check-model"] + "\n")

    @pytest.mark.parametrize("with_error", [True, False], ids=["errors", "warnings-only"])
    def test_the_json_findings_are_the_text_form_s_lines_in_order_with_its_exit_code(
        self, with_error: bool, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """One model, both forms: the same findings, order and verdict."""
        manifest = _seeded_trace_project(tmp_path, with_error=with_error)

        text_code, _, text_err = _run_check_model(manifest, monkeypatch, capsys)
        json_code, json_out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        lines = _finding_lines(text_err)
        assert lines
        assert [(finding["severity"], finding["message"]) for finding in json.loads(json_out)["findings"]] == lines
        assert json_code == text_code == (1 if with_error else 0)


# The CLI's own output on a redirected stream


#: The environment that makes a child's *undeclared* stdout and stderr resolve a
#: non-UTF-8 codec -- the state a redirected run is in on a Windows host, where only
#: a real console gets the console API's UTF-8.
#:
#: The environment, not a monkeypatch: a child's `sys.stdout` is built in C before
#: any of our code runs. Each variable is load-bearing:
#:
#: - `LC_ALL=C` makes the preferred encoding `ANSI_X3.4-1968`.
#: - `PYTHONCOERCECLOCALE=0` stops PEP 538's C-locale coercion to `C.UTF-8`.
#: - `PYTHONUTF8=0` stops PEP 540's UTF-8 Mode, on by default under the C locale.
#:
#: `PYTHONIOENCODING` is deliberately absent: the product would then only have to beat
#: an explicit request rather than a locale fallback, which is not the defect.
#:
#: **What this does not reach.** It is a locale simulation, not Windows: nothing about
#: the console API, code page 437, or which bytes that platform writes. On a Windows
#: host the same runs are in that state with no environment at all.
_NON_UTF8_STREAM_ENV = {
    "LC_ALL": "C",
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
}

#: The validator's advice, verbatim, em dash included: the identifier rule's
#: sentence, `validator/events.py::_identifier_issue`'s `Rename '…' — for example
#: to …`. The witness is an error because no warning sentence carries a non-ASCII
#: character.
_EM_DASH_ADVICE = (
    "SWC 'Bad-Swc' in the swc layer is not a legal AUTOSAR Identifier: must be an AUTOSAR Identifier matching "
    "[a-zA-Z][a-zA-Z0-9_]* (a letter first, then letters, digits or underscores only). Rename 'Bad-Swc' — for "
    "example to 'Bad_Swc' ('swcraft impact \"Bad-Swc\"' lists what references it)."
)

#: The one warning `_unallocated_requirement_project` carries, verbatim.
_UNALLOCATED_WARNING = (
    "Requirement 'REQ_ALONE' is satisfied by no element: add `satisfies: [REQ_ALONE@1]` on the component, "
    "port, runnable, composition, interface, task or hardware element that realizes it."
)

#: What an ASCII stream does to that em dash: `stderr`'s `errors="backslashreplace"`
#: falsifies the message into a literal escape. Named so the failure says which went wrong.
_BACKSLASH_ESCAPED_EM_DASH = "\\u2014"


def _run_cli_with_pipes(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    """Run the `swcraft` CLI as a child with both streams on pipes, in bytes.

    Not `text=True`: it would decode with this parent's codec and hide the thing under test.
    """
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *argv],
        capture_output=True,
        env={**os.environ, **env},
        cwd=str(REPO_ROOT),
    )


class TestCliStreamEncoding:
    """`swcraft` output survives a redirected non-UTF-8 stream.

    Invisible from a console: `swcraft check-model > log` wrote cp1252 bytes for a message
    the toolchain reads as UTF-8, and `swcraft --help > usage.txt` exited 1 with
    `UnicodeEncodeError`. An oracle on Linux too; none is `skipif`ed to Windows, since the
    mechanism can be created outright.
    """

    @staticmethod
    def _one_swc_project(tmp_path: Path, swc: str, *, placed: bool = True, requirement: bool = False) -> Path:
        """A project of one SWC *swc* that satisfies no requirement, a processor and a root composition.

        Seeded so that nothing but what a case names is a finding. *placed* puts the SWC in
        the root (an unplaced `Bad-Swc` is one identifier error, a placed one two: the member
        name is judged too); *requirement* adds `REQ_ALONE`, which nothing satisfies --
        `trace.requirement-unallocated`'s one warning.
        """
        manifest = _write_manifest(tmp_path)
        model = tmp_path / "model"
        members = [{"name": swc, "type": swc}] if placed else []
        documents: list[tuple[str, dict[str, Any]]] = [
            (
                f"swc/{swc}.yaml",
                {"name": swc, "type": "application", "handle_termination_and_restart": "no_support"},
            ),
            ("hardware/CPU.yaml", {"name": "CPU", "kind": "processor"}),
            (
                "compositions/Root.yaml",
                {"name": "Root", "title": "Root composition", "root": True, "members": members},
            ),
        ]
        if requirement:
            documents.append(
                (
                    "requirements/REQ_ALONE.yaml",
                    {
                        "id": "REQ_ALONE",
                        "title": "Alone",
                        "description": "Nothing satisfies this requirement.",
                        "version": 1,
                        "source": "architecture",
                    },
                )
            )
        for relative, document in documents:
            path = model / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        return manifest

    def test_the_forced_locale_actually_reaches_the_child(self) -> None:
        """The environment changes the child's streams, so the assertions below are not blind.

        Asserts the mechanism, not a codec name: on Windows the variables do nothing and the
        stream is already cp1252.
        """
        probe = "import sys; print(sys.flags.utf8_mode, sys.stdout.encoding)"
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            env={**os.environ, **_NON_UTF8_STREAM_ENV},
        )
        utf8_mode, _, encoding = completed.stdout.decode("ascii").strip().partition(" ")
        assert utf8_mode == "0", "PYTHONUTF8=0 did not take; UTF-8 Mode would mask everything below"
        codec = codecs.lookup(encoding.strip()).name
        assert codec != "utf-8", f"the child's redirected stdout is {codec}; the harness proves nothing"
        # Not "the codec cannot represent it": cp1252 has the em dash at 0x97, and one
        # wrong byte in a UTF-8 file is the worse failure.
        assert "—".encode(codec, errors="backslashreplace") != b"\xe2\x80\x94"

    def test_check_model_advice_reaches_a_redirected_stderr_as_utf8(self, tmp_path: Path) -> None:
        """`check-model > log 2>&1`: an identifier refusal exits 1, stdout empty, its advice UTF-8 on stderr."""
        manifest = self._one_swc_project(tmp_path, "Bad-Swc", placed=False)

        completed = _run_cli_with_pipes(["check-model", "--project", str(manifest)], env=_NON_UTF8_STREAM_ENV)

        stderr = completed.stderr.decode("utf-8")
        assert completed.returncode == 1, stderr
        assert stderr.splitlines() == [f"  ERROR: {_EM_DASH_ADVICE}", "1 error(s), 0 warning(s)."], stderr
        assert _BACKSLASH_ESCAPED_EM_DASH not in stderr, stderr
        # A refused run prints nothing on stdout; stdout's non-ASCII witness is
        # `test_help_text_reaches_a_redirected_stdout_as_utf8` below.
        assert completed.stdout == b""

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_the_flag_refuses_the_real_warning_through_the_pipes(self, command: str, tmp_path: Path) -> None:
        """`--warnings-as-errors` over a warning the real composer emitted, through the pipes: exit 1, stdout empty.

        `validate` generates nothing: no `dist/`, so no `model_old.arxml`. The component
        beside the requirement satisfies nothing too, and the one warning is the requirement's.
        """
        manifest = self._one_swc_project(tmp_path, "OrphanSwc", requirement=True)

        completed = _run_cli_with_pipes(
            [command, "--project", str(manifest), "--warnings-as-errors"], env=_NON_UTF8_STREAM_ENV
        )

        stderr = completed.stderr.decode("utf-8")
        assert completed.returncode == 1, stderr
        assert stderr.splitlines() == [
            f"  WARNING: {_UNALLOCATED_WARNING}",
            "0 error(s), 1 warning(s).",
            "Error: 1 warning(s) treated as errors (--warnings-as-errors).",
        ]
        assert "OrphanSwc" not in stderr
        assert completed.stdout == b""
        assert not (tmp_path / "dist").exists()

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_a_warnings_only_run_without_the_flag_passes_through_the_pipes(self, command: str, tmp_path: Path) -> None:
        """The same model without `--warnings-as-errors`: the warning is listed, the success line printed, exit 0."""
        manifest = self._one_swc_project(tmp_path, "OrphanSwc", requirement=True)

        completed = _run_cli_with_pipes([command, "--project", str(manifest)], env=_NON_UTF8_STREAM_ENV)

        stderr = completed.stderr.decode("utf-8")
        assert completed.returncode == 0, stderr
        assert stderr.splitlines() == [f"  WARNING: {_UNALLOCATED_WARNING}", "0 error(s), 1 warning(s)."]
        assert completed.stdout.decode("utf-8").splitlines()[-1] == _SUCCESS_LINE[command]

    @pytest.mark.parametrize("command", ["check-model", "validate"])
    def test_the_flag_accepts_a_component_that_satisfies_no_requirement(self, command: str, tmp_path: Path) -> None:
        """An unlinked component is no finding: under the flag exit 0, stderr empty, and `validate` writes the ARXML."""
        manifest = self._one_swc_project(tmp_path, "OrphanSwc")

        completed = _run_cli_with_pipes(
            [command, "--project", str(manifest), "--warnings-as-errors"], env=_NON_UTF8_STREAM_ENV
        )

        stderr = completed.stderr.decode("utf-8")
        assert completed.returncode == 0, stderr
        assert stderr == ""
        stdout = completed.stdout.decode("utf-8").splitlines()
        assert stdout[-1] == _SUCCESS_LINE[command]
        assert (tmp_path / "dist" / "model.arxml").is_file() is (command == "validate")

    def test_help_text_reaches_a_redirected_stdout_as_utf8(self) -> None:
        """`swcraft --help > usage.txt`: the parser's em dash reaches stdout as UTF-8.

        It writes from inside `parse_args()`, which is why `force_utf8_streams()` is the first
        statement of `main()`.
        """
        in_process = build_parser().format_help()
        assert "—" in in_process, "the parser's help text lost its non-ASCII; this test now proves nothing"

        completed = _run_cli_with_pipes(["--help"], env=_NON_UTF8_STREAM_ENV)

        assert completed.returncode == 0
        stdout = completed.stdout.decode("utf-8")
        assert "—" in stdout, stdout
        assert _BACKSLASH_ESCAPED_EM_DASH not in stdout, stdout

    @pytest.mark.parametrize(
        "entry_point",
        [
            REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "entry.py",
            REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "cli" / "dispatch.py",
            REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent" / "cli.py",
        ],
        ids=["swcraft", "swcraft-module-form", "swcraft-agent"],
    )
    def test_both_entry_points_reconfigure_before_anything_can_print(self, entry_point: Path) -> None:
        """All three entry points' `main()` call `force_utf8_streams()` first.

        Pinned on the source because the agent host has no end-to-end oracle here (it needs an
        LLM and an MCP server); anything that prints or exits before the call would be unfixed.
        """
        tree = ast.parse(entry_point.read_text(encoding="utf-8"))
        mains = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
        assert len(mains) == 1, f"{entry_point} has {len(mains)} module-level `main` definitions"

        body = mains[0].body
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # the docstring
        first = body[0]
        assert (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Call)
            and isinstance(first.value.func, ast.Name)
            and first.value.func.id == "force_utf8_streams"
        ), f"{entry_point}: `main()` must call force_utf8_streams() first; it starts with {ast.dump(first)[:120]}"


class TestForceUtf8StreamsDegrades:
    """What happens when a stream cannot be reconfigured.

    A CLI that raises while printing an error message is worse than one that prints an
    approximation that still names the element to fix.
    """

    def test_a_stream_without_reconfigure_is_left_alone(self, monkeypatch) -> None:
        """An `io.StringIO` is what a test substitutes, and it has no `reconfigure`."""
        substitute = io.StringIO()
        monkeypatch.setattr(sys, "stdout", substitute)

        env_module.force_utf8_streams()  # must not raise

        substitute.write("—")
        assert substitute.getvalue() == "—"

    def test_a_stream_whose_reconfigure_raises_does_not_break_main(self, monkeypatch) -> None:
        """`reconfigure` raising on a detached buffer does not stop the `print()` after it."""

        class _Hostile(io.StringIO):
            def reconfigure(self, **kwargs: Any) -> None:
                raise ValueError("underlying buffer has been detached")

        substitute = _Hostile()
        monkeypatch.setattr(sys, "stderr", substitute)

        env_module.force_utf8_streams()  # must not raise

        print("  ERROR: still printed", file=sys.stderr)
        assert substitute.getvalue() == "  ERROR: still printed\n"


class TestCliDiff:
    """swcraft diff — semantic diff between two model revisions.

    Arguments mirror git diff: zero args -> HEAD vs working tree; one -> REV vs working tree;
    two -> REV_A vs REV_B.
    """

    def test_cli_diff_requires_project(self, monkeypatch) -> None:
        """swcraft diff with no --project -> SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "diff"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_diff_zero_args_head_vs_working_tree(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft diff --project P (no revs) diffs HEAD vs working tree."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        _write_swc(model_root, "SwcOne", description="changed swc")

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "~ SwcOne" in out
        assert "test swc" in out
        assert "changed swc" in out

    def test_cli_diff_one_rev_vs_working_tree(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft diff --project P REV diffs REV vs working tree."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        first_rev = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

        _write_swc(model_root, "SwcTwo")

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "diff", "--project", str(manifest), first_rev],
        )

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "+ SwcTwo (added)" in out

    def test_cli_diff_two_revs(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """swcraft diff --project P REV_A REV_B diffs both as git revisions."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        first_rev = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

        _write_swc(model_root, "SwcOne", description="changed swc")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "modify SwcOne", cwd=tmp_path)
        second_rev = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

        # Both revs are historical: the working tree is not consulted at all.
        _write_swc(model_root, "SwcOne", description="uncommitted swc")

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "diff",
                "--project",
                str(manifest),
                first_rev,
                second_rev,
            ],
        )

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "~ SwcOne" in out
        assert "test swc" in out
        assert "changed swc" in out
        assert "uncommitted swc" not in out

    def test_cli_diff_invalid_revision_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """An invalid revision surfaces the ValueError message and exits non-zero."""
        monkeypatch.setenv("LC_ALL", "C")
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "diff", "--project", str(manifest), "not-a-real-revision"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "git archive failed" in err
        assert "fatal:" in err

    def test_cli_diff_failed_extraction_prints_tarfiles_own_text(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The terminal keeps what `tarfile` said; the shared sentence is a reply's."""
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        commit_link_out_of_the_tree(tmp_path, tmp_path)

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "failed to extract archive for revision 'HEAD'" in err
        assert "outside the destination" in err
        assert "could not be unpacked safely" not in err

    def test_cli_diff_duplicate_identity_key_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A duplicate identity key raised by diff_snapshots() is caught at the CLI boundary.

        Guards a defect: diff_snapshots() was called outside cmd_diff's try/except.
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        # A second file still declaring `name: SwcOne`: a duplicate on the working-tree side only.
        (model_root / "swc" / "SwcOne_dup.yaml").write_text(
            (model_root / "swc" / "SwcOne.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "duplicate key" in err
        assert "SwcOne" in err

    def test_cli_diff_malformed_yaml_exits_nonzero_not_traceback(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Malformed YAML exits non-zero via ElementParseError (a ValueError), never a traceback."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        (model_root / "swc" / "SwcOne.yaml").write_text("name: [unclosed\n", encoding="utf-8")

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "Error:" in err

    # `--format`

    def _committed_project_with_a_changed_swc(self, tmp_path: Path) -> Path:
        """A git project whose working tree differs from HEAD by one SWC field."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        _write_swc(model_root, "SwcOne", description="changed swc")
        return manifest

    def test_cli_diff_format_json_exits_zero_and_emits_parseable_json(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`diff --format json` exits 0 and emits a document that parses."""
        manifest = self._committed_project_with_a_changed_swc(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "json"])

        main()  # must not raise SystemExit

        document = json.loads(capsys.readouterr().out)
        assert document["schema_version"] == 2  # DIFF_SCHEMA_VERSION
        changed = document["layers"]["swcs"]["changed"]
        assert [item["key"] for item in changed] == ["SwcOne"]
        assert {"field": "description", "old": "test swc", "new": "changed swc"} in changed[0]["fields"]

    def test_cli_diff_over_a_nan_model_refuses_it_at_load_and_names_the_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A `.nan` in the model is refused at load: exit 1, no document, the file named on the clean `Error:` path.

        `ModelElement`'s `allow_inf_nan=False` refuses it at the source.
        """
        manifest = _write_manifest(tmp_path)
        swc_dir = tmp_path / "model" / "swc"
        swc_dir.mkdir(parents=True, exist_ok=True)
        (swc_dir / "SwcOne.yaml").write_text(
            "name: SwcOne\ntype: application\nrunnables:\n  - name: Run\n    min_start_interval: 0.01\n",
            encoding="utf-8",
        )
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        (swc_dir / "SwcOne.yaml").write_text(
            "name: SwcOne\ntype: application\nrunnables:\n  - name: Run\n    min_start_interval: .nan\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "json"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == "", "a refused model must produce no document at all"
        assert captured.err.startswith("Error: "), "the refusal must take the clean path, not a traceback"
        assert "SwcOne.yaml" in captured.err, "the message must name the offending file"
        assert "finite" in captured.err, "and say what is wrong with it"

    def test_cli_diff_format_json_encoder_error_exits_nonzero_not_traceback(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A `ValueError` from the strict JSON encoder takes the clean `Error:` path, not a traceback.

        `format_diff_json` renders with `allow_nan=False`, so a value `_json_finite`'s walk
        missed raises. Guards a defect: the JSON write was outside cmd_diff's try/except.
        """
        manifest = _write_manifest(tmp_path)
        swc_dir = tmp_path / "model" / "swc"
        swc_dir.mkdir(parents=True, exist_ok=True)
        (swc_dir / "SwcOne.yaml").write_text(
            "name: SwcOne\ntype: application\nrunnables:\n  - name: Run\n    min_start_interval: 0.01\n",
            encoding="utf-8",
        )
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        (swc_dir / "SwcOne.yaml").write_text(
            "name: SwcOne\ntype: application\nrunnables:\n  - name: Run\n    min_start_interval: .nan\n",
            encoding="utf-8",
        )
        # Blinding the walk is the one way to reach the encoder's guard.
        monkeypatch.setattr("swcraft_tools.differ._json_finite", lambda document: document)
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "json"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    # Collection changes, end to end

    def _committed_project_with_an_added_port(self, tmp_path: Path) -> Path:
        """A git project whose working tree adds one port to a committed SWC."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne", ports=[_port("pSpeed")])
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)
        _write_swc(model_root, "SwcOne", ports=[_port("pSpeed"), _port("pTorqueLimit")])
        return manifest

    def test_cli_diff_names_the_added_port_and_does_not_restate_the_port_list(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An added port is named alone: the unchanged `pSpeed` and `ports: [...] -> [...]` never appear."""
        manifest = self._committed_project_with_an_added_port(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "~ SwcOne" in out
        assert "+ pTorqueLimit (added)" in out
        assert "pSpeed" not in out, "the unchanged port is restated"
        assert "'direction'" not in out, "a whole port dict reached the output"

    def test_cli_diff_format_json_carries_the_added_port_as_a_collection_change(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """JSON: the added port is a `collections` entry; `fields` carries nothing for `ports`."""
        manifest = self._committed_project_with_an_added_port(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "json"])

        main()  # must not raise SystemExit

        changed = json.loads(capsys.readouterr().out)["layers"]["swcs"]["changed"]
        assert [item["key"] for item in changed] == ["SwcOne"]
        assert [field["field"] for field in changed[0]["fields"]] == []
        collections = changed[0]["collections"]
        assert [collection["field"] for collection in collections] == ["ports"]
        assert collections[0]["added"] == ["pTorqueLimit"]
        assert (collections[0]["removed"], collections[0]["changed"], collections[0]["reordered"]) == ([], [], False)

    def test_cli_diff_format_json_on_an_unchanged_model_still_emits_the_envelope(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The "No changes." message is prose for a reader; a consumer gets a document."""
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "json"])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "No changes." not in out
        assert json.loads(out)["layers"]["swcs"] == {
            "added": [],
            "removed": [],
            "changed": [],
            "rename_hints": [],
        }

    def test_cli_diff_defaults_to_text(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`--format text` is the default: the output is the same with and without it."""
        manifest = self._committed_project_with_a_changed_swc(tmp_path)

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])
        main()
        default_out = capsys.readouterr().out

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "text"])
        main()
        explicit_out = capsys.readouterr().out

        assert default_out == explicit_out
        assert default_out == "SWCs:\n  ~ SwcOne\n      description: 'test swc' -> 'changed swc'\n"

    def test_cli_diff_rejects_an_unknown_format(self, tmp_path: Path, monkeypatch) -> None:
        """An unknown format is argparse's usage error, not a silent fallback to text."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest), "--format", "yaml"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0


class TestCliImpact:
    """swcraft impact — transitive dependents of one named element, always in the working tree."""

    def test_cli_impact_requires_project(self, monkeypatch) -> None:
        """swcraft impact with no --project -> SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "SomeName"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_impact_known_element_on_example_model(self, monkeypatch, capsys) -> None:
        """swcraft impact --project P float32 reports IExample as a dependent."""
        repo_root = Path(__file__).resolve().parents[3]
        manifest = repo_root / "examples" / "basic" / "swcraft.yaml"
        if not manifest.exists():
            pytest.skip("examples/basic/swcraft.yaml not present")

        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "--project", str(manifest), "float32"])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "IExample" in out

    def test_cli_impact_no_match_prints_not_found_message(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A name matching no layer prints a clear message, not an error, exit 0."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "impact", "--project", str(manifest), "NoSuchElement"],
        )

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "NoSuchElement" in out
        assert "not found" in out.lower()

    def test_cli_impact_duplicate_identity_key_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A duplicate identity key from impact_of() is caught at the cmd_impact boundary."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        (model_root / "swc" / "SwcOne_dup.yaml").write_text(
            (model_root / "swc" / "SwcOne.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "impact", "--project", str(manifest), "SwcOne"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "duplicate key" in err
        assert "SwcOne" in err

    def test_cli_impact_malformed_yaml_exits_nonzero_not_traceback(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Malformed YAML in the working tree is caught at the cmd_impact boundary."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        (model_root / "swc" / "SwcOne.yaml").write_text("name: [unclosed\n", encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "impact", "--project", str(manifest), "SwcOne"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "Error:" in err


class TestCliImpactDepthAndFormat:
    """``swcraft impact --depth N`` and ``--format text|json``.

    Each claim against an oracle that is not ``cmd_impact``: the default output is
    ``format_impact``'s bytes alone; a bounded text report is the MCP ``impact_of`` tool's
    ``full`` composition (its own byte comparison is in
    ``packages/mcp/tests/test_diff_impact_tools.py``, since this suite cannot import the
    server); the JSON form is ``swcraft query --dependents``' stdout; a depth below 1 is
    argparse's usage error, exit 2, which is why ``impact`` stays in ``ALWAYS_EXITS_ZERO``.

    Does not reach: the asymmetry ``query --dependents X --depth 0`` exits 1 and
    ``impact X --depth 0`` exits 2 is asserted as two facts, not reconciled.
    """

    def _run(self, argv: list[str], monkeypatch, capsys) -> str:
        monkeypatch.setattr("sys.argv", ["swcraft", *argv])
        main()
        return capsys.readouterr().out

    def test_the_default_output_carries_no_footer_and_is_the_text_format(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The default is ``format_impact``'s bytes alone.

        The byte comparisons with the renderer and with the MCP tool live in ``test_impact.py``
        and ``packages/mcp/tests/test_diff_impact_tools.py``: this module does not import the
        snapshot adapter.
        """
        manifest = write_query_project(tmp_path)
        default = self._run(["impact", "float32", "--project", str(manifest)], monkeypatch, capsys)
        text = self._run(["impact", "float32", "--project", str(manifest), "--format", "text"], monkeypatch, capsys)
        # The start is a base type.
        assert default.startswith("Impact of 'float32':\n\nbase_type:\n")
        assert default.endswith("\n") and not default.endswith("\n\n")
        lines = default.splitlines()
        assert not any(line.startswith("(") for line in lines), "a scope footer on the default report"
        assert lines[-1].startswith("  "), "the report must end on a row, not on a footer"
        # A depth past the graph saturates. Each walk orders rows and details its own way,
        # so the elements are compared as a set of lines stripped of their parenthesis.
        bounded = self._run(["impact", "float32", "--project", str(manifest), "--depth", "99"], monkeypatch, capsys)
        report, _, footer = bounded.rpartition("\n\n")
        assert footer == "(depth: 99)\n"
        assert sorted(line.split(" (", 1)[0] for line in report.splitlines()) == sorted(
            line.split(" (", 1)[0] for line in lines
        )
        assert text == default

    def test_a_bounded_text_report_is_the_report_a_blank_line_and_the_footer(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The MCP ``full`` composition; the tool's own bytes are compared in ``mcp_server``."""
        manifest = write_query_project(tmp_path)
        bounded = self._run(["impact", "float32", "--project", str(manifest), "--depth", "1"], monkeypatch, capsys)
        transitive = self._run(["impact", "float32", "--project", str(manifest)], monkeypatch, capsys)

        report, _, footer = bounded.rpartition("\n\n")
        assert footer == "(depth: 1)\n"
        assert report.startswith("Impact of 'float32':")
        assert bounded != transitive, "the fixture must reach something at hop two, or depth 1 bounds nothing"
        assert set(report.splitlines()) < set(transitive.splitlines())

    @pytest.mark.parametrize("depth", [None, "1", "2"])
    def test_the_json_form_is_query_dependents_json(
        self, depth: str | None, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        bound = [] if depth is None else ["--depth", depth]
        reported = self._run(
            ["impact", "float32", "--project", str(manifest), "--format", "json", *bound], monkeypatch, capsys
        )
        queried = self._run(
            ["query", "--dependents", "float32", "--project", str(manifest), "--format", "json", *bound],
            monkeypatch,
            capsys,
        )
        assert reported == queried
        document = json.loads(reported)
        assert document["schemaVersion"] == 3  # QUERY_SCHEMA_VERSION
        assert document["direction"] == "dependents"
        assert document["depth"] == (None if depth is None else int(depth))
        assert document["matched"]

    def test_a_name_no_layer_carries_is_the_walk_document_in_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        out = self._run(
            ["impact", "NoSuchElement", "--project", str(manifest), "--format", "json"], monkeypatch, capsys
        )
        document = json.loads(out)
        assert document == {
            "schemaVersion": 3,
            "name": "NoSuchElement",
            "direction": "dependents",
            "depth": None,
            "matched": {},
        }

    def test_a_name_no_layer_carries_is_the_unchanged_sentence_in_text(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        out = self._run(["impact", "NoSuchElement", "--project", str(manifest)], monkeypatch, capsys)
        assert out == "Element 'NoSuchElement' not found in any layer.\n"

    def test_a_name_no_layer_carries_keeps_the_depth_footer_in_text(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The MCP tool's composition, not-found included: the sentence, a blank line, the footer."""
        manifest = write_query_project(tmp_path)
        out = self._run(["impact", "NoSuchElement", "--project", str(manifest), "--depth", "1"], monkeypatch, capsys)
        assert out == "Element 'NoSuchElement' not found in any layer.\n\n(depth: 1)\n"

    def test_a_name_no_layer_carries_echoes_the_depth_in_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        out = self._run(
            ["impact", "NoSuchElement", "--project", str(manifest), "--format", "json", "--depth", "2"],
            monkeypatch,
            capsys,
        )
        assert json.loads(out) == {
            "schemaVersion": 3,
            "name": "NoSuchElement",
            "direction": "dependents",
            "depth": 2,
            "matched": {},
        }

    def test_the_json_form_on_an_unreadable_project_exits_one_with_nothing_on_stdout(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        (model_root / "swc" / "SwcOne.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "SwcOne", "--project", str(manifest), "--format", "json"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert captured.out == ""
        assert "Error:" in captured.err

    @pytest.mark.parametrize("depth", ["0", "-1", "x", "1.5", "+3", " 3 ", "1_0", "\u0663", ""])
    def test_a_depth_that_is_not_a_positive_whole_number_is_a_usage_error(
        self, depth: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "float32", "--project", str(manifest), "--depth", depth])
        with pytest.raises(SystemExit) as exc_info:
            main()
        captured = capsys.readouterr()
        assert exc_info.value.code == 2
        assert captured.out == ""
        assert "--depth" in captured.err

    def test_the_asymmetry_with_query_depth_zero_is_stated_not_reconciled(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--dependents", "float32", "--depth", "0", "--project", str(manifest)]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        queried = capsys.readouterr()
        assert exc_info.value.code == 1
        assert queried.out == ""
        assert "depth must be at least 1" in queried.err, "query refuses in its own sentence, not argparse's usage"
        assert "usage:" not in queried.err

        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "float32", "--depth", "0", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        reported = capsys.readouterr()
        assert exc_info.value.code == 2
        assert reported.out == ""
        assert "usage:" in reported.err and "expected a whole number of at least 1" in reported.err

    def test_an_unknown_format_is_a_usage_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "impact", "float32", "--project", str(manifest), "--format", "yaml"]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2
        assert capsys.readouterr().out == ""

    def test_impact_stays_a_read_only_command_that_always_exits_zero(self) -> None:
        assert "impact" in guide_reference_cli.ALWAYS_EXITS_ZERO
        assert "impact" in project_args._PIN_ADVISORY_COMMANDS
        assert "impact" not in guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST


class TestCliList:
    """swcraft list — every declared layer's elements, by identity.

    Read-only: a successful run always exits 0, and only a project the command could not
    *read* exits non-zero; both halves are asserted. The renderers are tested in
    `test_listing.py`; these tests own the argv shapes, exit codes and output stream.
    """

    def test_cli_list_requires_project(self, monkeypatch) -> None:
        """swcraft list with no --project -> SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "list"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_list_unresolvable_project_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        """A --project path with no swcraft.yaml behind it exits non-zero."""
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "list", "--project", str(tmp_path / "nope" / "swcraft.yaml")],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_cli_list_prints_every_layer_in_display_order(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """One `layer: element` line per element, layers in the contract's displayOrder, derived from the registry."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        dump_yaml(
            model_root / "tasks" / "Task_10ms.yaml",
            {"name": "Task_10ms", "period": 0.01, "priority": 5, "partition": "PARTITION0"},
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        main()  # must not raise SystemExit

        lines = capsys.readouterr().out.splitlines()
        assert lines == ["swc: SwcOne", "tasks: Task_10ms"]
        order = production_registry().display_order
        assert order.index("swc") < order.index("tasks")

    def test_cli_list_named_layer_lists_only_that_layer(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        dump_yaml(
            model_root / "tasks" / "Task_10ms.yaml",
            {"name": "Task_10ms", "period": 0.01, "priority": 5, "partition": "PARTITION0"},
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "swc", "--project", str(manifest)])

        main()  # must not raise SystemExit

        assert capsys.readouterr().out.splitlines() == ["swc: SwcOne"]

    def test_cli_list_names_the_compositions_layer_like_any_other(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``compositions`` is accepted and listed by its declared identity field: `LAYER`'s set is the registry's."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        dump_yaml(
            model_root / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root composition", "root": True, "members": [{"name": "one", "type": "SwcOne"}]},
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "compositions", "--project", str(manifest)])

        main()  # must not raise SystemExit

        assert capsys.readouterr().out.splitlines() == ["compositions: Root"]

    def test_cli_list_json_carries_the_schema_version_and_every_layer(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The document the VS Code QuickPick parses: versioned, every layer a key.

        An absent layer directory is an **empty** list, not an absent key, so a consumer can
        tell "no SWCs" from "no such layer".
        """
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest), "--format", "json"])

        main()  # must not raise SystemExit

        document = json.loads(capsys.readouterr().out)
        assert document["schemaVersion"] == 1
        assert list(document["layers"]) == list(production_registry().display_order)
        assert document["layers"]["swc"] == {"identityField": "name", "elements": ["SwcOne"]}
        assert document["layers"]["hardware"]["elements"] == []

    def test_cli_list_layer_order_matches_the_order_diff_renders(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`list` and `diff` order their layers alike, compared in the bytes a user sees.

        Over the layers that have elements: `format_diff` emits no section for an unchanged
        layer, and every element here is new against an empty HEAD.

        Does not reach: the `choices` set (tested by
        `test_cli_list_unknown_layer_is_refused_with_the_accepted_set`), `impact`'s layer
        grouping, and the order of elements inside a layer (pinned in `test_listing.py`).
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        model_root.mkdir(exist_ok=True)
        # The `.gitkeep` makes the model root exist at HEAD: git tracks no empty
        # directory, and `diff` refuses a revision with no model root.
        (model_root / ".gitkeep").write_text("", encoding="utf-8")
        _git("init", cwd=tmp_path)
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-m", "initial commit", cwd=tmp_path)

        _write_swc(model_root, "SwcOne")
        dump_yaml(
            model_root / "tasks" / "Task_10ms.yaml",
            {"name": "Task_10ms", "period": 0.01, "priority": 5, "partition": "PARTITION0"},
        )
        write_model_element(model_root, {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
        dump_yaml(
            model_root / "requirements" / "REQ_ONE.yaml",
            {
                "id": "REQ_ONE",
                "title": "One",
                "description": "The system shall list.",
                "req_type": "req",
                "version": 1,
                "source": "architecture",
            },
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])
        main()
        listed: list[str] = []
        for line in capsys.readouterr().out.splitlines():
            layer = line.split(":", 1)[0]
            if layer not in listed:
                listed.append(layer)

        monkeypatch.setattr("sys.argv", ["swcraft", "diff", "--project", str(manifest)])
        main()
        headings = [
            line[:-1]
            for line in capsys.readouterr().out.splitlines()
            if line.endswith(":") and not line.startswith(" ")
        ]

        registry = production_registry()
        assert len(listed) >= 3, "a vacuous comparison: the fixture must populate several layers"
        assert headings == [registry.layer(layer).display_label for layer in listed]

    def test_cli_list_unknown_layer_is_refused_with_the_accepted_set(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """argparse rejects an unknown layer, naming the contract's accepted set, never a literal list."""
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "not_a_layer", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "invalid choice" in err
        for layer in production_registry().display_order:
            assert layer in err

    def test_cli_list_one_unparseable_element_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """One broken file fails the whole run: the reader Pydantic-validates every file.

        The extension keeps its input-box fallback for this case rather than reading the
        failure as an empty model.
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_swc(model_root, "SwcOne")
        (model_root / "swc" / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    def test_cli_list_absent_model_root_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A model root that is not there exits non-zero: empty layers would look like a fresh scaffold."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    def test_cli_list_empty_model_exits_0_with_no_output(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """An existing but empty model root exits 0 and prints nothing, not even a blank line."""
        manifest = _write_manifest(tmp_path)
        (tmp_path / "model").mkdir()
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        main()  # must not raise SystemExit

        assert capsys.readouterr().out == ""

    def test_cli_list_empty_model_json_still_names_every_layer(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The JSON envelope is unconditional: a consumer parses it, empty or not."""
        manifest = _write_manifest(tmp_path)
        (tmp_path / "model").mkdir()
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest), "--format", "json"])

        main()  # must not raise SystemExit

        document = json.loads(capsys.readouterr().out)
        assert list(document["layers"]) == list(production_registry().display_order)
        assert all(layer["elements"] == [] for layer in document["layers"].values())


class TestCliMcpServe:
    def test_cli_mcp_serve_requires_project(self, monkeypatch) -> None:
        """swcraft mcp-serve with no --project → SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_mcp_serve_checks_swcraft_mcp_importable(self, tmp_path: Path, monkeypatch) -> None:
        """When swcraft_mcp not importable → SystemExit(1) with install hint."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])

        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1


class TestTheHandoverIsAProcessOnWindows:
    """What ``exec`` means on the two platforms.

    ``swcraft_tools`` may not import ``swcraft_mcp`` or ``swcraft_agent``, so both
    subcommands hand over to ``python -m <module>``. On POSIX ``os.execvp`` keeps the
    process (pipes, PID, lifetime), which a stdio MCP server needs. Windows' ``_execv``
    creates a new process and terminates the caller, so VS Code would see its server die;
    there the handover is a child that is **waited for**, and this process exits with its code.

    Does not reach Windows itself: the branch is driven through
    :func:`handover._exec_replaces_this_process`, so the predicate is asserted separately
    against this host.
    """

    def test_the_predicate_answers_this_host(self) -> None:
        """The one fact the cases below stand on, read from the interpreter itself."""
        assert handover._exec_replaces_this_process() is (os.name != "nt")

    def test_windows_waits_for_the_child_instead_of_replacing_itself(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: False)

        with patch("os.execvp") as execvp, patch("subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 3
            with pytest.raises(SystemExit) as exc_info:
                main()

        execvp.assert_not_called()
        assert popen.call_args.args == ([sys.executable, "-m", "swcraft_mcp.server"],)
        assert exc_info.value.code == 3, "the child's exit code is this command's exit code"
        popen.return_value.kill.assert_not_called()

    def test_posix_still_replaces_the_image(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp, patch("subprocess.Popen") as popen:
            main()

        popen.assert_not_called()
        assert execvp.call_args.args == (
            sys.executable,
            [sys.executable, "-m", "swcraft_mcp.server"],
        )

    def test_the_manifest_reaches_the_child_through_the_environment(self, tmp_path: Path, monkeypatch) -> None:
        """The waited child inherits ``os.environ``, where ``SWCRAFT_MANIFEST`` is set before the spawn."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: False)
        monkeypatch.delenv("SWCRAFT_MANIFEST", raising=False)
        seen = {}

        def record(command, **spawn):
            seen["manifest"] = os.environ.get("SWCRAFT_MANIFEST")
            seen["stdin"] = spawn.get("stdin", "not declared")
            child = MagicMock()
            child.wait.return_value = 0
            return child

        with patch("subprocess.Popen", side_effect=record):
            with pytest.raises(SystemExit):
                main()

        assert seen["manifest"] == str(manifest)
        assert seen["stdin"] is None, "the child inherits the pipe VS Code writes into, and says so"

    def test_the_agent_handover_takes_the_same_branch(self, tmp_path: Path, monkeypatch) -> None:
        """``swcraft agent`` execs for the same reason, and breaks the same way."""
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: False)

        with patch("os.execvp") as execvp, patch("subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            with pytest.raises(SystemExit) as exc_info:
                main()

        execvp.assert_not_called()
        assert popen.call_args.args == (
            [sys.executable, "-m", "swcraft_agent.cli", "chat", "--project", str(manifest)],
        )
        assert exc_info.value.code == 0

    def test_an_interrupt_at_the_console_does_not_kill_the_waited_child(self, tmp_path: Path, monkeypatch) -> None:
        """An interrupt at the console does not kill the waited child: it owns its shutdown.

        A Windows console delivers ``CTRL_C_EVENT`` to every attached process; guards a defect
        where ``subprocess.call``'s handler killed the child, lost its exit code and ended in a
        traceback. Driven with a real child that ignores the interrupt and exits 7.

        Does not reach: Windows, and a child that never exits (waited for forever, as an
        ``exec`` would be).
        """
        marker = tmp_path / "child-cleaned-up"
        program = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(0.5)\n"
            f"open({str(marker)!r}, 'w', encoding='utf-8').close()\n"
            "sys.exit(7)\n"
        )
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: False)
        real_wait = subprocess.Popen.wait
        interrupted: list[bool] = []

        def wait_once_interrupted(self, timeout=None):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return real_wait(self, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait_once_interrupted)

        with pytest.raises(SystemExit) as exc_info:
            handover._hand_over([sys.executable, "-c", program])

        assert interrupted == [True], "the case is vacuous unless the interrupt was actually delivered"
        assert exc_info.value.code == 7, "the interrupted parent still answers the child's own exit code"
        assert marker.exists(), "the child ran its own shutdown instead of being killed"

    def test_a_windows_status_code_is_normalised_before_it_is_re_exited(self, monkeypatch) -> None:
        """A Windows status code is folded to its signed value before it is re-exited.

        ``Popen.wait()`` returns an unsigned DWORD (``STATUS_CONTROL_C_EXIT`` is
        ``3221225786``), and ``sys.exit`` overflows a 32-bit ``long`` above ``0x7FFFFFFF`` and
        exits ``-1``. Does not reach Windows itself: only the normalisation is driven here.
        """
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: False)
        child = MagicMock()
        child.wait.return_value = 0xC000013A

        with patch("subprocess.Popen", return_value=child):
            with pytest.raises(SystemExit) as exc_info:
                handover._hand_over([sys.executable, "-c", "pass"])

        assert exc_info.value.code == -1073741510, (
            "the caller must read the child's own status, not the unsigned DWORD it arrives as"
        )

    def test_the_ordinary_and_the_posix_signalled_status_pass_through_unchanged(self) -> None:
        """A POSIX signalled status and an ordinary exit pass through: only the unsigned half is folded."""
        assert handover._exit_status_of(0) == 0
        assert handover._exit_status_of(7) == 7
        assert handover._exit_status_of(-2) == -2, "a POSIX signalled status is already signed"
        assert handover._exit_status_of(0x7FFF_FFFF) == 0x7FFF_FFFF, "the largest positive status is not folded"
        assert handover._exit_status_of(0x8000_0000) == -0x8000_0000, "the first status that is"
        assert handover._exit_status_of(0xFFFF_FFFF) == -1


class TestCliAgent:
    """`swcraft agent` — the third exec'd subcommand.

    `swcraft_tools` may not import `swcraft_agent`, so the handover is a process replacement.
    The argv is the whole contract: everything after `agent` is the assistant's command line.
    """

    def test_agent_execs_the_agent_module_with_the_remainder_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat", "--project", str(manifest), "--scoped"])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        assert execvp.call_args.args == (
            sys.executable,
            [sys.executable, "-m", "swcraft_agent.cli", "chat", "--project", str(manifest), "--scoped"],
        )

    def test_the_remainder_is_handed_over_byte_for_byte(self, monkeypatch) -> None:
        """Tokens `swcraft` declares for its own commands and `--` reach the assistant unparsed, unreordered.

        `--project` is not among them: :func:`cli.project_args.remainder_project` reads it to
        check the pin before the exec (:class:`TestToolchainPin`).
        """
        passed = [
            "chat",
            "--script",
            "packages/agent/tests/fixtures/scenarios/01-add-swc.yaml",
            "--format",
            "json",
            "--",
            "-x",
        ]
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", *passed])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        assert execvp.call_args.args[1] == [sys.executable, "-m", "swcraft_agent.cli", *passed]

    def test_agent_with_nothing_after_it_lets_the_assistant_answer(self, monkeypatch) -> None:
        """An empty remainder lets the assistant print its own usage."""
        monkeypatch.setattr("sys.argv", ["swcraft", "agent"])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        assert execvp.call_args.args[1] == [sys.executable, "-m", "swcraft_agent.cli"]

    def test_agent_without_the_package_exits_one_naming_it(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat"])

        with patch("importlib.util.find_spec", return_value=None), patch("os.execvp") as execvp:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        assert "swcraft-agent" in capsys.readouterr().err
        execvp.assert_not_called()


def _project_with_outputs(tmp_path: Path, data: dict | None = None) -> Path:
    """A manifest with a populated `dist/`, `views/` and `model/` (the model is what must stay)."""
    manifest = _write_manifest(tmp_path, data)
    (tmp_path / "dist").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dist" / "model.arxml").write_text("<AUTOSAR/>", encoding="utf-8", newline="\n")
    (tmp_path / "dist" / "components").mkdir(exist_ok=True)
    (tmp_path / "dist" / "components" / "Engine.arxml").write_text("<AUTOSAR/>", encoding="utf-8", newline="\n")
    (tmp_path / "views").mkdir(parents=True, exist_ok=True)
    (tmp_path / "views" / "model.c4").write_text("model {}", encoding="utf-8", newline="\n")
    (tmp_path / "model" / "interfaces").mkdir(parents=True, exist_ok=True)
    (tmp_path / "model" / "interfaces" / "ISpeed.yaml").write_text("name: ISpeed\n", encoding="utf-8", newline="\n")
    return manifest


class TestCliCleanOutputs:
    """`swcraft clean-outputs` deletes what the manifest declares as output and nothing else.

    It removes `dist_root` and `views_root`. It is a *write* command, not in
    `guide_reference.cli.ALWAYS_EXITS_ZERO`.
    """

    def test_the_two_declared_roots_go_and_the_model_stays(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _project_with_outputs(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()
        capsys.readouterr()

        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["model_old.arxml"]
        assert not (tmp_path / "views").exists()
        assert (tmp_path / "model" / "interfaces" / "ISpeed.yaml").is_file()
        assert manifest.is_file()

    def test_each_removed_path_is_reported(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Each removed path is reported.

        `dist/` survives for the kept `_old` file, so its removed children are reported one by one; a root with nothing
        to keep is reported whole.
        """
        manifest = _project_with_outputs(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()

        out = capsys.readouterr().out
        assert out.splitlines() == [
            f"Previous ARXML kept: {tmp_path / 'dist' / 'model_old.arxml'}",
            f"Removed {tmp_path / 'dist' / 'components'}",
            f"Removed {tmp_path / 'views'}",
        ]

    def test_without_an_arxml_or_its_old_file_the_dist_root_is_removed_whole(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _project_with_outputs(tmp_path)
        (tmp_path / "dist" / "model.arxml").unlink()
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()

        assert f"Removed {tmp_path / 'dist'}" in capsys.readouterr().out
        assert not (tmp_path / "dist").exists()

    def test_the_audit_log_survives_and_everything_around_it_goes(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The audit log survives: it is the record of every model mutation, and `.gitignore` excludes `dist/`."""
        manifest = _project_with_outputs(tmp_path)
        audit = tmp_path / "dist" / "audit.jsonl"
        audit.write_text('{"op": "create"}\n', encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()
        capsys.readouterr()

        assert audit.is_file()
        assert not (tmp_path / "dist" / "model.arxml").exists()
        assert not (tmp_path / "dist" / "components").exists()

    def test_it_creates_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A project whose outputs were never generated is left as it was: no empty `dist/` appears."""
        manifest = _write_manifest(tmp_path)
        (tmp_path / "model").mkdir(exist_ok=True)
        before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()
        capsys.readouterr()

        assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before

    def test_a_declared_root_that_is_a_file_is_removed_like_any_other_output(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A declared root that is a file is removed, not `iterdir()`-ed into an `OSError` traceback.

        `views_root` is a declaration; an interrupted run or an editor can leave a file there.
        """
        manifest = _project_with_outputs(tmp_path)
        shutil.rmtree(tmp_path / "views")
        (tmp_path / "views").write_text("not a directory\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        main()

        assert f"Removed {tmp_path / 'views'}" in capsys.readouterr().out
        assert not (tmp_path / "views").exists()
        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["model_old.arxml"]

    def test_it_requires_project(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_a_manifest_it_cannot_read_exits_nonzero_and_deletes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _project_with_outputs(tmp_path)
        manifest.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert "Error" in capsys.readouterr().err
        assert (tmp_path / "dist" / "model.arxml").is_file()

    def test_it_is_not_a_command_that_always_exits_zero(self) -> None:
        """It writes, by deleting, so neither read-only tuple names it nor does the reference page."""
        assert "clean-outputs" not in guide_reference_cli.ALWAYS_EXITS_ZERO
        assert "clean-outputs" not in guide_reference_cli.WRITES_NOTHING
        assert "clean-outputs" not in guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST


def _run_clean_outputs(manifest: Path, monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])
    main()
    return capsys.readouterr().out


def _outputs_manifest(tmp_path: Path, *, dist_root: str, views_root: str, arxml: str, needs: str) -> Path:
    return _write_manifest(
        tmp_path,
        {
            "name": "test-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": dist_root,
            "views_root": views_root,
            "outputs": {"arxml": arxml, "needs": needs},
        },
    )


class TestCliCleanOutputsKeepsThePreviousArxml:
    """The first step moves the ARXML aside and removes the rest.

    The manifest's ARXML, when a **direct child** of `dist_root`, is moved to
    `<stem>_old<suffix>` and kept with `audit.jsonl`; the three cases
    the move does not reach are pinned as stated limits.
    """

    def test_the_arxml_moves_aside_and_survives_with_the_audit_log(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _project_with_outputs(tmp_path)
        (tmp_path / "dist" / "model.arxml").write_bytes(b"<AUTOSAR>first</AUTOSAR>")
        (tmp_path / "dist" / "audit.jsonl").write_text('{"op": "create"}\n', encoding="utf-8", newline="\n")
        (tmp_path / "dist" / "needs.json").write_text("{}\n", encoding="utf-8", newline="\n")

        out = _run_clean_outputs(manifest, monkeypatch, capsys)

        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["audit.jsonl", "model_old.arxml"]
        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<AUTOSAR>first</AUTOSAR>"
        assert not (tmp_path / "views").exists()
        assert out.splitlines()[0] == f"Previous ARXML kept: {tmp_path / 'dist' / 'model_old.arxml'}"
        assert out.count("Previous ARXML kept") == 1

    def test_a_second_clean_replaces_the_old_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _project_with_outputs(tmp_path)
        _run_clean_outputs(manifest, monkeypatch, capsys)
        (tmp_path / "dist" / "model.arxml").write_bytes(b"<AUTOSAR>second</AUTOSAR>")

        _run_clean_outputs(manifest, monkeypatch, capsys)

        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<AUTOSAR>second</AUTOSAR>"
        assert not (tmp_path / "dist" / "model.arxml").exists()

    def test_a_dist_root_holding_only_the_old_file_and_the_audit_log_is_left_as_it_is(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """After a build that failed at `check-model`, the last written ARXML survives the next clean too."""
        manifest = _write_manifest(tmp_path)
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "model_old.arxml").write_bytes(b"<AUTOSAR>kept</AUTOSAR>")
        (tmp_path / "dist" / "audit.jsonl").write_text("{}\n", encoding="utf-8", newline="\n")

        out = _run_clean_outputs(manifest, monkeypatch, capsys)

        assert out == ""
        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<AUTOSAR>kept</AUTOSAR>"
        assert (tmp_path / "dist" / "audit.jsonl").is_file()

    def test_a_nested_arxml_is_not_moved_and_its_old_file_goes_with_its_directory(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The stated limit: the keep set is one level deep."""
        manifest = _outputs_manifest(
            tmp_path, dist_root="dist", views_root="views", arxml="dist/sub/model.arxml", needs="dist/needs.json"
        )
        (tmp_path / "dist" / "sub").mkdir(parents=True)
        (tmp_path / "dist" / "sub" / "model.arxml").write_bytes(b"<AUTOSAR/>")
        (tmp_path / "dist" / "sub" / "model_old.arxml").write_bytes(b"<AUTOSAR/>")

        out = _run_clean_outputs(manifest, monkeypatch, capsys)

        assert "Previous ARXML kept" not in out
        assert not (tmp_path / "dist").exists()

    def test_a_dist_root_inside_the_views_root_loses_the_kept_file_on_the_second_pass(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The stated limit: `views_root` is cleaned after `dist_root`, with the base keep set only."""
        manifest = _outputs_manifest(
            tmp_path, dist_root="out/dist", views_root="out", arxml="out/dist/model.arxml", needs="out/dist/needs.json"
        )
        (tmp_path / "out" / "dist").mkdir(parents=True)
        (tmp_path / "out" / "dist" / "model.arxml").write_bytes(b"<AUTOSAR/>")

        out = _run_clean_outputs(manifest, monkeypatch, capsys)

        assert f"Previous ARXML kept: {tmp_path / 'out' / 'dist' / 'model_old.arxml'}" in out
        assert not (tmp_path / "out").exists()

    def test_an_arxml_directly_under_the_views_root_is_never_moved(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _outputs_manifest(
            tmp_path, dist_root="dist", views_root="views", arxml="views/model.arxml", needs="dist/needs.json"
        )
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "model.arxml").write_bytes(b"<AUTOSAR/>")

        out = _run_clean_outputs(manifest, monkeypatch, capsys)

        assert "Previous ARXML kept" not in out
        assert not (tmp_path / "views").exists()

    def test_an_unusual_arxml_name_derives_its_old_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _outputs_manifest(
            tmp_path, dist_root="dist", views_root="views", arxml="dist/Vehicle.System.arxml", needs="dist/needs.json"
        )
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "Vehicle.System.arxml").write_bytes(b"<AUTOSAR/>")

        _run_clean_outputs(manifest, monkeypatch, capsys)

        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["Vehicle.System_old.arxml"]

    def test_a_refused_manifest_moves_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Every refusal is decided before the move."""
        manifest = _outputs_manifest(
            tmp_path, dist_root="dist", views_root="views", arxml="dist/model.arxml", needs="needs.json"
        )
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "model.arxml").write_bytes(b"<AUTOSAR/>")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "declares outputs.needs: needs.json, which is outside the directories" in capsys.readouterr().err
        assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["model.arxml"]


class TestCliCleanOutputsRefusals:
    """The refusals, checked on the manifest's *fields*.

    A `dist_root` / `views_root` that is the project directory, absolute, or escapes through
    `..` is refused before anything is deleted, the message naming the field. The loader
    refuses most of these first, so both layers are asserted: the command through `main()`,
    and its own guard directly, because a guard never seen to fire proves nothing.
    """

    def _manifest_with(self, tmp_path: Path, **roots: str) -> Path:
        return _project_with_outputs(
            tmp_path,
            {
                "name": "test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": roots.get("dist_root", "dist"),
                "views_root": roots.get("views_root", "views"),
                "outputs": {
                    "arxml": "dist/model.arxml",
                    "needs": "dist/needs.json",
                },
            },
        )

    def _refuses(self, tmp_path: Path, manifest: Path, monkeypatch, capsys, field: str) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        assert field in capsys.readouterr().err
        assert (tmp_path / "dist" / "model.arxml").is_file()
        assert (tmp_path / "views" / "model.c4").is_file()

    def test_an_absolute_root_is_refused_by_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Absolute even when it points inside the project; the loader refuses it for every command."""
        manifest = self._manifest_with(tmp_path, dist_root=str(tmp_path / "dist"))
        self._refuses(tmp_path, manifest, monkeypatch, capsys, "dist_root")

    def test_every_other_command_refuses_the_absolute_root_too(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`check-model` refuses the absolute root too.

        `clean-outputs` is the moon graph's first task, so a manifest only it refused failed the
        build at step one while every other command accepted the project.
        """
        manifest = self._manifest_with(tmp_path, dist_root=str(tmp_path / "dist"))
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        assert "dist_root" in capsys.readouterr().err

    def test_a_windows_absolute_root_is_refused_on_every_platform(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`C:/dist` is refused as absolute on every platform, not only where `WindowsPath` says so.

        The message promises the manifest means the same on every machine; `Path.is_absolute()`
        disagrees across platforms, and `clean-outputs` would have rmtree'd `<project>/C:/dist`.
        """
        self._refuses_as_absolute(tmp_path, monkeypatch, capsys, dist_root="C:/dist")

    def test_a_backslash_windows_root_is_refused_too(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The spelling a Windows reader actually writes."""
        self._refuses_as_absolute(tmp_path, monkeypatch, capsys, views_root="C:\\views")

    def _refuses_as_absolute(self, tmp_path: Path, monkeypatch, capsys, **roots: str) -> None:
        """Refused, by name, **and for being absolute**.

        Not `_refuses`: a moved root is also refused by the outputs rule, which names the same
        field, so asserting the field alone would pass on the wrong rule.
        """
        (field,) = roots
        manifest = self._manifest_with(tmp_path, **roots)
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        error = capsys.readouterr().err
        assert field in error and "absolute" in error, error
        assert (tmp_path / "dist" / "model.arxml").is_file()
        assert (tmp_path / "views" / "model.c4").is_file()

    def test_a_relative_root_that_merely_contains_a_colon_is_still_accepted(self, tmp_path: Path) -> None:
        """The negative control: `build:2024` is a legal relative path; the rule is "absolute", not "a colon"."""
        project = require_project(str(self._manifest_with(tmp_path, dist_root="build:2024")))
        assert project.resolved_dist_root == str(tmp_path / "build:2024")

    def test_the_project_directory_itself_is_refused_by_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`dist_root: .` would delete the model."""
        manifest = self._manifest_with(tmp_path, dist_root=".")
        self._refuses(tmp_path, manifest, monkeypatch, capsys, "dist_root")

    def test_a_root_escaping_the_project_is_refused_by_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = self._manifest_with(tmp_path, views_root="../elsewhere")
        self._refuses(tmp_path, manifest, monkeypatch, capsys, "views_root")

    def _unvalidated_project(self, tmp_path: Path, **fields: str) -> SwcraftProject:
        """A manifest object built as the loader builds one, minus `_validate_project_paths`.

        The only way to put the command's own guard in front of a layout the loader refuses first."""
        project = SwcraftProject.model_validate(
            {"name": "test-project", "model_root": "model", "dist_root": "dist", "views_root": "views", **fields}
        )
        project._project_dir = tmp_path.resolve()
        return project

    #: ``(id, field, value)`` for the layouts ``_clean_root_problems`` refuses on its own.
    #: *value* is a callable because one layout needs the case's ``tmp_path``.
    _REFUSED_ROOTS = (
        ("a_windows_absolute_root", "dist_root", lambda tmp_path: "C:/dist"),
        ("the_project_directory", "dist_root", lambda tmp_path: "."),
        ("an_escape", "views_root", lambda tmp_path: "../elsewhere"),
        ("an_absolute_root", "dist_root", lambda tmp_path: str(tmp_path / "dist")),
    )

    @pytest.mark.parametrize(
        ("field", "value"),
        [(field, value) for _, field, value in _REFUSED_ROOTS],
        ids=[name for name, _, _ in _REFUSED_ROOTS],
    )
    def test_the_guard_itself_refuses(self, tmp_path: Path, field: str, value) -> None:
        """The guard refuses each layout, naming its field."""
        project = self._unvalidated_project(tmp_path, **{field: value(tmp_path)})
        problems = build_commands._clean_root_problems(project)
        assert [problem for problem in problems if field in problem]

    def test_the_guard_passes_a_manifest_that_declares_ordinary_roots(self, tmp_path: Path) -> None:
        """The negative control: the harness itself does not refuse everything."""
        assert build_commands._clean_root_problems(self._unvalidated_project(tmp_path)) == []

    def test_an_output_written_outside_both_roots_is_refused(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """An output declared outside both roots is refused rather than accommodated.

        It would survive a failing build as a stale artifact; deleting it instead would mean a
        build step that removes arbitrary paths on the strength of a manifest line.
        """
        manifest = _project_with_outputs(
            tmp_path,
            {
                "name": "test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/model.arxml", "needs": "needs.json"},
            },
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "outputs.needs" in err
        assert (tmp_path / "dist" / "model.arxml").is_file()


# Component-scoped ARXML exchange: `generate arxml --component` /
# `--all-components` and `import component`.


def _write_component_project(project_dir: Path) -> Path:
    """swcraft.yaml + a minimal model with one exportable SWC (SpeedSwc providing ISpeed over float32).

    Returns the manifest path."""
    manifest = _write_manifest(project_dir)
    model_root = project_dir / "model"
    # The element names a value type over a base type.
    write_model_element(
        model_root,
        {
            "name": "float32_base",
            "kind": "base_type",
            "size": 32,
            "encoding": "IEEE754",
            "native_declaration": "float32",
        },
    )
    write_model_element(model_root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
    dump_yaml(
        model_root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "float32"}],
        },
    )
    dump_yaml(
        model_root / "swc" / "body" / "SpeedSwc.yaml",
        {
            "name": "SpeedSwc",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    return manifest


def _export_speed_swc(tmp_path: Path) -> Path:
    """Build a source project and export SpeedSwc's closure; returns the component ARXML path."""
    _write_component_project(tmp_path / "source")
    arxml = tmp_path / "SpeedSwc.arxml"
    export_component(str(tmp_path / "source" / "model"), "SpeedSwc", str(arxml))
    return arxml


def _init_target_project(tmp_path: Path, name: str = "target") -> Path:
    """Scaffold a fresh project through the real ``swcraft init``; returns the manifest path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target / "swcraft.yaml"


# A valid SpeedSwc that diverges from the exported one (no ports): the update-eligible collision.
_DIVERGENT_SPEED_SWC = {"name": "SpeedSwc", "type": "application", "description": "diverged"}


class TestCliGenerateComponent:
    def test_component_without_output_uses_dist_default(self, tmp_path: Path, monkeypatch) -> None:
        """`--component NAME` with no --output targets <dist_root>/components/<NAME>.arxml.

        The default lives in the CLI layer, not in export_component()."""
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSwc"],
        )

        with patch("swcraft_tools.cli.build_commands.export_component") as mock_export:
            mock_export.return_value = ComponentExportReport(swc="SpeedSwc", output_path="unused", status="ok")
            main()  # must not raise SystemExit

        mock_export.assert_called_once()
        call_args = mock_export.call_args[0]
        assert call_args == (
            str(tmp_path / "model"),
            "SpeedSwc",
            str(tmp_path / "dist" / "components" / "SpeedSwc.arxml"),
        )

    def test_component_with_output_overrides_default(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_component_project(tmp_path)
        override = tmp_path / "elsewhere" / "custom.arxml"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--component",
                "SpeedSwc",
                "--output",
                str(override),
            ],
        )

        with patch("swcraft_tools.cli.build_commands.export_component") as mock_export:
            mock_export.return_value = ComponentExportReport(swc="SpeedSwc", output_path=str(override), status="ok")
            main()  # must not raise SystemExit

        mock_export.assert_called_once()
        assert mock_export.call_args[0] == (str(tmp_path / "model"), "SpeedSwc", str(override))

    def test_component_real_export_writes_default_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Unmocked end-to-end: the ARXML lands at the dist default and the report is printed."""
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSwc"],
        )

        main()  # must not raise SystemExit

        out_file = tmp_path / "dist" / "components" / "SpeedSwc.arxml"
        assert out_file.is_file()
        captured = capsys.readouterr().out
        assert "SpeedSwc" in captured
        assert "ok" in captured

    def test_component_unknown_swc_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "NoSuchSwc"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "NoSuchSwc" in err

    def test_component_ambiguous_swc_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Two SWC files sharing one `name` exit non-zero cleanly, not with a traceback."""
        manifest = _write_component_project(tmp_path)
        dump_yaml(tmp_path / "model" / "swc" / "chassis" / "SpeedSwcCopy.yaml", {"name": "SpeedSwc"})
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSwc"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "SpeedSwc" in err

    def test_component_and_all_components_mutually_exclusive(self, tmp_path: Path, monkeypatch) -> None:
        """argparse rejects the combination itself (usage error, exit 2)."""
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--component",
                "SpeedSwc",
                "--all-components",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2

    def test_component_with_non_arxml_format_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "diagrams", "--project", str(manifest), "--component", "SpeedSwc"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "arxml" in capsys.readouterr().err


class TestCliGenerateAllComponents:
    def test_all_components_calls_batch_export(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_component_project(tmp_path)
        out_dir = tmp_path / "components_out"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--all-components",
                "--output-dir",
                str(out_dir),
            ],
        )

        with patch("swcraft_tools.cli.build_commands.export_all_components", return_value=[]) as mock_batch:
            main()  # must not raise SystemExit

        mock_batch.assert_called_once()
        assert mock_batch.call_args[0] == (str(tmp_path / "model"), str(out_dir))

    def test_all_components_requires_output_dir(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--all-components"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--output-dir" in capsys.readouterr().err

    def test_output_without_component_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``--output`` only makes sense for a single-component export."""
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--output", str(tmp_path / "x.arxml")],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--output requires --component" in capsys.readouterr().err

    def test_output_dir_without_all_components_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``--output-dir`` only makes sense for the batch export."""
        manifest = _write_component_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--component",
                "SpeedSwc",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--output-dir requires --all-components" in capsys.readouterr().err

    def test_all_components_real_batch_writes_files_and_summary(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _write_component_project(tmp_path)
        out_dir = tmp_path / "components_out"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--all-components",
                "--output-dir",
                str(out_dir),
            ],
        )

        main()  # must not raise SystemExit

        assert (out_dir / "SpeedSwc.arxml").is_file()
        assert (out_dir / "export-summary.json").is_file()

    def test_all_components_exits_nonzero_when_any_swc_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A per-SWC failure never aborts the batch, but the exit code signals it for CI."""
        manifest = _write_component_project(tmp_path)
        dump_yaml(
            tmp_path / "model" / "swc" / "body" / "BrokenSwc.yaml",
            {
                "name": "BrokenSwc",
                "ports": [{"name": "pGone", "direction": "provide", "interface": "IUndeclared"}],
            },
        )
        out_dir = tmp_path / "components_out"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "generate",
                "arxml",
                "--project",
                str(manifest),
                "--all-components",
                "--output-dir",
                str(out_dir),
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert (out_dir / "SpeedSwc.arxml").is_file()
        assert not (out_dir / "BrokenSwc.arxml").exists()
        summary = json.loads((out_dir / "export-summary.json").read_text(encoding="utf-8"))
        assert {e["swc"]: e["status"] for e in summary} == {"BrokenSwc": "failed", "SpeedSwc": "ok"}


class TestCliImportComponent:
    def test_import_component_success(self, tmp_path: Path, monkeypatch, capsys) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        main()  # must not raise SystemExit

        target_model = tmp_path / "target" / "model"
        assert (target_model / "swc" / "components" / "SpeedSwc.yaml").is_file()
        assert (target_model / "interfaces" / "ISpeed.yaml").is_file()
        assert (model_element_path(target_model, "float32", kind="value_type")).is_file()
        out = capsys.readouterr().out
        assert "SpeedSwc" in out
        assert "created" in out

    def test_import_component_dry_run_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--dry-run",
            ],
        )

        main()  # must not raise SystemExit

        # Stated against the exact scaffold rather than by skipping `hardware/`:
        # a stray write there is a write too.
        assert _written_element_files(tmp_path / "target" / "model") == _SCAFFOLDED_ELEMENT_FILES
        assert "DRY RUN" in capsys.readouterr().out

    def _report_run(self, tmp_path: Path, monkeypatch, capsys, report: Path, *extra: str) -> str:
        """``import component --dry-run --report <report>`` over the SpeedSwc export; its stdout."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--report",
                str(report),
                *extra,
            ],
        )
        main()  # must not raise SystemExit
        return capsys.readouterr().out

    @pytest.mark.parametrize("name", ["report.md", "REPORT.MD"])
    def test_a_markdown_report_path_gets_the_markdown_form(
        self, tmp_path: Path, monkeypatch, capsys, name: str
    ) -> None:
        report = tmp_path / name
        out = self._report_run(tmp_path, monkeypatch, capsys, report, "--dry-run")
        markdown = report.read_bytes().decode("utf-8")
        assert markdown.startswith("# Component import report\\: ")
        assert "\n\n(DRY RUN -- no files were written)\n\n**SWC SpeedSwc\\: created**\n" in markdown
        assert "\n## CREATED 1 interface(s)\n\n- ISpeed\n" in markdown
        assert "\r" not in markdown and markdown.endswith("\n")
        assert out.startswith("Component import report: ")
        assert "SWC SpeedSwc: created\n" in out
        assert "#" not in out and "**" not in out

    def test_any_other_report_path_gets_the_printed_text_as_before(self, tmp_path: Path, monkeypatch, capsys) -> None:
        report = tmp_path / "report.txt"
        out = self._report_run(tmp_path, monkeypatch, capsys, report, "--dry-run")
        assert report.read_text(encoding="utf-8") == out.removesuffix("\n")

    def test_an_applied_run_writes_the_markdown_of_what_it_wrote(self, tmp_path: Path, monkeypatch, capsys) -> None:
        report = tmp_path / "report.md"
        self._report_run(tmp_path, monkeypatch, capsys, report)
        markdown = report.read_text(encoding="utf-8")
        assert "DRY RUN" not in markdown
        assert "\n## WROTE " in markdown

    def test_a_refused_run_writes_no_report_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        dump_yaml(tmp_path / "target" / "model" / "swc" / "body" / "SpeedSwc.yaml", _DIVERGENT_SPEED_SWC)
        report = tmp_path / "report.md"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--dry-run",
                "--report",
                str(report),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 3
        assert not report.exists()

    def test_an_applied_update_writes_the_markdown_of_the_update(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``--update --report r.md`` over a divergent target: the CLI's own file carries the update."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        divergent_file = tmp_path / "target" / "model" / "swc" / "body" / "SpeedSwc.yaml"
        dump_yaml(divergent_file, _DIVERGENT_SPEED_SWC)
        divergent_bytes = divergent_file.read_bytes()
        report = tmp_path / "r.md"
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--update",
                "--report",
                str(report),
            ],
        )

        main()  # must not raise SystemExit

        markdown = report.read_bytes().decode("utf-8")
        assert markdown.startswith("# Component import report\\: ")
        assert "\n**SWC SpeedSwc\\: updated**\n" in markdown
        assert "DRY RUN" not in markdown
        # The diff preview is a fenced block, verbatim, not Markdown.
        assert "\n```" in markdown
        assert "\r" not in markdown and markdown.endswith("\n")
        assert divergent_file.read_bytes() != divergent_bytes

    def _component_argv(self, arxml: Path, manifest: Path, report: Path, *extra: str) -> list[str]:
        return [
            "swcraft",
            "import",
            "component",
            "--source",
            str(arxml),
            "--project",
            str(manifest),
            "--report",
            str(report),
            *extra,
        ]

    @pytest.mark.parametrize(("name", "renderer"), [("report.txt", "render"), ("report.md", "render_markdown")])
    def test_a_report_that_cannot_be_rendered_after_the_apply_says_the_import_was_applied(
        self, tmp_path: Path, monkeypatch, capsys, name: str, renderer: str
    ) -> None:
        from swcraft_tools.component_exchange.report import ComponentImportReport

        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        report = tmp_path / name

        def render(self: Any) -> str:
            raise ValueError("injected render failure")

        monkeypatch.setattr(ComponentImportReport, renderer, render)
        monkeypatch.setattr("sys.argv", self._component_argv(arxml, target_manifest, report))
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err == "Error: the import was applied, but its report could not be rendered (injected render failure).\n"
        assert "Traceback" not in err
        assert (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").is_file()
        assert not report.exists()

    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    @pytest.mark.parametrize("name", ["report.txt", "report.md"])
    def test_a_report_that_cannot_be_written_after_the_run_says_the_run_completed(
        self, tmp_path: Path, monkeypatch, capsys, name: str, dry_run: bool
    ) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        report = tmp_path / name
        real_write_text = Path.write_text

        def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            if self == report:
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        argv = self._component_argv(arxml, target_manifest, report, *(["--dry-run"] if dry_run else []))
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exc_info:
            main()
        monkeypatch.undo()

        assert exc_info.value.code == 1
        out, err = capsys.readouterr()
        assert out.startswith("Component import report: ")
        done = "the dry run completed" if dry_run else "the import was applied"
        assert err == (
            f"Error: {done} and its report printed, but --report {report} could not be written "
            "(No space left on device).\n"
        )
        written = (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").is_file()
        assert written is not dry_run
        assert not report.exists()

    def test_a_report_directory_that_does_not_exist_is_refused_before_anything_is_written(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        report = tmp_path / "absent" / "report.md"
        monkeypatch.setattr("sys.argv", self._component_argv(arxml, target_manifest, report))
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert capsys.readouterr().err == f"Error: --report {report}: the directory {report.parent} does not exist.\n"
        assert _written_element_files(tmp_path / "target" / "model") == _SCAFFOLDED_ELEMENT_FILES

    def test_import_component_divergent_swc_without_update_exits_nonzero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An unresolved SWC-name conflict without --update exits non-zero, nothing written (all-or-nothing)."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        target_model = tmp_path / "target" / "model"
        divergent_file = target_model / "swc" / "body" / "SpeedSwc.yaml"
        dump_yaml(divergent_file, _DIVERGENT_SPEED_SWC)
        divergent_bytes = divergent_file.read_bytes()
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        # The one refusal `--update` resolves exits 3 (`cli.UPDATE_RESOLVES_EXIT`).
        assert exc_info.value.code == 3
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "SpeedSwc" in err
        assert divergent_file.read_bytes() == divergent_bytes
        assert list((target_model / "data_types").rglob("*.yaml")) == []
        assert list((target_model / "interfaces").glob("*.yaml")) == []

    def test_import_component_update_replaces_divergent_swc(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--update exits 0, replacing the exact existing file (not a re-derived path); the report carries the diff."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()
        target_model = tmp_path / "target" / "model"
        divergent_file = target_model / "swc" / "body" / "SpeedSwc.yaml"
        dump_yaml(divergent_file, _DIVERGENT_SPEED_SWC)
        divergent_bytes = divergent_file.read_bytes()
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--update",
            ],
        )

        main()  # must not raise SystemExit

        assert divergent_file.read_bytes() != divergent_bytes
        assert not (target_model / "swc" / "components" / "SpeedSwc.yaml").exists()
        out = capsys.readouterr().out
        assert "updated" in out
        assert "diff preview" in out

    def test_import_component_shared_conflict_exits_nonzero_even_with_update(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A divergent shared data type always rejects: --update never touches shared definitions.

        The divergent definition is the base type `float32_base`: a base type named `float32`
        would be another layer's element, a legal pair and no conflict.
        """
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        write_model_element(
            tmp_path / "target" / "model",
            {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"},
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--update",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "float32_base" in err

    def test_import_component_malformed_shape_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A whole-model export (2 SWCs) supplied as a component export is rejected before any write."""
        _write_component_project(tmp_path / "source")
        dump_yaml(
            tmp_path / "source" / "model" / "swc" / "body" / "OtherSwc.yaml",
            {
                "name": "OtherSwc",
                "type": "application",
                "ports": [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
                "handle_termination_and_restart": "no_support",
            },
        )
        whole_model = tmp_path / "whole_model.arxml"
        generate_arxml(str(tmp_path / "source" / "model"), str(whole_model))
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(whole_model), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "exactly one SWC" in err
        # Stated against the exact scaffold rather than by skipping `hardware/`:
        # a stray write there is a write too.
        assert _written_element_files(tmp_path / "target" / "model") == _SCAFFOLDED_ELEMENT_FILES

    def test_import_component_ambiguous_target_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Two target-project files sharing one name abort the import, even a name unrelated to it."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        dt_dir = tmp_path / "target" / "model" / "data_types" / "base"
        dump_yaml(dt_dir / "DupA.yaml", {"name": "Dup_T", "kind": "base_type", "size": 8, "encoding": "NONE"})
        dump_yaml(dt_dir / "DupB.yaml", {"name": "Dup_T", "kind": "base_type", "size": 16, "encoding": "NONE"})
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "Dup_T" in err
        # The CLI prints the exception on its own stderr, so it keeps each file's full path.
        assert str(dt_dir / "DupA.yaml") in err and str(dt_dir / "DupB.yaml") in err, err
        assert not (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").exists()

    def test_import_component_missing_source_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--source" in capsys.readouterr().err

    def test_import_component_with_an_unknown_sd_in_the_swcraft_group_exits_nonzero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The marker group is the writer's own only as a whole: an unknown SD in it aborts the import."""
        arxml = _export_speed_swc(tmp_path)
        anchor = '<SD GID="DATA-TYPES">'
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(text.replace(anchor, '<SD GID="VENDOR-FLAG">1</SD>' + anchor), encoding="utf-8", newline="\n")
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "vendor metadata" in captured.out + captured.err
        assert not (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").exists()

    def test_import_component_reads_an_export_carrying_both_marker_sds(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Control: the group written for a component with an access point (`ACCESS-POINTS`, `DATA-TYPES`) imports."""
        _write_component_project(tmp_path / "source")
        source_swc = tmp_path / "source" / "model" / "swc" / "body" / "SpeedSwc.yaml"
        declared = yaml.safe_load(source_swc.read_text(encoding="utf-8"))
        declared["runnables"] = [
            {"name": "SpeedRun", "min_start_interval": 0.01, "writes": [{"port": "pSpeed", "element": "speed"}]}
        ]
        dump_yaml(source_swc, declared)
        arxml = tmp_path / "SpeedSwc.arxml"
        export_component(str(tmp_path / "source" / "model"), "SpeedSwc", str(arxml))
        text = arxml.read_text(encoding="utf-8")
        assert '<SD GID="ACCESS-POINTS">' in text
        assert '<SD GID="DATA-TYPES">' in text
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        main()  # must not raise SystemExit

        target_swc = tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml"
        assert yaml.safe_load(target_swc.read_text(encoding="utf-8"))["runnables"][0]["writes"] == [
            {"port": "pSpeed", "element": "speed"}
        ]
        assert "created" in capsys.readouterr().out

    def test_import_component_with_text_inside_the_swcraft_group_exits_nonzero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Character data beside the marker is content the exporter did not write: the import aborts."""
        arxml = _export_speed_swc(tmp_path)
        anchor = '<SDG GID="SWCRAFT">'
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(text.replace(anchor, anchor + "payload"), encoding="utf-8", newline="\n")
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "vendor metadata" in captured.out + captured.err
        assert not (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").exists()

    def test_import_component_with_a_second_admin_data_beside_the_marker_exits_nonzero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A second `ADMIN-DATA` beside the marker's is reported, so the import aborts."""
        arxml = _export_speed_swc(tmp_path)
        anchor = "</ADMIN-DATA>"
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(
            text.replace(
                anchor, anchor + '<ADMIN-DATA><SDGS><SDG GID="VENDOR"><SD GID="X">1</SD></SDG></SDGS></ADMIN-DATA>'
            ),
            encoding="utf-8",
            newline="\n",
        )
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "<second ADMIN-DATA>, VENDOR" in captured.out + captured.err
        assert not (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").exists()

    def test_import_component_with_an_admin_data_outside_the_autosar_namespace_exits_nonzero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An `ADMIN-DATA` outside the AUTOSAR namespace beside the marker is reported, not walked: abort."""
        arxml = _export_speed_swc(tmp_path)
        anchor = "</ADMIN-DATA>"
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(
            text.replace(
                anchor,
                anchor + '<ADMIN-DATA xmlns=""><SDGS><SDG GID="VENDOR"><SD GID="X">1</SD></SDG></SDGS></ADMIN-DATA>',
            ),
            encoding="utf-8",
            newline="\n",
        )
        target_manifest = _init_target_project(tmp_path)
        capsys.readouterr()  # drop init output
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "element ADMIN-DATA outside the AUTOSAR namespace (no namespace)" in captured.out + captured.err
        assert not (tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml").exists()

    def test_import_component_report_flag_writes_rendered_text(self, tmp_path: Path, monkeypatch) -> None:
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        report_path = tmp_path / "import-report.txt"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--report",
                str(report_path),
            ],
        )

        main()  # must not raise SystemExit

        assert report_path.is_file()
        text = report_path.read_text(encoding="utf-8")
        assert "SpeedSwc" in text
        assert "created" in text

    def test_import_component_report_not_written_on_failure(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A failed import writes no ``--report`` file: the report is a success artifact."""
        target_manifest = _init_target_project(tmp_path)
        garbage = tmp_path / "garbage.arxml"
        garbage.write_text("not arxml at all", encoding="utf-8")
        report_path = tmp_path / "import-report.txt"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(garbage),
                "--project",
                str(target_manifest),
                "--report",
                str(report_path),
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert not report_path.exists()
        assert "Error" in capsys.readouterr().err

    def test_import_component_rejects_rhapsody_only_flags(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--packages/--force belong to `import model`; on `import component` they are refused, not ignored."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--packages",
                "P1",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "import model" in capsys.readouterr().err

    def test_import_component_rejects_the_replace_flag_without_update(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`--replace-access-lists` without `--update` decides nothing, so it is refused, not ignored."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--replace-access-lists",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--replace-access-lists applies only with --update" in capsys.readouterr().err

    def test_import_rhapsody_rejects_the_replace_flag(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A component-only flag on `import model` is refused, never ignored."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--replace-access-lists",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "--replace-access-lists applies only to 'import component'" in capsys.readouterr().err

    def test_the_replace_flag_reaches_the_import_and_the_regime_is_reported(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`--update --replace-access-lists` end to end: the list is replaced and stdout names the deciding signal."""
        source_manifest = _write_component_project(tmp_path / "source")
        source_swc = tmp_path / "source" / "model" / "swc" / "body" / "SpeedSwc.yaml"
        declared = yaml.safe_load(source_swc.read_text(encoding="utf-8"))
        declared["runnables"] = [{"name": "SpeedRun", "min_start_interval": 0.01}]
        dump_yaml(source_swc, declared)
        arxml = tmp_path / "SpeedSwc.arxml"
        export_component(str(tmp_path / "source" / "model"), "SpeedSwc", str(arxml))
        assert "ACCESS-POINTS" not in arxml.read_text(encoding="utf-8"), "the export must carry no access marker"
        assert source_manifest.is_file()

        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(arxml), "--project", str(target_manifest)],
        )
        main()
        target_swc = tmp_path / "target" / "model" / "swc" / "components" / "SpeedSwc.yaml"
        diverged = yaml.safe_load(target_swc.read_text(encoding="utf-8"))
        diverged["runnables"][0]["min_start_interval"] = 0.02
        diverged["runnables"][0]["writes"] = [{"port": "pSpeed", "element": "speed"}]
        dump_yaml(target_swc, diverged)
        capsys.readouterr()

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--update",
                "--replace-access-lists",
            ],
        )
        main()

        out = capsys.readouterr().out
        assert "ACCESS LISTS regime: replace-flag" in out
        assert yaml.safe_load(target_swc.read_text(encoding="utf-8"))["runnables"][0].get("writes") is None

    def test_import_rhapsody_rejects_update_flag(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--update belongs to `import component` only."""
        arxml = _export_speed_swc(tmp_path)
        target_manifest = _init_target_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(arxml),
                "--project",
                str(target_manifest),
                "--update",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "import component" in capsys.readouterr().err


# `swcraft resource-report`.
#
# The report is read-only: a successful run **exits 0**, even over a threshold; a missing or
# unresolvable project and an unreadable model file exit non-zero.
# `swcraft validate` is the gate: it runs check_resource_budgets inside
# check_cross_layer_consistency, so gating is tested through that one path.


def _write_hardware(model_root: Path, name: str, data: dict[str, Any]) -> None:
    """Write one hardware element; a core also gets its unrated partition (:func:`conftest.partition_on`)."""
    dump_yaml(model_root / "hardware" / f"{name}.yaml", {"name": name, **data})
    if data.get("kind") == "core":
        partition = partition_on(name)
        dump_yaml(
            model_root / "hardware" / f"{partition}.yaml",
            {"name": partition, "kind": "partition", "core": name},
        )


def _write_processor(model_root: Path, name: str = "CPU") -> None:
    """The one processor `hardware.no-processor` requires; the report itself never reads it."""
    _write_hardware(model_root, name, {"kind": "processor"})


def _write_task(model_root: Path, name: str, data: dict[str, Any]) -> None:
    dump_yaml(model_root / "tasks" / f"{name}.yaml", {"name": name, **data})


def _breaching_cpu_model(tmp_path: Path) -> Path:
    """A project whose CORE0 utilization (73%) exceeds its 70% threshold.

    No SWCs: the component checks would add unrelated issues to the ``validate`` assertions.
    """
    manifest = _write_manifest(tmp_path)
    model_root = tmp_path / "model"
    _write_processor(model_root)
    _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.7})
    _write_task(model_root, "Task_1ms", {"period": 0.001, "priority": 30, "partition": "PARTITION0", "wcet": "400us"})
    _write_task(model_root, "Task_5ms", {"period": 0.005, "priority": 20, "partition": "PARTITION0", "wcet": "900us"})
    _write_task(model_root, "Task_10ms", {"period": 0.01, "priority": 10, "partition": "PARTITION0", "wcet": "1500us"})
    return manifest


class TestCliResourceReport:
    def test_cli_resource_report_requires_project(self, monkeypatch) -> None:
        """swcraft resource-report with no --project -> SystemExit(1)."""
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_resource_report_unresolvable_project_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        """A --project path with no swcraft.yaml behind it exits non-zero."""
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "resource-report", "--project", str(tmp_path / "nope" / "swcraft.yaml")],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_cli_resource_report_prints_report_and_exits_0_over_threshold(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A resource over its threshold is *reported*, not gated: exit 0."""
        manifest = _breaching_cpu_model(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "Resource utilization report" in out
        assert "CORE0" in out
        assert "over budget" in out
        # The per-consumer breakdown is the point of the report.
        assert "Task_1ms" in out
        assert "Task_10ms" in out

    def test_cli_resource_report_no_budget_declared_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A processor and nothing else: nothing to budget, a zero count, exit 0."""
        manifest = _write_manifest(tmp_path)
        _write_processor(tmp_path / "model")
        _write_swc(tmp_path / "model", "SwcOne")
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "DECLARED 0 resource(s)" in out
        assert "No resources declared" not in out

    def test_cli_resource_report_malformed_yaml_exits_nonzero_not_traceback(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Malformed hardware YAML is reported at the CLI boundary, like impact."""
        manifest = _write_manifest(tmp_path)
        hardware = tmp_path / "model" / "hardware"
        hardware.mkdir(parents=True, exist_ok=True)
        (hardware / "CORE0.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    def test_cli_resource_report_layer_absent_does_not_read_tasks_without_a_data_event(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """No ``hardware/`` and no data-received event: ``tasks/`` is never opened, exit 0.

        ``check_hardware_topology`` and ``analyze_resource_budgets`` both return before their
        reads, so an unparseable ``tasks/`` file is opened by neither; the arrivals read ``swc/``
        alone (the next test's subject).

        Does not reach ``check-model`` or ``validate``, which refuse this model on
        ``hardware.layer-absent``.
        """
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        tasks = tmp_path / "model" / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit: the unparseable file is never opened

        captured = capsys.readouterr()
        assert captured.err == ""
        assert "TOPOLOGY 1 finding(s)" in captured.out
        assert "hardware.layer-absent" in captured.out

    def test_cli_resource_report_layer_absent_reads_swc_for_the_arrivals(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """No ``hardware/``: an unparseable ``swc/`` file exits 1, named by its storage name.

        ``data_arrival.arrivals`` parses ``swc/`` on every run, and whether the file holds a
        data-received event cannot be known without reading it.
        """
        manifest = _write_manifest(tmp_path)
        swc = tmp_path / "model" / "swc" / "group"
        swc.mkdir(parents=True, exist_ok=True)
        (swc / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Error:" in captured.err
        assert "Broken.yaml" in captured.err

    def test_cli_resource_report_absurd_pool_size_renders_exactly(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A `size` past ~1e308 renders exactly: the figures are `Fraction`s, never a float."""
        manifest = _write_manifest(tmp_path)
        _write_hardware(
            tmp_path / "model",
            "Huge",
            {"kind": "memory", "type": "RAM", "size": 10**400, "accessible": ["CORE0"], "threshold": 0.8},
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert f"{8 * 10**399} byte budget" in out

    def test_cli_validate_exits_nonzero_on_budget_breach(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`validate` is the gate: an unmocked budget breach fails it through check_cross_layer_consistency."""
        manifest = _breaching_cpu_model(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])

        with patch("swcraft_tools.cli.build_commands.generate_arxml") as mock_gen:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_gen.assert_not_called()
        err = capsys.readouterr().err
        assert "ERROR:" in err
        assert "CORE0" in err
        assert "threshold" in err

    def test_cli_validate_passes_when_budget_within_threshold(self, tmp_path: Path, monkeypatch) -> None:
        """The same model under its threshold passes validate: the breach fails it, not the budget."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_processor(model_root)
        _root_composition(model_root)
        _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.7})
        _write_task(
            model_root, "Task_1ms", {"period": 0.001, "priority": 30, "partition": "PARTITION0", "wcet": "250us"}
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])

        with patch(
            "swcraft_tools.cli.build_commands.generate_arxml", return_value=_stub_generated_arxml(tmp_path)
        ) as mock_gen:
            main()  # must not raise SystemExit

        mock_gen.assert_called_once()

    # The command composes `check_hardware_topology` into the report. The
    # renderer's own half (it renders no topology when handed none) is in
    # `test_resource_report.py`.

    def test_cli_resource_report_reports_a_task_in_an_undeclared_partition(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A task whose `partition` names no declared element is reported by rule id and wording.

        It is in no block and no budget finding, so the budget section beside it is unchanged.
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_processor(model_root)
        _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.7})
        _write_task(
            model_root, "Task_orphan", {"period": 0.01, "priority": 10, "partition": "PARTITION9", "wcet": "9000us"}
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "TOPOLOGY 1 finding(s)" in out
        assert "  - hardware.undeclared-partition: " in out
        assert "Task_orphan" in out
        assert "PARTITION9" in out
        # The budget half is untouched: the orphan is still no core's consumer.
        assert "FINDINGS 0 error(s), 0 informational" in out
        assert "  CONSUMERS 0 task(s), 0 without a declared budget" in out

    def test_cli_resource_report_reports_an_unreachable_memory(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`hardware.unreachable-memory` is reported here.

        The budget arithmetic charges the memory whatever `accessible` says, so without the
        section the report would show a sound block for a design-rule violation.
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_processor(model_root)
        _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.7})
        _write_hardware(model_root, "CORE1", {"kind": "core", "threshold": 0.7})
        _write_hardware(
            model_root,
            "RAM0",
            {"kind": "memory", "type": "RAM", "size": 65536, "accessible": ["CORE0"], "threshold": 0.8},
        )
        dump_yaml(
            model_root / "swc" / "SwcOne.yaml",
            {
                "name": "SwcOne",
                "type": "application",
                "runnables": [{"name": "Run_one"}],
                "memory": {"RAM0": 128},
            },
        )
        _write_task(
            model_root,
            "Task_10ms",
            {
                "period": 0.01,
                "priority": 10,
                "partition": "PARTITION1",
                "wcet": "100us",
                "runnables": [{"swc": "SwcOne", "runnable": "Run_one"}],
            },
        )
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "TOPOLOGY 1 finding(s)" in out
        assert (
            "  - hardware.unreachable-memory: Task 'Task_10ms' runs in partition 'PARTITION1' on core 'CORE1', "
            "but SWC 'SwcOne' budgets memory 'RAM0', which is not accessible from 'CORE1'." in out
        )
        # It reports; it does not gate. The memory is charged as it always was.
        assert "RESOURCE RAM0 (memory, size 65536 bytes, strict)" in out
        assert "    - SwcOne: 128 bytes" in out

    def test_cli_resource_report_exits_0_with_topology_findings(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`resource-report` exits 0 over topology findings that `check-model` refuses (asserted beside it)."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_processor(model_root)
        _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.7})
        _write_task(
            model_root, "Task_orphan", {"period": 0.01, "priority": 10, "partition": "PARTITION9", "wcet": "9000us"}
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])
        main()  # must not raise SystemExit
        assert "TOPOLOGY" in capsys.readouterr().out

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_cli_resource_report_reports_the_topology_of_a_model_with_no_budget(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A model with no core or memory still gets its topology.

        `analyze_resource_budgets` returns before reading tasks or SWCs, but
        `check_hardware_topology` is called by `cmd_resource_report` independently.
        """
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        (model_root / "hardware").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "DECLARED 0 resource(s)" in out
        assert "TOPOLOGY 1 finding(s)" in out
        assert "  - hardware.no-processor: " in out

    def test_cli_resource_report_reports_an_absent_hardware_layer(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """No `hardware/` directory at all: `hardware.layer-absent` is reported."""
        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "DECLARED 0 resource(s)" in out
        assert "TOPOLOGY 1 finding(s)" in out
        assert "  - hardware.layer-absent: " in out

    def test_cli_resource_report_prints_no_topology_section_for_a_clean_model(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A clean model's report has no topology section.

        `_breaching_cpu_model` is over its threshold yet topologically sound: a budget breach
        is not a topology finding.
        """
        manifest = _breaching_cpu_model(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "TOPOLOGY" not in out
        assert "over budget" in out

    def test_cli_resource_report_does_not_import_the_renderer_into_the_gate(self) -> None:
        """`cmd_resource_report` makes the topology call itself; the renderer does not.

        The renderer's half is read off its **AST**: a text search cannot tell prose naming
        `check_hardware_topology` from a call.

        Does **not** reach whether the records reach the output (the rendering tests above).
        """
        assert "check_hardware_topology(model_root)" in inspect.getsource(cli.cmd_resource_report)

        renderer = ast.parse(Path(inspect.getsourcefile(resource_report_module)).read_text(encoding="utf-8"))
        named = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(renderer)
            if isinstance(node, ast.Name | ast.Attribute)
        }

        assert "check_hardware_topology" not in named

    def test_cli_resource_report_advisory_findings_are_visible_here_only(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An advisory breach's `info` findings are invisible to validate and visible in this report."""
        manifest = _write_manifest(tmp_path)
        model_root = tmp_path / "model"
        _write_processor(model_root)
        _root_composition(model_root)
        _write_hardware(model_root, "CORE0", {"kind": "core", "threshold": 0.1, "enforcement": "advisory"})
        _write_task(
            model_root, "Task_1ms", {"period": 0.001, "priority": 30, "partition": "PARTITION0", "wcet": "400us"}
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        with patch("swcraft_tools.cli.build_commands.generate_arxml", return_value=_stub_generated_arxml(tmp_path)):
            main()  # advisory breach must not fail the gate

        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])
        main()

        out = capsys.readouterr().out
        assert "[info]" in out
        assert "over budget" in out


def _task_09_project(tmp_path: Path) -> Path:
    """`_breaching_cpu_model` (CORE0 over its 70%) plus task 09's memory: 14336 of a strict 32768-byte RAM at 0.75."""
    manifest = _breaching_cpu_model(tmp_path)
    model_root = tmp_path / "model"
    _write_hardware(
        model_root,
        "RAM",
        {"kind": "memory", "type": "RAM", "size": 32768, "accessible": ["CORE0"], "threshold": 0.75},
    )
    _swc(model_root, "Alpha", memory={"RAM": 8192})
    _swc(model_root, "Beta", memory={"RAM": 6144})
    return manifest


class TestCliResourceReportFormsAndFit:
    """`--format json` and the fit question: two forms of one report, and the exit contract unchanged."""

    def _run(self, monkeypatch, capsys, manifest: Path, *flags: str) -> tuple[str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest), *flags])
        main()  # always exits 0 on a successful run: it must not raise
        captured = capsys.readouterr()
        return captured.out, captured.err

    def _refused(self, monkeypatch, capsys, manifest: Path, *flags: str) -> tuple[int, str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest), *flags])
        with pytest.raises(SystemExit) as exc_info:
            main()
        captured = capsys.readouterr()
        return exc_info.value.code, captured.out, captured.err

    def test_the_json_document_is_the_renderers_and_exits_0_over_budget(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json")

        document = json.loads(out)
        assert list(document) == ["schemaVersion", "findings", "topology", "fit", "cores", "memories"]
        assert document["schemaVersion"] == 1
        assert [core["status"] for core in document["cores"]] == ["over budget"]
        assert out == resource_report_module.format_report_json(
            resource_report_module.resource_report(
                str(tmp_path / "model"), check_hardware_topology(str(tmp_path / "model")).records
            )
        )

    def test_an_unknown_status_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)
        _swc(tmp_path / "model", "Gamma")

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "1")

        (memory,) = json.loads(out)["memories"]
        assert memory["status"].startswith("unknown")
        assert (memory["fit"]["fits"], memory["fit"]["within_size"]["fits"]) == (None, None)

    def test_the_document_names_no_storage_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "12288")

        model_root = tmp_path / "model"
        assert str(tmp_path) not in out and model_root.as_posix() not in out
        # Every directory the model's files sit in, read off the tree rather than listed here.
        for directory in {path.parent.relative_to(model_root) for path in model_root.rglob("*.yaml")}:
            assert f"{directory.as_posix()}/" not in out and f"{directory}{os.sep}" not in out
        assert ".yaml" not in out

    def test_the_fit_bytes_question_answers_every_memory_and_no_core(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "12288")

        document = json.loads(out)
        assert document["fit"] == {"kind": "memory", "asked": "12288"}
        assert document["memories"][0]["fit"]["answer"] == "fits physically, breaks the threshold by 2048 bytes"
        assert all("fit" not in core for core in document["cores"])

    def test_the_text_form_carries_the_fit_line(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--fit-bytes", "12288")

        assert "  PHYSICAL HEADROOM 18432 bytes of a 32768 byte size\n" in out
        assert "  FIT 12288 bytes: fits physically, breaks the threshold by 2048 bytes\n" in out
        assert out.count("  FIT ") == 1
        assert out.endswith("\n") and not out.endswith("\n\n")

    def test_a_fit_utilization_fraction_is_read_exactly(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json", "--fit-utilization", "125/1000")

        document = json.loads(out)
        assert document["fit"] == {"kind": "core", "asked": "12.50%"}
        assert document["cores"][0]["fit"]["answer"] == "fits physically, breaks the threshold by 15.50%"
        assert all("fit" not in memory for memory in document["memories"])

    @pytest.mark.parametrize(
        "flags",
        [
            ["--fit-bytes", "-1"],
            ["--fit-bytes", "12 KB"],
            ["--fit-bytes", "1.5"],
            ["--fit-utilization", "1e999"],
            ["--fit-utilization", "-0.1"],
            ["--fit-utilization", "5%"],
            ["--fit-bytes", "1", "--fit-utilization", "0.05"],
            ["--format", "yaml"],
        ],
        ids=["negative", "units", "fraction", "unbounded", "negative-share", "percent-sign", "both", "format"],
    )
    def test_an_amount_argparse_cannot_read_is_a_usage_error(
        self, tmp_path: Path, monkeypatch, capsys, flags: list[str]
    ) -> None:
        manifest = _task_09_project(tmp_path)

        code, out, err = self._refused(monkeypatch, capsys, manifest, "--format", "json", *flags)

        assert code == 2
        assert out == ""
        assert "usage:" in err

    @pytest.mark.parametrize("digits", [65, 5000], ids=["past-the-bound", "past-int-digit-limit"])
    def test_too_many_byte_digits_is_a_worded_usage_error(
        self, tmp_path: Path, monkeypatch, capsys, digits: int
    ) -> None:
        """Bounded as ``--fit-utilization`` is, so 5000 digits never reach ``int()``'s own limit unworded."""
        manifest = _task_09_project(tmp_path)

        code, out, err = self._refused(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "9" * digits)

        assert code == 2
        assert out == ""
        assert f"expected a whole number of bytes of at most 64 digits, got {digits} digits" in err

    def test_sixty_four_byte_digits_are_read(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _task_09_project(tmp_path)

        out, _ = self._run(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "9" * 64)

        assert json.loads(out)["fit"]["kind"] == "memory"

    def test_an_unreadable_model_exits_1_with_stdout_empty(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_manifest(tmp_path)
        hardware = tmp_path / "model" / "hardware"
        hardware.mkdir(parents=True, exist_ok=True)
        (hardware / "CORE0.yaml").write_text("name: [unclosed\n", encoding="utf-8")

        code, out, err = self._refused(monkeypatch, capsys, manifest, "--format", "json", "--fit-bytes", "1")

        assert code == 1
        assert out == ""
        assert err.startswith("Error: ")

    def test_a_pin_mismatch_warns_on_stderr_and_stdout_stays_one_document(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")

        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json")

        assert err.startswith("Warning: ") and "9.9.9" in err
        assert json.loads(out)["schemaVersion"] == 1


# The toolchain pin


def _pinned_project(project_dir: Path, version: str | None) -> Path:
    """A `swcraft query`-grade project whose manifest pins *version*; a git repository for `diff`."""
    manifest = write_query_project(project_dir)
    # `generate arxml` refuses a data element typed by a base type, so the pinned copy
    # re-points `ISpeed` at the value type `Speed`; the query project keeps the edge its
    # assertions pin.
    dump_yaml(
        project_dir / "model" / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "Speed"}],
        },
    )
    # `generate arxml` also refuses a base type with no `native_declaration`: the pinned
    # copy declares them.
    write_model_element(
        project_dir / "model",
        {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float"},
    )
    write_model_element(
        project_dir / "model",
        {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    )
    if version is not None:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = version
        manifest.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    _git("init", cwd=project_dir)
    _git("add", "-A", cwd=project_dir)
    _git("commit", "-m", "initial commit", cwd=project_dir)
    return manifest


#: The read-only six, each with the argv that makes it produce an answer.
_READ_ONLY_INVOCATIONS = {
    "list": ["list"],
    "diff": ["diff"],
    "impact": ["impact", "ISpeed"],
    "metrics": ["metrics"],
    "resource-report": ["resource-report"],
    "query": ["query", "swc"],
}

#: The commands that write, gate or hand the project to another process, each with the
#: argv that reaches the check. Keyed by invocation: `import component` and `import model`
#: reach the pin down different paths (:func:`cli.project_args.resolve_project` and
#: :func:`cli.project_args.check_toolchain_pin_of`).
#:
#: The `import` forms name a source file that does not exist, so the run must fail on the
#: pin, which is checked first. `mcp-serve` is here because the refusal must precede the exec.
_WRITING_INVOCATIONS = {
    "generate": ["generate", "arxml"],
    "validate": ["validate"],
    "check-model": ["check-model"],
    "clean-outputs": ["clean-outputs"],
    "import component": ["import", "component", "--source", "no-such-export.arxml"],
    "import model": ["import", "model", "--source", "no-such-export.arxml"],
    "import requirements": ["import", "requirements", "--source", "no-such-needs.json"],
    "mcp-serve": ["mcp-serve"],
    # The `import` forms' reason: the configuration file does not exist, and the run
    # must fail on the pin, which is checked before the file is read.
    "experiment": ["experiment", "allocation", "--config", "no-such-config.yaml"],
    # It writes its report and can gate a pipeline with `--fail-on`, so a mismatch refuses.
    "change-report": ["change-report", "--no-diagrams"],
    # It writes the passport, which should describe the release the manifest pins.
    "passport": ["passport"],
}

#: The writing commands whose pin refusal withholds the pinned value, naming the field and
#: this release only (a passport's stderr travels with a bug report).
_WITHHOLDS_THE_PIN = frozenset({"passport"})


def _assert_the_pin_refusal(error: str, command: str) -> None:
    """*error* is the pin refusal *command* prints for a ``9.9.9`` pin."""
    assert error.startswith("Error: ") and __version__ in error and "install" in error.lower()
    if command in _WITHHOLDS_THE_PIN:
        assert error == f"Error: {toolchain_pin.withheld_pin_sentence()}\n" and "9.9.9" not in error
    else:
        assert "9.9.9" in error


class TestToolchainPin:
    """`toolchain_version` against the running toolchain.

    A command that **writes or gates** refuses a project pinned to another release before it
    writes anything. The **read-only six** report the same sentence on stderr and answer
    anyway: their guide sentence promises success whenever the project could be read, and a
    pin mismatch is not a failure to read.

    Not here: whether the pin is newer or older. The check is equality, and the sentence
    names both versions.
    """

    def test_a_matching_pin_is_silent_and_the_command_writes(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, __version__)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        main()

        assert "toolchain_version" not in capsys.readouterr().err
        assert (tmp_path / "dist" / "model.arxml").is_file()

    def test_a_project_without_the_field_is_unchanged(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Every manifest authored before the field existed pins nothing."""
        manifest = _pinned_project(tmp_path, None)
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        main()

        captured = capsys.readouterr()
        assert "toolchain_version" not in captured.err
        assert "SpeedSwc" in captured.out

    @pytest.mark.parametrize("command", sorted(_WRITING_INVOCATIONS))
    def test_a_mismatched_pin_stops_a_writing_command(self, tmp_path: Path, monkeypatch, capsys, command: str) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", *_WRITING_INVOCATIONS[command], "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        _assert_the_pin_refusal(capsys.readouterr().err, command)

    def test_the_refusal_line_opens_with_the_extensions_pin_prefix(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The refusal line opens with the extension's ``PIN_REFUSAL_PREFIX``, read from ``helpLinks.ts``.

        A reworded refusal fails here rather than silently dropping the *Learn more* button.
        Does not reach ``passport``'s withheld sentence, which the extension never runs; the
        ``Warning:`` form must not match.
        """
        literal = help_links_constant("PIN_REFUSAL_PREFIX")
        found = re.fullmatch(r'"([^"]+)"', literal)
        assert found is not None, f"helpLinks.ts declares PIN_REFUSAL_PREFIX as one string literal: {literal}"
        prefix = found.group(1)
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        with pytest.raises(SystemExit):
            main()

        error = capsys.readouterr().err
        assert error.startswith(prefix), error

        # The read-only form on the same project: its `Warning:` line must not
        # match, or a read-only command's notification would offer the button.
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])
        main()
        warning = capsys.readouterr().err
        assert warning.startswith("Warning:") and "9.9.9" in warning, warning
        assert not any(line.startswith(prefix) for line in warning.splitlines()), warning

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["import", "model", "--source", "no-such-export.arxml", "--update"], "--update applies only"),
            (["import", "model"], "--source is required"),
            (["import", "component"], "--source is required"),
            (["import", "requirements"], "--source is required"),
            (["import", "requirements", "--source", "x.json", "--force"], "--force applies only to 'import model'"),
            (["import", "component", "--packages", "P", "--source", "x.arxml"], "--packages/--force apply only"),
            (
                ["import", "model", "--source", "x.arxml", "--replace-access-lists"],
                "--replace-access-lists applies only to 'import component'",
            ),
            (
                ["import", "component", "--source", "x.arxml", "--replace-access-lists"],
                "--replace-access-lists applies only with --update",
            ),
            # The positive control: `cmd_query` already refused before resolving the project.
            # Only the shapes `_refuse_unanswerable_argv` knows are decided that early.
            (["query", "swc", "--dependents", "ISpeed"], "either a LAYER to slice"),
        ],
    )
    def test_an_unanswerable_argv_is_refused_before_the_pin_is_read(
        self, tmp_path: Path, monkeypatch, capsys, argv: list[str], expected: str
    ) -> None:
        """One ordering for every command: argv first, then the project.

        Guards a defect: the rhapsody branch of `cmd_import` checked the pin above its usage
        refusals, so the message depended on a field the user did not write.

        Does not reach the exit code (1 either way) or a well-formed command line, where the
        pin is the only refusal left.
        """
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", *argv, "--project", str(manifest)])

        with pytest.raises(SystemExit):
            main()

        error = capsys.readouterr().err
        assert expected in error, error
        assert "9.9.9" not in error, (
            f"`swcraft {' '.join(argv)}` reported the toolchain pin for a command line it cannot "
            f"answer at all; the argv refusal comes first, whatever project it was pointed at:\n{error}"
        )

    def test_the_writing_command_is_stopped_before_it_writes(self, tmp_path: Path, monkeypatch) -> None:
        """The half a message cannot state: before any output and before any write."""
        manifest = _pinned_project(tmp_path, "9.9.9")
        (tmp_path / "dist").mkdir(exist_ok=True)
        (tmp_path / "dist" / "keep.txt").write_text("still here", encoding="utf-8")
        for argv in (["generate", "arxml"], ["clean-outputs"]):
            monkeypatch.setattr("sys.argv", ["swcraft", *argv, "--project", str(manifest)])
            with pytest.raises(SystemExit):
                main()

        assert (tmp_path / "dist" / "keep.txt").is_file(), "clean-outputs deleted despite the refusal"
        assert not (tmp_path / "dist" / "model.arxml").exists(), "generate wrote despite the refusal"

    @pytest.mark.parametrize("command", sorted(_READ_ONLY_INVOCATIONS))
    def test_a_mismatched_pin_still_answers_a_read_only_command(
        self, tmp_path: Path, monkeypatch, capsys, command: str
    ) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", *_READ_ONLY_INVOCATIONS[command], "--project", str(manifest)])

        main()  # must not raise SystemExit

        captured = capsys.readouterr()
        assert "9.9.9" in captured.err and __version__ in captured.err
        assert captured.out.strip(), f"`swcraft {command}` reported the pin and then answered nothing"

    def test_both_sides_report_one_sentence(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The refusal and the warning differ in what follows, never in what they say."""
        manifest = _pinned_project(tmp_path, "9.9.9")

        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])
        main()
        advisory = capsys.readouterr().err

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])
        with pytest.raises(SystemExit):
            main()
        refusal = capsys.readouterr().err

        sentence = cli.toolchain_pin_problem(require_project(str(manifest)))
        assert sentence is not None
        assert sentence in advisory and sentence in refusal
        assert advisory.replace("Warning: ", "") == refusal.replace("Error: ", "")

    def test_mcp_serve_checks_before_its_exec(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])

        with patch("os.execvp") as execvp:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        execvp.assert_not_called()
        assert "9.9.9" in capsys.readouterr().err

    def test_agent_checks_the_project_in_its_own_remainder(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The remainder is another program's command line, and `--project` in it is still this project."""
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat", "--project", str(manifest), "--scoped"])

        with patch("os.execvp") as execvp:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        execvp.assert_not_called()
        assert "9.9.9" in capsys.readouterr().err

    def test_agent_checks_the_project_spelled_with_an_equals_sign(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``--project=P`` is found too: losing that branch would silently skip the pin check."""
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat", f"--project={manifest}", "--scoped"])

        with patch("os.execvp") as execvp:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        execvp.assert_not_called()
        assert "9.9.9" in capsys.readouterr().err

    def test_agent_does_not_read_a_project_past_the_passthrough_separator(self, tmp_path: Path, monkeypatch) -> None:
        """Past a bare ``--`` a ``--project`` is the assistant's passthrough: not read, and the exec happens."""
        manifest = _pinned_project(tmp_path, "9.9.9")
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat", "--", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        execvp.assert_called_once()

    def test_agent_with_no_project_in_its_remainder_still_execs(self, monkeypatch) -> None:
        """Nothing to check: an empty or `--project`-less remainder is the assistant's own answer."""
        monkeypatch.setattr("sys.argv", ["swcraft", "agent", "chat"])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        execvp.assert_called_once()

    def test_a_matching_pin_lets_the_exec_happen(self, tmp_path: Path, monkeypatch) -> None:
        manifest = _pinned_project(tmp_path, __version__)
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(manifest)])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        execvp.assert_called_once()

    def test_a_manifest_the_exec_commands_cannot_read_is_left_to_the_program_they_hand_it_to(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An unreadable manifest is left to the server or assistant it is handed to, by decision.

        A refusal here would move that report into the wrong process, so the check answers only
        when the manifest parses.
        """
        monkeypatch.setattr("sys.argv", ["swcraft", "mcp-serve", "--project", str(tmp_path / "nope.yaml")])
        monkeypatch.setattr(handover, "_exec_replaces_this_process", lambda: True)

        with patch("os.execvp") as execvp:
            main()

        execvp.assert_called_once()

    def test_the_advisory_commands_are_the_generated_pages_read_only_ones(self) -> None:
        """The advisory commands equal the generated page's read-only set.

        The `cli` package cannot import `guide_reference.cli`, so the set is written twice and
        this stops the copies drifting.
        """
        assert project_args._PIN_ADVISORY_COMMANDS == set(guide_reference_cli.ALWAYS_EXITS_ZERO) | set(
            guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST
        )

    def test_the_pin_is_the_swcraft_cli_s_and_neither_server_reads_it(self) -> None:
        """The pin is the `swcraft` CLI's, and neither server reads it -- a limit, not an omission.

        `swcraft mcp-serve` and `swcraft agent` check the pin, then exec the *module*
        (:func:`handover._hand_over`). The `swcraft-mcp` / `swcraft-agent` console scripts and
        `python -m swcraft_mcp.server` never pass through the check. Asserted as an absence
        *and* a presence (`swcraft_tools` reads the field), since an absence alone goes green
        on a rename. The release note's § *What the pin does not reach* names this limit; both
        change together.
        """
        readers = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for root in ("packages/mcp/src", "packages/agent/src", "packages/tools/src")
            for path in (REPO_ROOT / root).rglob("*.py")
            if "toolchain_version" in path.read_text(encoding="utf-8")
        )

        assert readers, "nothing reads toolchain_version at all -- the pin has gone, not moved"
        assert [name for name in readers if not name.startswith("packages/tools/src/")] == [], (
            f"{readers} read toolchain_version outside the swcraft CLI. That is a deliberate boundary: "
            "widening it means updating docs/release-notes/0.7.0.md § 'What the pin does not reach' too"
        )

    def test_every_subcommand_that_takes_a_project_is_in_one_of_the_two_tables(self) -> None:
        """Every subcommand that takes `--project` is in one of the two tables, derived from the parser.

        The tables are hand-written; a subcommand added later that called ``require_project``
        directly would otherwise ignore the pin with every gate green.

        Does **not** reach `swcraft agent`, whose ``argparse.REMAINDER`` declares no
        ``--project``; its two cases above are written by hand.
        """
        declared = {
            name for name, options in parser_subcommand_options(build_parser()).items() if "--project" in options
        }
        covered = {argv[0] for argv in (*_READ_ONLY_INVOCATIONS.values(), *_WRITING_INVOCATIONS.values())}

        assert declared - covered == set(), (
            f"{sorted(declared - covered)} declare --project and are in neither invocation table, so the "
            "toolchain pin is unasserted for them"
        )
        assert covered - declared == set(), (
            f"{sorted(covered - declared)} are in an invocation table and declare no --project; the table "
            "is describing a command line the parser no longer accepts"
        )


@pytest.mark.parametrize("argv", [["impact", "SwcOne"], ["query", "swc"], ["check-model"], ["list"]])
def test_an_unparseable_file_is_named_by_its_full_path_on_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """An unparseable file is named by its full path on the terminal, the user's own.

    MCP clients and host logs get the storage name; ``full_path`` is the CLI's choice of
    ``ElementParseError.render``.
    """
    manifest = _write_manifest(tmp_path)
    model_root = tmp_path / "model"
    _write_swc(model_root, "SwcOne")
    broken = model_root / "swc" / "SwcOne.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.argv", ["swcraft", *argv, "--project", str(manifest)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert f"Error: Invalid SWC YAML in {broken.resolve()}: " in err, err


class TestALoadRefusalIsOneLinePerField:
    """``check-model``'s refusal of a malformed file: one ``Error:`` line per field."""

    @staticmethod
    def _refuse(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        text: str,
        manifest_data: dict | None = None,
    ) -> tuple[Path, list[str]]:
        manifest = _write_manifest(tmp_path, manifest_data)
        broken = tmp_path / "model" / "tasks" / "Task_bad.yaml"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text(text, encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 1
        return broken.resolve(), capsys.readouterr().err.splitlines()

    def test_every_stderr_line_is_error_the_file_the_field_and_the_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken, lines = self._refuse(
            tmp_path, monkeypatch, capsys, "name: Task_bad\nperiod: .nan\npriority: x\npartition: P\n"
        )
        assert lines == [
            f"Error: Invalid task YAML in {broken}: period: Input should be a finite number",
            f"Error: Invalid task YAML in {broken}: priority: Input should be a valid integer, unable to parse string "
            "as an integer",
        ]

    def test_a_yaml_syntax_error_is_one_line_placed_by_line_and_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken, lines = self._refuse(tmp_path, monkeypatch, capsys, "name: Task_bad\n  period: [1\n")
        assert lines == [
            f"Error: Invalid task YAML in {broken}: line 2, column 9: mapping values are not allowed in this context"
        ]

    def test_an_undeclared_manifest_key_adds_no_line_to_a_load_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `manifest.unknown-key` is a finding of a model that loaded: a load refusal stops
        # before it, so the terminal shows the file's fields alone, each under `Error: `.
        manifest = {
            "name": "test-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
            "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
            "modle_root": "model",
        }
        broken, lines = self._refuse(
            tmp_path, monkeypatch, capsys, "name: Task_bad\nperiod: .nan\npriority: 1\npartition: P\n", manifest
        )
        assert lines == [f"Error: Invalid task YAML in {broken}: period: Input should be a finite number"]


class TestTheExportRouteNamesNoTemporaryTreeOnTheTerminal:
    """A writer refusal through a component export names the layer directory, on the terminal too.

    The export's temporary directory is deleted before the refusal is printed, so a full path
    would name nothing the user can open.
    """

    _REFUSAL = "Base type(s) ['float32_base'] in data_types/base/ declare no native_declaration"

    def _without_native_declaration(self, tmp_path: Path) -> Path:
        manifest = _write_component_project(tmp_path)
        base_type = {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        dump_yaml(model_element_path(tmp_path / "model", "float32_base", kind="base_type"), base_type)
        return manifest

    def test_the_single_export_prints_the_layer_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = self._without_native_declaration(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "SpeedSwc"]
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        terminal = captured.out + captured.err
        assert "swcraft-component-export" not in terminal, terminal
        assert f"Error: {self._REFUSAL}" in captured.err, terminal
        assert not (tmp_path / "dist" / "components" / "SpeedSwc.arxml").exists()

    def test_the_batch_export_prints_the_layer_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = self._without_native_declaration(tmp_path)
        out_dir = tmp_path / "components_out"
        argv = ["swcraft", "generate", "arxml", "--project", str(manifest), "--all-components"]
        monkeypatch.setattr("sys.argv", [*argv, "--output-dir", str(out_dir)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        terminal = captured.out + captured.err
        assert "swcraft-component-export" not in terminal, terminal
        assert f"ERROR {self._REFUSAL}" in captured.out, terminal


class TestEveryExceptionThatRendersItselfTakesTheFullPathKeyword:
    """Every ``swcraft_tools`` exception with a ``render`` takes the ``full_path`` keyword.

    ``cli.messages.error_text`` calls ``render(full_path=True)`` on any exception with the
    attribute, so one without the keyword would turn a refusal into a ``TypeError``.
    Does not reach a class outside ``swcraft_tools`` or a module not importable on this host.
    """

    def test_every_rendering_exception_class_accepts_full_path(self) -> None:
        import importlib
        import pkgutil

        import swcraft_tools

        rendering: list[str] = []
        refused: list[str] = []
        for module_info in pkgutil.walk_packages(swcraft_tools.__path__, "swcraft_tools."):
            module = importlib.import_module(module_info.name)
            for name, value in vars(module).items():
                if not (isinstance(value, type) and issubclass(value, BaseException)):
                    continue
                if value.__module__ != module.__name__ or not callable(getattr(value, "render", None)):
                    continue
                rendering.append(f"{module.__name__}.{name}")
                if "full_path" not in inspect.signature(value.render).parameters:
                    refused.append(f"{module.__name__}.{name}")

        assert refused == []
        assert len(rendering) >= 8, rendering


class TestEveryCommandStartsFromAnEmptyParseCache:
    """``main`` clears the YAML parse cache once the arguments are parsed, before the command runs.

    The clear keeps one command's working set to the files it reads. A text read before
    ``main`` is parsed again (the clear happened), and the manifest is still held (it came
    first). Does not reach ``--help`` and a usage error, which exit inside ``parse_args()``.
    """

    def test_main_clears_the_parse_cache_before_the_command_runs(self, tmp_path: Path, monkeypatch) -> None:
        from swcraft_tools.yaml_load import load_yaml_document, parse_cache_info

        manifest = _write_manifest(tmp_path)
        _write_swc(tmp_path / "model", "SwcOne")
        held_over = "name: HeldOverFromAnEarlierCommand\n"
        load_yaml_document(held_over)
        monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(manifest)])

        main()  # must not raise SystemExit

        misses = parse_cache_info().misses
        load_yaml_document(held_over)
        assert parse_cache_info().misses == misses + 1
        hits = parse_cache_info().hits
        load_yaml_document(manifest.read_text(encoding="utf-8"))
        assert parse_cache_info().hits == hits + 1


@pytest.mark.usefixtures("_no_local_schema")
class TestThePreviousArxmlEdgeCases:
    """The previous ARXML's edge cases: each is the command's ``Error:`` line or the stated behaviour.

    An ``_old`` file already present, a ``validate`` that refuses its ARXML, a ``dist`` the
    state read cannot search, and a move ``clean-outputs`` cannot make; never a traceback."""

    def test_a_hand_written_old_file_is_replaced_and_the_move_announced(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _version_project(tmp_path, None)
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "model.arxml").write_bytes(b"<previous build/>\n")
        (tmp_path / "dist" / "model_old.arxml").write_bytes(b"<older build/>\n")

        TestCliKeepsOnePreviousArxml._run(monkeypatch, manifest, "generate", "arxml")

        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<previous build/>\n"
        assert capsys.readouterr().out.count("Previous ARXML kept:") == 1

    def test_a_validate_that_refuses_its_arxml_leaves_it_beside_the_previous_one(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``_old`` is the previous **written** ARXML, never certified good: the refused file stays."""
        manifest = _version_project(tmp_path, None)
        TestCliKeepsOnePreviousArxml._run(monkeypatch, manifest, "generate", "arxml")
        first_build = (tmp_path / "dist" / "model.arxml").read_bytes()
        _write_swc(tmp_path / "model", "BrakeSWC")
        refused = ValidationOutcome(valid=False, mode="structural", messages=("refused",))
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            patch("swcraft_tools.cli.build_commands.check_arxml", return_value=refused),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == first_build
        assert (tmp_path / "dist" / "model.arxml").read_bytes() == TestCliKeepsOnePreviousArxml._fresh_build(tmp_path)
        assert "failed structural validation" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [("generate", "arxml"), ("validate",)])
    def test_a_dist_root_that_is_a_file_is_an_error_line_not_a_traceback(
        self, tmp_path: Path, monkeypatch, capsys, command: tuple[str, ...]
    ) -> None:
        manifest = _version_project(tmp_path, None)
        (tmp_path / "dist").write_text("not a directory\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", *command, "--project", str(manifest)])
        success = ConsistencyResult(consistent=True, issues=[])

        with (
            patch("swcraft_tools.cli.build_commands.check_cross_layer_consistency", return_value=success),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert capsys.readouterr().err.startswith("Error: ")
        assert (tmp_path / "dist").read_text(encoding="utf-8") == "not a directory\n"

    def test_a_refused_move_in_clean_outputs_is_an_error_line_and_removes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A directory holding the ``_old`` name: the move raises, and nothing is removed."""
        manifest = _project_with_outputs(tmp_path)
        (tmp_path / "dist" / "model_old.arxml").mkdir()
        (tmp_path / "dist" / "model_old.arxml" / "inside.txt").write_text("x\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "clean-outputs", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("Error: cannot keep the previous ARXML: ")
        assert "Removed" not in captured.out
        assert (tmp_path / "dist" / "model.arxml").read_text(encoding="utf-8") == "<AUTOSAR/>"
        assert (tmp_path / "views" / "model.c4").is_file()

    def test_only_a_file_of_a_kept_name_is_kept(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The keep set names files: a directory named ``model_old.arxml`` or ``audit.jsonl`` is removed."""
        manifest = _write_manifest(tmp_path)
        for name in ("model_old.arxml", "audit.jsonl"):
            (tmp_path / "dist" / name).mkdir(parents=True)

        _run_clean_outputs(manifest, monkeypatch, capsys)

        assert not (tmp_path / "dist").exists()


# swcraft experiment allocation -- the allocation experiment's command and its exit contract


def _experiment_run(argv: list[str], monkeypatch, capsys) -> tuple[int, str, str]:
    """Run ``swcraft experiment ...`` in process: ``(exit code, stdout, stderr)``, 0 when it returned."""
    monkeypatch.setattr("sys.argv", ["swcraft", "experiment", *argv])
    try:
        main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _experiment_project(tmp_path: Path, model=None, **experiment: Any) -> tuple[Path, Path]:
    from carma_models import three_cores, write_project

    return write_project(three_cores() if model is None else model, tmp_path, **experiment)


def _experiment_argv(manifest: Path, file: Path | None, *extra: str) -> list[str]:
    """``allocation --project MANIFEST [--config FILE]``: *file* ``None`` runs the defaults."""
    config = [] if file is None else ["--config", str(file)]
    return ["allocation", "--project", str(manifest), *config, *extra]


def _experiments_dir(tmp_path: Path) -> Path:
    return tmp_path / "dist" / "experiments"


class TestExperimentCommand:
    """The exit contract: 0 on a run, 1 on every refusal or failure, writing nothing then.

    The pin mismatch is ``_WRITING_INVOCATIONS``' ``experiment`` entry.
    """

    def test_a_run_prints_the_summary_and_writes_the_two_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from swcraft_tools.allocation import REPORT_SECTIONS, RESULT_SCHEMA_VERSION

        manifest, file = _experiment_project(tmp_path)
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        directory = _experiments_dir(tmp_path) / "relieve"
        report = (directory / "report.md").read_text(encoding="utf-8")
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        summary = out.splitlines()[0]
        assert summary == (
            "Experiment 'relieve': inter-partition volume 1200 bytes x Hz now; N = 1: 800 (optimal); "
            "N = 2: 400 (optimal); N = 3: 0 (optimal); 3 candidates at N = 3."
        )
        assert f"Generated report: {directory / 'report.md'}" in out
        assert f"Generated result: {directory / 'result.json'}" in out
        assert document["schemaVersion"] == RESULT_SCHEMA_VERSION == 1
        assert document["summary"] == summary
        # The report opens with the summary line.
        assert report.splitlines()[2] == summary
        headings = [line[3:] for line in report.splitlines() if line.startswith("## ")]
        assert headings == list(REPORT_SECTIONS)
        assert not (tmp_path / "views").exists()

    def test_the_two_files_are_one_content(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``report.md`` with its links reduced to names is ``result.json`` rendered; every link target exists."""
        from swcraft_tools.allocation import render_markdown

        manifest, file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        directory = _experiments_dir(tmp_path) / "relieve"
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        report = (directory / "report.md").read_text(encoding="utf-8")
        link = re.compile(r"\[([^\]\[]+)\]\(([^)\s]+)\)")
        assert link.sub(lambda match: match.group(1), report) == render_markdown(document)
        targets = {target for _name, target in link.findall(report)}
        assert targets, "the report links no name, so the second half proves nothing"
        model_root = (tmp_path / "model").resolve()
        for target in targets:
            resolved = (directory / urllib.parse.unquote(target)).resolve()
            assert resolved.is_file() and resolved.is_relative_to(model_root), target

    def test_every_figure_is_an_integer_or_a_rational_never_a_float(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path, tolerance=0.25)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        document = json.loads((_experiments_dir(tmp_path) / "relieve" / "result.json").read_text(encoding="utf-8"))

        def floats(value: Any) -> list[float]:
            if isinstance(value, float):
                return [value]
            if isinstance(value, dict):
                return [each for item in value.values() for each in floats(item)]
            if isinstance(value, list):
                return [each for item in value for each in floats(item)]
            return []

        assert floats(document) == []
        assert document["fingerprint"]["parameters"]["search"]["tolerance"] == {"n": 1, "d": 4}
        assert document["currentState"]["loads"][0]["load"] == {"n": 3, "d": 20}

    def test_json_prints_the_result_document(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        code, out, _err = _experiment_run(_experiment_argv(manifest, file, "--format", "json"), monkeypatch, capsys)

        assert code == 0
        written = (_experiments_dir(tmp_path) / "relieve" / "result.json").read_text(encoding="utf-8")
        assert out == written
        assert json.loads(out)["schemaVersion"] == 1

    def test_no_improvement_at_any_n_is_a_successful_run(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from carma_models import THREE_CORES_LAYOUT

        every = [name for names in THREE_CORES_LAYOUT.values() for name in names]
        manifest, file = _experiment_project(tmp_path, locked=every)
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        assert "no improvement at any N" in out.splitlines()[0]
        assert (_experiments_dir(tmp_path) / "relieve" / "report.md").is_file()

    def test_no_free_component_is_a_successful_run(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A runnable without a cost makes its component not free: with none declared, nothing can move."""
        from carma_models import three_cores

        model = three_cores()
        for relative, document in model.files.items():
            if relative.startswith("swc/"):
                for each in document.get("runnables", []):
                    each.pop("wcet", None)
                    each.pop("wcet_type", None)
        manifest, file = _experiment_project(tmp_path, model)
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        assert out.splitlines()[0].endswith("no free component, so nothing can move.")

    def test_a_model_check_model_refuses_is_refused_with_the_count(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from carma_models import three_cores

        model = three_cores()
        # Two runnables of 100us each in a task budgeted 150us: rule:task.runnable-cost-exceeds-task.
        next(doc for doc in model.files.values() if doc.get("name") == "T_P1_10ms")["wcet"] = "150us"
        manifest, file = _experiment_project(tmp_path, model)
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert err.startswith("Error: the model is refused: check-model reports 1 error ")
        assert not _experiments_dir(tmp_path).exists()

    def test_a_model_whose_only_finding_is_a_warning_runs_and_lists_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A warnings-only model runs: exit 0, the two files, no `WARNING:` line.

        `--warnings-as-errors` is argparse's usage error on this command.
        """
        from carma_models import three_cores

        model = three_cores()
        model.unsatisfied_requirement("REQ_CARMA_UNSATISFIED")  # a warning, and no other finding
        manifest, file = _experiment_project(tmp_path, model)
        verdict = check_cross_layer_consistency(str(tmp_path / "model"))
        assert verdict.consistent is True, verdict.issues
        assert [record.rule for record in verdict.warnings] == ["trace.requirement-unallocated"]
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        assert "WARNING:" not in err and "REQ_CARMA_UNSATISFIED" not in err
        assert out.splitlines()[0].startswith("Experiment 'relieve': ")
        assert (_experiments_dir(tmp_path) / "relieve" / "result.json").is_file()

        code, _out, err = _experiment_run(_experiment_argv(manifest, file, "--warnings-as-errors"), monkeypatch, capsys)

        assert code == 2
        assert "unrecognized arguments: --warnings-as-errors" in err

    def test_a_project_it_cannot_read_is_refused(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        (tmp_path / "model" / "interfaces" / "ISig.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert err.startswith("Error: ") and "ISig.yaml" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_a_refused_file_names_the_key(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path, max_move=2)
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert "'max_move'" in err and str(tmp_path) not in err
        assert not _experiments_dir(tmp_path).exists()

    def test_an_undeclared_name_is_refused_by_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path, locked=["NoSuchComponent"])
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert "'NoSuchComponent'" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_an_absent_experiment_file_is_refused(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = _experiment_project(tmp_path)
        code, _out, err = _experiment_run(
            _experiment_argv(manifest, tmp_path / "experiments" / "absent.yaml"), monkeypatch, capsys
        )

        assert code == 1
        assert err.startswith("Error: the configuration file cannot be read: ") and "absent.yaml" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_the_solver_absent_is_refused_by_name(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        monkeypatch.setitem(sys.modules, "scipy", None)
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert "'scipy'" in err and "scipy>=1.17.1" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_a_solver_that_will_not_load_is_refused_in_one_sentence(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from carma_models import unloadable_solver

        manifest, file = _experiment_project(tmp_path)
        unloadable_solver(monkeypatch, OSError("[WinError 126] The specified module could not be found: 'C:/x/y.pyd'"))
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert err.startswith("Error: the allocation search's solver 'scipy' is installed but its native libraries")
        assert err.count("\n") == 1 and "y.pyd" not in err and "C:" not in err
        assert not _experiments_dir(tmp_path).exists()

    def test_an_experiment_the_solvers_integers_cannot_represent_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from carma_models import awkward_periods
        from swcraft_tools.allocation import NOT_REPRESENTABLE

        manifest, file = _experiment_project(tmp_path, awkward_periods(), locked=["RA", "RB"])
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert NOT_REPRESENTABLE in err
        assert not _experiments_dir(tmp_path).exists()

    def test_a_solver_error_status_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from types import SimpleNamespace

        from swcraft_tools import allocation

        failed = SimpleNamespace(status=4, message="(HiGHS Status 13: Solve error)", x=None)
        monkeypatch.setattr(allocation.SearchModel, "solve", lambda _search, _limit: failed)
        manifest, file = _experiment_project(tmp_path)
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert "status 4" in err and "execution failure" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_a_missing_project_flag_is_refused(self, monkeypatch, capsys) -> None:
        code, _out, err = _experiment_run(["allocation"], monkeypatch, capsys)
        assert code == 1
        assert "--project is required" in err

    @pytest.mark.parametrize(
        "argv", [[], ["run"], ["walk"]], ids=["missing KIND", "the retired run as KIND", "unknown KIND"]
    )
    def test_a_usage_error_exits_2_before_the_command_runs(self, monkeypatch, capsys, argv: list[str]) -> None:
        code, _out, _err = _experiment_run(argv, monkeypatch, capsys)
        assert code == 2

    def test_clean_outputs_removes_the_reports_and_leaves_the_experiment_files(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest, file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        assert _experiments_dir(tmp_path).is_dir()

        _run_clean_outputs(manifest, monkeypatch, capsys)

        assert not _experiments_dir(tmp_path).exists()
        assert file.is_file()

    def test_a_run_changes_no_model_file_and_leaves_no_scratch_copy(self, tmp_path: Path, monkeypatch, capsys) -> None:
        import tempfile as tempfile_module

        from swcraft_tools import allocation

        manifest, file = _experiment_project(tmp_path)
        model = tmp_path / "model"
        before = {path.relative_to(model).as_posix(): path.read_bytes() for path in model.rglob("*") if path.is_file()}
        made: list[Path] = []
        real_mkdtemp = tempfile_module.mkdtemp

        def recording(*args: Any, **kwargs: Any) -> str:
            directory = real_mkdtemp(*args, **kwargs)
            made.append(Path(directory))
            return directory

        monkeypatch.setattr(allocation.tempfile, "mkdtemp", recording)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0

        after = {path.relative_to(model).as_posix(): path.read_bytes() for path in model.rglob("*") if path.is_file()}
        assert after == before
        assert made, "the run checked no move set on a copy, so this test proves nothing"
        assert [directory for directory in made if directory.exists()] == []

    def test_a_second_run_replaces_both_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        file.write_text("name: relieve\nsearch:\n  max_moves: 1\n", encoding="utf-8", newline="\n")
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        directory = _experiments_dir(tmp_path) / "relieve"
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert document["fingerprint"]["parameters"]["search"]["max_moves"] == 1
        assert [point["n"] for point in document["curve"]] == [1]
        assert (directory / "report.md").read_text(encoding="utf-8").splitlines()[2] == out.splitlines()[0]
        assert sorted(path.name for path in directory.iterdir()) == ["report.md", "result.json"]

    def test_a_failed_write_replaces_neither_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A write that fails part-way leaves the previous run's pair as it was, and no temporary file."""
        manifest, file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        directory = _experiments_dir(tmp_path) / "relieve"
        previous = {path.name: path.read_bytes() for path in directory.iterdir()}
        real_write_text = Path.write_text

        def full_disk(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name.startswith("report.md.") and self.name.endswith(".partial"):
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", full_disk)
        file.write_text("name: relieve\nsearch:\n  max_moves: 1\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1 and "No space left on device" in err
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == previous

    def test_a_stale_partial_file_is_neither_used_nor_fatal(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A ``*.partial`` a killed run left behind is not overwritten, not renamed into place and not an error."""
        manifest, file = _experiment_project(tmp_path)
        directory = _experiments_dir(tmp_path) / "relieve"
        directory.mkdir(parents=True)
        stale = {name: f"stale {name}\n" for name in ("result.json.partial", "report.md.partial")}
        for name, text in stale.items():
            (directory / name).write_text(text, encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        assert json.loads((directory / "result.json").read_text(encoding="utf-8"))["fingerprint"]
        assert {name: (directory / name).read_text(encoding="utf-8") for name in stale} == stale
        assert sorted(path.name for path in directory.iterdir()) == sorted(["report.md", "result.json", *stale])

    def test_two_runs_write_under_different_temporary_names(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Each run's temporary names are its own, so concurrent runs never write each other's files."""
        manifest, file = _experiment_project(tmp_path)
        written: list[str] = []
        real_write_text = Path.write_text

        def recording(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name.endswith(".partial"):
                written.append(self.name)
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", recording)
        for _ in range(2):
            assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0

        assert len(written) == 4 and len(set(written)) == 4
        assert sorted(name.split(".partial")[0].rsplit(".", 1)[0] for name in written) == [
            "report.md",
            "report.md",
            "result.json",
            "result.json",
        ]
        directory = _experiments_dir(tmp_path) / "relieve"
        assert sorted(path.name for path in directory.iterdir()) == ["report.md", "result.json"]

    def test_the_pair_gets_the_mode_a_plain_write_gets(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The umask governs both files, never ``mkstemp``'s owner-only ``0o600``.

        Compared with a sibling ``write_text``, so it holds under any umask and on Windows.
        """
        import stat

        manifest, file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        directory = _experiments_dir(tmp_path) / "relieve"
        sibling = directory / "sibling.txt"
        sibling.write_text("x\n", encoding="utf-8", newline="\n")

        expected = stat.S_IMODE(sibling.stat().st_mode)
        assert {name: stat.S_IMODE((directory / name).stat().st_mode) for name in ("result.json", "report.md")} == {
            "result.json": expected,
            "report.md": expected,
        }

    def test_a_temporary_name_already_taken_is_retried(self, tmp_path: Path, monkeypatch) -> None:
        """A collision with an existing ``*.partial`` draws a fresh name and leaves the existing file alone."""
        from swcraft_tools import allocation

        draws = iter([b"\x00" * 8, b"\x00" * 8, b"\x01" * 8])
        monkeypatch.setattr(allocation.os, "urandom", lambda size: next(draws))
        taken = tmp_path / f"report.md.{'00' * 8}.partial"
        taken.write_text("held\n", encoding="utf-8", newline="\n")

        allocation._write_pair(tmp_path, ((tmp_path / "result.json", "{}\n"), (tmp_path / "report.md", "# r\n")))

        assert taken.read_text(encoding="utf-8") == "held\n"
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(["report.md", "result.json", taken.name])

    def test_a_file_that_is_not_utf8_is_refused_naming_the_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        file.write_bytes(b"name: relieve\nsearch: \xff\xfe\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert err == f"Error: the configuration file cannot be read: it is not UTF-8 text ({file})\n"
        assert not _experiments_dir(tmp_path).exists()

    def test_the_command_is_neither_pin_advisory_nor_in_a_shared_exit_tuple(self) -> None:
        """It writes, so a pin mismatch refuses it, and it is in no shared exit sentence."""
        assert "experiment" not in project_args._PIN_ADVISORY_COMMANDS
        assert "experiment" not in guide_reference_cli.WRITES_NOTHING
        assert "experiment" not in guide_reference_cli.ALWAYS_EXITS_ZERO
        assert "experiment" not in guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST
        assert "experiment" in guide_reference_cli.REFUSES_A_MODEL_CHECK_MODEL_REFUSES

    # -- the command by kind, the configuration file and the record ------------

    def test_without_a_configuration_the_kind_runs_with_its_defaults(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """No ``--config``: the kind's default name and directory, and the record says "defaults"."""
        from swcraft_tools.allocation import AllocationConfig

        manifest, _file = _experiment_project(tmp_path)
        code, out, err = _experiment_run(_experiment_argv(manifest, None), monkeypatch, capsys)

        assert code == 0, err
        directory = _experiments_dir(tmp_path) / "allocation"
        assert f"Generated report: {directory / 'report.md'}" in out
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        defaults = AllocationConfig()
        assert (document["name"], document["kind"]) == ("allocation", "allocation")
        assert document["fingerprint"]["configuration"] is None
        assert document["fingerprint"]["parameters"] == {
            "name": "allocation",
            "output": "dist/experiments/allocation",
            "search": {
                "max_moves": defaults.search.max_moves,
                "candidates": defaults.search.candidates,
                "candidates_at": defaults.search.max_moves,
                "tolerance": 0,
                "time_limit_s": 10,
            },
            "constraints": {"locked": [], "allowed_partitions": {}},
        }
        report = (directory / "report.md").read_text(encoding="utf-8")
        assert "- Kind: allocation\n" in report
        assert "- Configuration: defaults, no configuration file\n" in report
        assert "- Output: `dist/experiments/allocation`\n" in report
        assert "- search.max_moves: 3\n" in report and "- constraints.locked: none\n" in report
        # `name` and `output` are the title's and the `Output:` line's, never parameter lines.
        assert "\n- name:" not in report and "\n- output:" not in report
        assert sorted(path.name for path in _experiments_dir(tmp_path).iterdir()) == ["allocation"]

    def test_a_configuration_is_recorded_by_its_relative_path_and_digest_in_both_files(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        import hashlib

        manifest, file = _experiment_project(tmp_path, max_moves=2, candidates=1, locked=["Q1"])
        code, out, err = _experiment_run(_experiment_argv(manifest, file, "--format", "json"), monkeypatch, capsys)

        assert code == 0, err
        digest = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
        directory = _experiments_dir(tmp_path) / "relieve"
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert json.loads(out) == document
        assert document["fingerprint"]["configuration"] == {
            "file": "experiments/relieve.yaml",
            "insideProject": True,
            "sha256": digest,
        }
        parameters = document["fingerprint"]["parameters"]
        assert (parameters["name"], parameters["output"]) == ("relieve", "dist/experiments/relieve")
        assert (parameters["search"]["max_moves"], parameters["search"]["candidates"]) == (2, 1)
        assert parameters["constraints"]["locked"] == ["Q1"]
        report = (directory / "report.md").read_text(encoding="utf-8")
        assert f"- Configuration: `experiments/relieve.yaml`, {digest}\n" in report
        assert "- search.max_moves: 2\n" in report and "- constraints.locked: Q1\n" in report

    def test_a_name_alone_names_the_report_directory(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        file.write_text("name: alone\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        assert sorted(path.name for path in _experiments_dir(tmp_path).iterdir()) == ["alone"]
        document = json.loads((_experiments_dir(tmp_path) / "alone" / "result.json").read_text(encoding="utf-8"))
        assert document["fingerprint"]["parameters"]["search"]["max_moves"] == 3

    def test_an_output_alone_is_honoured_inside_the_project(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = _experiment_project(tmp_path)
        file.write_text("output: reports/carma\n", encoding="utf-8", newline="\n")
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        directory = tmp_path / "reports" / "carma"
        assert f"Generated report: {directory.resolve() / 'report.md'}" in out
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert document["name"] == "allocation"
        assert document["fingerprint"]["parameters"]["output"] == "reports/carma"
        assert "- Output: `reports/carma`\n" in (directory / "report.md").read_text(encoding="utf-8")
        assert not _experiments_dir(tmp_path).exists()

    def test_the_record_names_the_resolved_output_and_not_its_spelling(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``parameters.output`` is the resolved directory made project-relative, never the written string."""
        manifest, file = _experiment_project(tmp_path)
        file.write_text("output: ./reports/../reports/carma/\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        directory = tmp_path / "reports" / "carma"
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert document["fingerprint"]["parameters"]["output"] == "reports/carma"
        assert "- Output: `reports/carma`\n" in (directory / "report.md").read_text(encoding="utf-8")

    def test_a_hand_written_report_in_the_output_is_refused_and_kept(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A ``report.md`` no run wrote is kept: exit 1, bytes unchanged."""
        manifest, file = _experiment_project(tmp_path)
        (tmp_path / "docs").mkdir()
        mine = b"my own notes\r\nkept byte for byte\n"
        (tmp_path / "docs" / "report.md").write_bytes(mine)
        file.write_text("output: docs\n", encoding="utf-8", newline="\n")
        before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
        code, out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert out == ""
        assert err == (
            "Error: the configuration's 'output' ('docs') already holds 'report.md', which no experiment run "
            "wrote: the run would replace it. Choose another directory, or move that file away.\n"
        )
        assert (tmp_path / "docs" / "report.md").read_bytes() == mine
        assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before

    def test_an_earlier_runs_report_in_the_output_is_replaced(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A run's own ``result.json`` and ``report.md``, written by a real run, are replaced by the next run."""
        manifest, file = _experiment_project(tmp_path)
        directory = tmp_path / "reports" / "carma"
        file.write_text("output: reports/carma\nsearch:\n  max_moves: 2\n", encoding="utf-8", newline="\n")
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0
        first = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert first["fingerprint"]["parameters"]["search"]["max_moves"] == 2

        file.write_text("output: reports/carma\nsearch:\n  max_moves: 1\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 0, err
        second = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert second["fingerprint"]["parameters"]["search"]["max_moves"] == 1
        assert "- search.max_moves: 1\n" in (directory / "report.md").read_text(encoding="utf-8")

    def test_the_default_directory_is_run_into_again(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """No ``output``: the default under ``dist_root`` is not checked for foreign files, and reruns."""
        manifest, _file = _experiment_project(tmp_path)
        assert _experiment_run(_experiment_argv(manifest, None), monkeypatch, capsys)[0] == 0
        report = _experiments_dir(tmp_path) / "allocation" / "report.md"
        report.write_text("edited by hand\n", encoding="utf-8", newline="\n")
        (report.parent / "result.json").write_text("{}\n", encoding="utf-8", newline="\n")

        code, _out, err = _experiment_run(_experiment_argv(manifest, None), monkeypatch, capsys)

        assert code == 0, err
        assert report.read_text(encoding="utf-8").startswith("# Allocation experiment ")
        assert json.loads((report.parent / "result.json").read_text(encoding="utf-8"))["kind"] == "allocation"

    def test_clean_outputs_leaves_a_report_whose_output_lies_outside_the_two_roots(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest, file = _experiment_project(tmp_path)
        file.write_text("output: reports/carma\n", encoding="utf-8", newline="\n")
        assert _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)[0] == 0

        _run_clean_outputs(manifest, monkeypatch, capsys)

        assert (tmp_path / "reports" / "carma" / "report.md").is_file()
        assert (tmp_path / "reports" / "carma" / "result.json").is_file()

    def test_a_configuration_outside_the_project_is_recorded_by_its_file_name(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Accepted, named by its file name alone with ``insideProject: false``, never by a location."""
        import hashlib

        from carma_models import three_cores, write_project

        project_dir = tmp_path / "project"
        manifest, _file = write_project(three_cores(), project_dir)
        outside = tmp_path / "elsewhere" / "outside.yaml"
        outside.parent.mkdir()
        outside.write_text("name: outside\nsearch:\n  max_moves: 1\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, outside), monkeypatch, capsys)

        assert code == 0, err
        directory = project_dir / "dist" / "experiments" / "outside"
        texts = {name: (directory / name).read_text(encoding="utf-8") for name in ("report.md", "result.json")}
        configuration = json.loads(texts["result.json"])["fingerprint"]["configuration"]
        digest = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()
        assert configuration == {"file": "outside.yaml", "insideProject": False, "sha256": digest}
        assert f"- Configuration: `outside.yaml` (outside the project directory), {digest}\n" in texts["report.md"]
        for name, text in texts.items():
            assert str(tmp_path) not in text and tmp_path.as_posix() not in text and "elsewhere" not in text, name

    def test_a_relative_configuration_resolves_against_the_current_directory(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``--config`` is a path as given, like ``--project``; the record is still project-relative."""
        manifest, file = _experiment_project(tmp_path)
        monkeypatch.chdir(file.parent)
        code, _out, err = _experiment_run(_experiment_argv(manifest, Path(file.name)), monkeypatch, capsys)

        assert code == 0, err
        document = json.loads((_experiments_dir(tmp_path) / "relieve" / "result.json").read_text(encoding="utf-8"))
        assert document["fingerprint"]["configuration"]["file"] == "experiments/relieve.yaml"
        assert document["fingerprint"]["configuration"]["insideProject"] is True

    def test_the_kind_choices_are_the_configuration_class_kinds(self) -> None:
        """The kind choices equal the configuration classes' kinds (the literal stays in its module)."""
        from swcraft_tools.allocation import AllocationConfig
        from swcraft_tools.trace_search import TraceConfig

        subparsers = next(action for action in build_parser()._actions if action.dest == "command")
        experiment = subparsers.choices["experiment"]
        kind = next(action for action in experiment._actions if action.dest == "kind")
        assert kind.choices == [AllocationConfig.kind, TraceConfig.kind]
        assert kind.metavar == "KIND"

    @pytest.mark.parametrize(
        ("written", "refusal"),
        [
            ("/abs/report", "is written as the absolute path '/abs/report'"),
            ("../escaped-experiment-report", "('../escaped-experiment-report') resolves outside the project directory"),
            ("model/reports", "('model/reports') overlaps 'model_root'"),
            ("notes.txt", "('notes.txt') names a file, not a directory"),
            (".git", "('.git') names or lies inside '.git', which belongs to git"),
            (".git/hooks", "('.git/hooks') names or lies inside '.git', which belongs to git"),
        ],
        ids=["absolute", "escaping", "over the model", "a file", "git", "inside git"],
    )
    def test_a_refused_output_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys, written: str, refusal: str
    ) -> None:
        project_dir = tmp_path / "project"
        manifest, file = _experiment_project(project_dir)
        (project_dir / "notes.txt").write_text("mine\n", encoding="utf-8", newline="\n")
        file.write_text(f"output: '{written}'\n", encoding="utf-8", newline="\n")
        before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)

        assert code == 1
        assert err.startswith("Error: the configuration's 'output' ") and refusal in err
        assert err.count("\n") == 1
        assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before
        assert (project_dir / "notes.txt").read_text(encoding="utf-8") == "mine\n"
        assert not _experiments_dir(project_dir).exists()

    def test_a_refused_output_costs_no_solve_and_a_refused_name_is_reported_first(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Names, then the report directory, then the solve: a refusal before it never solves."""
        from swcraft_tools import allocation

        def never(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the solve was reached")

        monkeypatch.setattr(allocation, "solve_curve", never)
        project_dir = tmp_path / "project"
        manifest, file = _experiment_project(project_dir)
        file.write_text("output: ../x\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)
        assert code == 1
        assert err == "Error: the configuration's 'output' ('../x') resolves outside the project directory\n"

        file.write_text("output: ../x\nconstraints:\n  locked: [X9]\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, file), monkeypatch, capsys)
        assert code == 1
        assert err == "Error: 'constraints.locked' names component 'X9', which the model does not declare\n"
        assert not (tmp_path / "x").exists() and not _experiments_dir(project_dir).exists()

    def test_a_default_directory_below_a_file_is_refused_before_the_solve(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from swcraft_tools import allocation

        def never(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the solve was reached")

        monkeypatch.setattr(allocation, "solve_curve", never)
        manifest, _file = _experiment_project(tmp_path)
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "experiments").write_text("mine\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(_experiment_argv(manifest, None), monkeypatch, capsys)

        assert code == 1
        assert err == (
            "Error: the report directory '<dist_root>/experiments/allocation' lies below 'dist/experiments', "
            "which is a file, not a directory\n"
        )
        assert (tmp_path / "dist" / "experiments").read_text(encoding="utf-8") == "mine\n"

    def test_a_configuration_that_cannot_be_read_is_named_as_given(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A directory given as ``--config``: an ``OSError`` on every platform, named by the value as given."""
        manifest, file = _experiment_project(tmp_path)
        code, _out, err = _experiment_run(_experiment_argv(manifest, file.parent), monkeypatch, capsys)

        assert code == 1
        assert err.startswith("Error: the configuration file cannot be read: ")
        assert err.endswith(f" ({file.parent})\n")
        assert not _experiments_dir(tmp_path).exists()

    # -- the trace kind ----------------------------------------------------------

    @staticmethod
    def _trace_project(tmp_path: Path, **experiment: Any) -> tuple[Path, Path | None]:
        from test_trace_search_run import _model
        from trace_models import write_project

        return write_project(_model(), tmp_path, **experiment)

    @staticmethod
    def _trace_argv(manifest: Path, file: Path | None, *extra: str) -> list[str]:
        config = [] if file is None else ["--config", str(file)]
        return ["trace", "--project", str(manifest), *config, *extra]

    def test_trace_prints_the_summary_and_the_three_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, file = self._trace_project(tmp_path, candidates=3)
        code, out, err = _experiment_run(self._trace_argv(manifest, file), monkeypatch, capsys)
        assert code == 0, err
        directory = _experiments_dir(tmp_path) / "trace"
        lines = out.splitlines()
        assert (
            lines[0].startswith("trace: ")
            and lines[0] == json.loads((directory / "result.json").read_text(encoding="utf-8"))["summary"]
        )
        assert lines[1:] == [
            f"Generated report: {directory / 'report.md'}",
            f"Generated result: {directory / 'result.json'}",
            f"Generated proposals: {directory / 'proposals.json'}",
        ]
        assert sorted(path.name for path in directory.iterdir()) == ["proposals.json", "report.md", "result.json"]

    def test_trace_json_is_the_result_document(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = self._trace_project(tmp_path)
        code, out, err = _experiment_run(self._trace_argv(manifest, None, "--format", "json"), monkeypatch, capsys)
        assert code == 0, err
        written = (_experiments_dir(tmp_path) / "trace" / "result.json").read_text(encoding="utf-8")
        assert out == written

    @pytest.mark.parametrize(
        "keys",
        [{"requirements": ["SR-4"], "without_document": False}, {"target_kinds": ["memory"]}],
        ids=["no requirement in scope", "nothing found for all"],
    )
    def test_trace_exits_0_on_an_empty_answer(self, tmp_path: Path, monkeypatch, capsys, keys: dict) -> None:
        manifest, file = self._trace_project(tmp_path, **keys)
        code, _out, err = _experiment_run(self._trace_argv(manifest, file), monkeypatch, capsys)
        assert code == 0, err

    @pytest.mark.parametrize(
        ("keys", "sentence"),
        [
            (
                {"strictness": 1},
                "Error: the configuration file is refused: 'strictness': Extra inputs are not permitted",
            ),
            ({"documents": ["Nope"]}, "Error: 'constraints.documents' names document 'Nope'"),
            ({"output": "/elsewhere"}, "Error: the configuration's 'output'"),
        ],
        ids=["a key", "a name", "an output"],
    )
    def test_trace_refuses_a_configuration_writing_nothing(
        self, tmp_path: Path, monkeypatch, capsys, keys: dict, sentence: str
    ) -> None:
        manifest, file = self._trace_project(tmp_path, **keys)
        code, _out, err = _experiment_run(self._trace_argv(manifest, file), monkeypatch, capsys)
        assert code == 1 and err.startswith(sentence), err
        assert not _experiments_dir(tmp_path).exists()

    def test_trace_refuses_an_ambiguous_identity_writing_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from test_trace_search_run import _model
        from trace_models import write_project

        model = _model()
        model.component("DoorUnit", group="other")
        manifest, _file = write_project(model, tmp_path)
        code, _out, err = _experiment_run(self._trace_argv(manifest, None), monkeypatch, capsys)
        assert code == 1
        assert err.startswith("Error: the search is refused: the model has ") and "name.duplicate-swc" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_trace_names_an_unreadable_configuration_as_given(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = self._trace_project(tmp_path)
        missing = tmp_path / "experiments" / "absent.yaml"
        code, _out, err = _experiment_run(self._trace_argv(manifest, missing), monkeypatch, capsys)
        assert code == 1
        assert err.startswith("Error: the configuration file cannot be read: ") and err.endswith(f" ({missing})\n")
        assert not _experiments_dir(tmp_path).exists()

    def test_trace_names_an_unreadable_project(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = self._trace_project(tmp_path)
        broken = tmp_path / "model" / "swc" / "app" / "Broken.yaml"
        broken.write_text("name: Broken\nports: not a list\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(self._trace_argv(manifest, None), monkeypatch, capsys)
        assert code == 1 and err.startswith("Error: ") and "Broken" in err
        assert not _experiments_dir(tmp_path).exists()

    def test_trace_refuses_on_a_pin_mismatch(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")
        code, _out, err = _experiment_run(["trace", "--project", str(manifest)], monkeypatch, capsys)
        assert code == 1 and "9.9.9" in err and __version__ in err
        assert not _experiments_dir(tmp_path).exists()

    def test_a_kind_neither_kind_names_is_a_usage_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = self._trace_project(tmp_path)
        code, _out, err = _experiment_run(["walk", "--project", str(manifest)], monkeypatch, capsys)
        assert code == 2 and "invalid choice: 'walk'" in err

    # `trace --apply FILE`, its flags, its refusals and `--discard-pending`

    @staticmethod
    def _apply_project(tmp_path: Path) -> Any:
        """A synthetic project whose proposals file accepts one port link, rejects one, and confirms one."""
        from trace_models import OUT, TOP, ApplyProject, apply_model, confirming

        project = ApplyProject(tmp_path, apply_model())
        project.write(confirming(project, {"SR-2": [(OUT, "accept"), (TOP, "reject")]}))
        return project

    @staticmethod
    def _apply_argv(project: Any, *extra: str) -> list[str]:
        return ["trace", "--apply", str(project.proposals), "--project", str(project.manifest), *extra]

    @staticmethod
    def _pin(manifest: Path, version: str = "9.9.9") -> None:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = version
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")

    @pytest.mark.parametrize(
        ("argv", "sentence"),
        [
            (["allocation", "--apply", "p.json"], "--apply applies only to 'trace'."),
            (
                ["trace", "--apply", "p.json", "--config", "c.yaml"],
                "--config applies only to a search, not to --apply.",
            ),
            (
                ["trace", "--apply", "p.json", "--discard-pending"],
                "--discard-pending applies only to a search, not to --apply.",
            ),
            (["trace", "--dry-run"], "--dry-run applies only to --apply."),
            (["trace", "--expect-plan", "sha256:0"], "--expect-plan applies only to --apply."),
            (["allocation", "--dry-run"], "--dry-run applies only to --apply."),
            (["allocation", "--discard-pending"], "--discard-pending applies only to 'trace'."),
        ],
        ids=[
            "apply-with-allocation",
            "config-with-apply",
            "discard-with-apply",
            "dry-run-without-apply",
            "expect-plan-without-apply",
            "dry-run-with-allocation",
            "discard-with-allocation",
        ],
    )
    def test_every_usage_refusal_by_its_sentence_before_the_pin(
        self, tmp_path: Path, monkeypatch, capsys, argv: list[str], sentence: str
    ) -> None:
        project = self._apply_project(tmp_path)
        self._pin(project.manifest)
        before = tree_of(project.dir)
        code, out, err = _experiment_run([*argv, "--project", str(project.manifest)], monkeypatch, capsys)
        assert (code, out, err) == (1, "", f"Error: {sentence}\n")
        assert tree_of(project.dir) == before

    # `trace --documents`

    @pytest.mark.parametrize(
        ("argv", "sentence"),
        [
            (["allocation", "--documents"], "--documents applies only to 'trace'."),
            (["trace", "--documents", "--apply", "p.json"], "--documents and --apply cannot be combined."),
            (
                ["trace", "--documents", "--discard-pending"],
                "--discard-pending applies only to a search, not to --documents.",
            ),
            # The `--apply` refusals come first: `--apply` with `allocation` before `--documents` with it.
            (["allocation", "--documents", "--apply", "p.json"], "--apply applies only to 'trace'."),
        ],
        ids=["documents-with-allocation", "documents-with-apply", "discard-with-documents", "d34-first"],
    )
    def test_every_documents_refusal_by_its_sentence_before_the_pin(
        self, tmp_path: Path, monkeypatch, capsys, argv: list[str], sentence: str
    ) -> None:
        project = self._apply_project(tmp_path)
        self._pin(project.manifest)
        before = tree_of(project.dir)
        code, out, err = _experiment_run([*argv, "--project", str(project.manifest)], monkeypatch, capsys)
        assert (code, out, err) == (1, "", f"Error: {sentence}\n")
        assert tree_of(project.dir) == before

    # `trace --labelling=<JSON>`

    @pytest.mark.parametrize(
        ("argv", "sentence"),
        [
            (
                ["trace", "--documents", "--labelling", '"all"'],
                "--labelling applies only to a search, not to --documents.",
            ),
            (["allocation", "--labelling", '"all"'], "--labelling applies only to 'trace'."),
            (
                ["trace", "--apply", "p.json", "--labelling", '"all"'],
                "--labelling applies only to a search, not to --apply.",
            ),
            # --labelling beside --documents is refused before --discard-pending beside it.
            (
                ["trace", "--documents", "--discard-pending", "--labelling", "[]"],
                "--labelling applies only to a search, not to --documents.",
            ),
        ],
        ids=["labelling-with-documents", "labelling-with-allocation", "labelling-with-apply", "r5-order"],
    )
    def test_every_labelling_refusal_by_its_sentence_before_the_pin(
        self, tmp_path: Path, monkeypatch, capsys, argv: list[str], sentence: str
    ) -> None:
        project = self._apply_project(tmp_path)
        self._pin(project.manifest)
        before = tree_of(project.dir)
        code, out, err = _experiment_run([*argv, "--project", str(project.manifest)], monkeypatch, capsys)
        assert (code, out, err) == (1, "", f"Error: {sentence}\n")
        assert tree_of(project.dir) == before

    def test_labelling_reaches_the_search(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _config = self._trace_project(tmp_path)
        argv = ["trace", "--project", str(manifest), "--labelling=[]", "--format", "json"]
        code, out, err = _experiment_run(argv, monkeypatch, capsys)
        assert code == 0, err
        assert json.loads(out)["fingerprint"]["commandLine"] == {"labelling": []}

    def test_a_pin_mismatch_refuses_documents(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        self._pin(project.manifest)
        before = tree_of(project.dir)
        code, out, err = _experiment_run(
            ["trace", "--documents", "--project", str(project.manifest)], monkeypatch, capsys
        )
        assert code == 1 and out == "" and "9.9.9" in err and __version__ in err
        assert tree_of(project.dir) == before

    def test_documents_given_a_value_is_a_usage_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        code, _out, err = _experiment_run(
            ["trace", "--documents=yes", "--project", str(project.manifest)], monkeypatch, capsys
        )
        assert code == 2 and "--documents" in err

    @pytest.mark.parametrize("form", ["text", "json"])
    def test_documents_prints_the_answer_and_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys, form: str
    ) -> None:
        from swcraft_tools.trace_search import documents_text, run_trace_documents

        project = self._apply_project(tmp_path)
        before = tree_of(project.dir)
        argv = ["trace", "--documents", "--project", str(project.manifest), "--format", form]
        code, out, err = _experiment_run(argv, monkeypatch, capsys)
        assert code == 0, err
        answer = run_trace_documents(project.project, None)
        if form == "json":
            assert json.loads(out) == answer
        else:
            assert out == documents_text(answer)
        assert answer["proposals"] == "dist/experiments/trace/proposals.json"
        assert tree_of(project.dir) == before

    @pytest.mark.parametrize("extra", [[], ["--dry-run"]], ids=["applied", "dry-run"])
    def test_a_pin_mismatch_refuses_apply(self, tmp_path: Path, monkeypatch, capsys, extra: list[str]) -> None:
        project = self._apply_project(tmp_path)
        self._pin(project.manifest)
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project, *extra), monkeypatch, capsys)
        assert code == 1 and out == ""
        assert err.startswith("Error: ") and "9.9.9" in err and __version__ in err
        assert tree_of(project.dir) == before

    def test_a_dry_run_prints_the_report_and_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from swcraft_tools.trace_apply import run_apply

        project = self._apply_project(tmp_path)
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project, "--dry-run"), monkeypatch, capsys)
        assert code == 0, err
        assert out == run_apply(project.project, project.proposals, dry_run=True).report
        assert out.splitlines()[1] == "Dry run: nothing was written."
        assert tree_of(project.dir) == before

    def test_an_applied_run_writes_and_prints_the_report_after(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from swcraft_tools.trace_apply import run_apply

        project = self._apply_project(tmp_path)
        expected = run_apply(project.project, project.proposals, dry_run=True)
        code, out, err = _experiment_run(
            self._apply_argv(project, "--expect-plan", expected.plan.digest), monkeypatch, capsys
        )
        assert code == 0, err
        assert out == expected.report.replace("Dry run: nothing was written.\n", "")
        assert (
            "Summary: 1 written, 1 confirmed, 1 rejected, 0 already done, 0 stale, 0 not written, 0 undecided; 1 files"
            in out
        )
        assert (project.dir / "trace-decisions.yaml").is_file()
        assert str(project.dir) not in out

    def test_nothing_to_do_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        assert _experiment_run(self._apply_argv(project), monkeypatch, capsys)[0] == 0
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert code == 0, err
        assert "No change." in out.splitlines()
        assert tree_of(project.dir) == before

    def test_every_decision_stale_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from trace_models import TOP

        project = self._apply_project(tmp_path)
        project.write(project.propose({"SR-2": [(TOP, "accept")]}))
        (project.root / "requirements" / "SR-2.yaml").unlink()
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert code == 0, err
        assert "0 written, 0 confirmed, 0 rejected, 0 already done, 1 stale" in out
        assert "SR-2 is no longer in the model; search again" in out

    def test_format_json_is_the_runs_document(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from swcraft_tools.trace_apply import run_apply

        project = self._apply_project(tmp_path)
        expected = run_apply(project.project, project.proposals, dry_run=True).document
        code, out, err = _experiment_run(
            self._apply_argv(project, "--dry-run", "--format", "json"), monkeypatch, capsys
        )
        assert code == 0, err
        assert json.loads(out) == expected
        assert expected["kind"] == "trace-apply" and expected["dryRun"] is True

    def test_a_malformed_proposals_file_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        project.proposals.write_text("{}\n", encoding="utf-8", newline="\n")
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert code == 1 and out == "" and err.startswith(f"Error: {project.proposals}")
        assert tree_of(project.dir) == before

    def test_a_malformed_decisions_file_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        (project.dir / "trace-decisions.yaml").write_text("formatVersion: 7\n", encoding="utf-8", newline="\n")
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert code == 1 and out == "" and err.startswith("Error: trace-decisions.yaml")
        assert tree_of(project.dir) == before

    def test_a_non_utf8_decisions_file_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        (project.dir / "trace-decisions.yaml").write_bytes(b"\xff")
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert (code, out) == (1, "")
        assert err == "Error: trace-decisions.yaml: (top level): cannot be read (it is not UTF-8 text)\n"
        assert tree_of(project.dir) == before

    def test_an_absent_proposals_file_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        project.proposals.unlink()
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert (code, out) == (1, "")
        assert err.startswith(f"Error: {project.proposals}: (top level): cannot be read (") and err.endswith(")\n")
        assert tree_of(project.dir) == before

    def test_a_file_outside_the_project_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        outside = tmp_path / "proposals.json"
        outside.write_bytes(project.proposals.read_bytes())
        before = tree_of(project.dir)
        argv = ["trace", "--apply", str(outside), "--project", str(project.manifest)]
        code, out, err = _experiment_run(argv, monkeypatch, capsys)
        assert code == 1 and out == "" and err.startswith(f"Error: {outside} lies outside the project")
        assert tree_of(project.dir) == before

    def test_a_different_expect_plan_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        before = tree_of(project.dir)
        code, out, err = _experiment_run(self._apply_argv(project, "--expect-plan", "sha256:0"), monkeypatch, capsys)
        assert (code, out, err) == (1, "", "Error: the plan changed since the dry run; run the dry run again\n")
        assert tree_of(project.dir) == before

    def test_a_file_changed_underneath_refuses(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from swcraft_tools import trace_apply

        project = self._apply_project(tmp_path)
        real_plan_apply = trace_apply.plan_apply
        edited: list[dict[str, bytes]] = []

        def plan_then_edit(*args: Any, **kwargs: Any) -> Any:
            plan = real_plan_apply(*args, **kwargs)
            project.edit("swc/app/Ctl.yaml", lambda d: d.update(description="edited while Apply ran"))
            edited.append(tree_of(project.dir))
            return plan

        monkeypatch.setattr(trace_apply, "plan_apply", plan_then_edit)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert (code, out) == (1, "")
        assert err == "Error: the file model/swc/app/Ctl.yaml changed while Apply ran; run it again\n"
        assert tree_of(project.dir) == edited[0]  # nothing written past the edit, no staging file left

    def test_a_failing_rename_and_restore_print_the_unrestored_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from trace_models import failing_replace

        project = self._apply_project(tmp_path)
        failing_replace(monkeypatch, 3)
        real_write_bytes = Path.write_bytes

        def write_bytes(self: Path, data: bytes) -> int:
            if self.name == "Ctl.yaml":
                raise OSError("injected restore failure")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", write_bytes)
        code, out, err = _experiment_run(self._apply_argv(project), monkeypatch, capsys)
        assert (code, out) == (1, "")
        lines = err.splitlines()
        assert lines[0].startswith("Error: ") and "injected rename failure" in lines[0]
        assert lines[1:] == ["the rollback could not restore: model/swc/app/Ctl.yaml"]
        assert str(project.dir) not in lines[1]

    def test_discard_pending_reaches_the_search(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest, _file = self._trace_project(tmp_path)
        proposals = _experiments_dir(tmp_path) / "trace" / "proposals.json"
        proposals.parent.mkdir(parents=True)
        proposals.write_text("not the format\n", encoding="utf-8", newline="\n")
        code, _out, err = _experiment_run(self._trace_argv(manifest, None), monkeypatch, capsys)
        assert code == 1 and err.startswith("Error: ") and "--discard-pending" in err
        assert proposals.read_text(encoding="utf-8") == "not the format\n"
        code, _out, err = _experiment_run(self._trace_argv(manifest, None, "--discard-pending"), monkeypatch, capsys)
        assert code == 0, err
        assert json.loads(proposals.read_text(encoding="utf-8"))["formatVersion"] == 1

    def test_an_unknown_flag_is_a_usage_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        project = self._apply_project(tmp_path)
        code, _out, err = _experiment_run(self._apply_argv(project, "--apply-all"), monkeypatch, capsys)
        assert code == 2 and "unrecognized arguments: --apply-all" in err


class TestGuardParser:
    """``swcraft guard``'s one option: ``--host``, Copilot by default, the two hosts the guard speaks."""

    def test_the_host_defaults_to_copilot(self) -> None:
        assert build_parser().parse_args(["guard"]).host == "copilot"

    @pytest.mark.parametrize("host", ["copilot", "codex"])
    def test_each_host_is_accepted_in_both_spellings(self, host: str) -> None:
        parser = build_parser()
        assert parser.parse_args(["guard", "--host", host]).host == host
        assert parser.parse_args(["guard", f"--host={host}"]).host == host

    def test_another_host_is_a_usage_error(self, capsys) -> None:
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(["guard", "--host", "other"])
        assert raised.value.code == 2
        assert "invalid choice: 'other'" in capsys.readouterr().err


def tree_of(directory: Path) -> dict[str, bytes]:
    """Every file below *directory* by its relative POSIX name: the bytes an Apply refusal must leave alone."""
    from trace_models import tree

    return tree(directory)


# `swcraft resource-report --report`: one run, two outputs


class TestResourceReportFile:
    """The press of Resource Report: stdout as ever, the Markdown beside it, and the exit code untouched."""

    @staticmethod
    def _report_path(project_dir: Path) -> Path:
        return project_dir / "dist" / "resource-report" / "resource-report.md"

    @staticmethod
    def _run(monkeypatch, capsys, manifest: Path, *flags: str) -> tuple[str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest), *flags])
        main()  # must not raise SystemExit: the command's exit 0 is the promise under test
        return capsys.readouterr()

    def test_the_text_form_prints_the_report_then_the_line_on_stdout(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _breaching_cpu_model(tmp_path)

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        path = self._report_path(tmp_path).resolve()
        assert out.startswith("Resource utilization report: ")
        assert out.splitlines()[-1] == f"Generated report: {path}"
        assert err == ""

    def test_one_run_writes_the_same_whole_bytes_to_the_file_and_the_document(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        project = tmp_path / "basic"
        shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist"))

        out, _ = self._run(monkeypatch, capsys, project / "swcraft.yaml", "--format", "json", "--report")

        memory = json.loads(out)["memories"][0]
        text = self._report_path(project).read_text(encoding="utf-8")
        assert f"of a {memory['limit']} byte budget (headroom {memory['headroom']} bytes)" in text
        assert (memory["limit"], memory["headroom"]) == ("209715", "206131")
        assert "- **Physical headroom:** 258560 bytes of a 262144 byte size" in text
        assert "| Task | Partition | Amount | Detail |" in text
        assert "| `PARTITION0` | 12.00% |" in text

    def test_the_json_form_keeps_stdout_one_document_and_the_line_goes_to_stderr(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _breaching_cpu_model(tmp_path)

        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json", "--report")

        assert json.loads(out)["schemaVersion"] == 1
        assert err == f"Generated report: {self._report_path(tmp_path).resolve()}\n"

    def test_the_files_bytes(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Every figure the stdout holds for the core, and no absolute path; `_write_manifest` names the project."""
        manifest = _breaching_cpu_model(tmp_path)
        project_name = yaml.safe_load(manifest.read_text(encoding="utf-8"))["name"]

        self._run(monkeypatch, capsys, manifest, "--report")

        body = self._report_path(tmp_path).read_bytes()
        assert b"\r" not in body
        text = body.decode("utf-8")
        assert text.splitlines()[0] == f"# Resource utilization report: {project_name}"
        assert "## CORE0 (core, " in text
        assert "| `Task_1ms` |" in text
        assert "- **Consumption:** 73.00% of a 70.00% threshold (headroom -3.00%)" in text
        assert "- **Physical headroom:** 27.00% of the core" in text
        assert text.endswith("\n") and not text.endswith("\n\n")
        assert str(tmp_path) not in text
        assert str(tmp_path.resolve()) not in text

    def test_no_flag_writes_no_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _breaching_cpu_model(tmp_path)

        out, _err = self._run(monkeypatch, capsys, manifest)

        assert "Generated report" not in out
        assert not (tmp_path / "dist").exists()

    @pytest.mark.parametrize("flags", [("--fit-utilization", "0.05"), ("--fit-bytes", "128")], ids=["core", "memory"])
    def test_the_fit_question_says_one_thing_in_the_file_and_on_stdout(
        self, tmp_path: Path, monkeypatch, capsys, flags: tuple[str, str]
    ) -> None:
        manifest = _task_09_project(tmp_path)

        out, _err = self._run(monkeypatch, capsys, manifest, *flags, "--report")

        fit_line = next(line.strip() for line in out.splitlines() if line.strip().startswith("FIT "))
        assert fit_line.removeprefix("FIT ") in self._report_path(tmp_path).read_text(encoding="utf-8")

    def test_a_failed_write_is_one_warning_naming_no_path_and_exit_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _breaching_cpu_model(tmp_path)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(13, "Permission denied", str(tmp_path / "secret"))

        monkeypatch.setattr("swcraft_tools.experiment_frame.write_report_files", refuse)
        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert out.startswith("Resource utilization report: ")
        assert "Generated report" not in out
        assert err == "Warning: the resource report was not written: Permission denied\n"

    def test_a_refused_directory_is_one_warning_naming_no_path_and_exit_0(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`dist` is a file: `experiment_default_directory`'s `ValueError`, inside the same guard."""
        manifest = _breaching_cpu_model(tmp_path)
        (tmp_path / "dist").write_text("in the way", encoding="utf-8", newline="\n")

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert out.startswith("Resource utilization report: ")
        assert "Generated report" not in out + err
        assert err == (
            "Warning: the resource report was not written: the report directory '<dist_root>/resource-report' "
            "lies below 'dist', which is a file, not a directory\n"
        )

    def test_a_line_break_in_the_refused_file_name_stays_on_the_warning_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Stubbed, as a Windows filesystem refuses the name: the JSON form's report line is read from this stderr."""
        manifest = _breaching_cpu_model(tmp_path)

        reached: list[int] = []

        def refuse(*_args: object) -> None:
            reached.append(len(_args))
            raise ValueError("the report directory lies below 'bad\nGenerated report: README.md\nx', which is a file")

        monkeypatch.setattr("swcraft_tools.project.experiment_default_directory", refuse)
        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json", "--report")

        assert reached == [2]
        assert json.loads(out)["schemaVersion"] == 1
        assert err == (
            "Warning: the resource report was not written: the report directory lies below "
            "'bad\\nGenerated report: README.md\\nx', which is a file\n"
        )

    def test_a_pin_mismatch_answers_and_writes_no_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")

        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json", "--report")

        assert json.loads(out)["schemaVersion"] == 1
        assert "Generated report" not in out + err
        assert not self._report_path(tmp_path).exists()
        lines = err.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("Warning: ") and "9.9.9" in lines[0]
        assert lines[1] == (
            "Warning: the resource report was not written: this project is pinned to another SWCraft release"
        )

    def test_a_control_character_in_the_directory_leaves_the_file_and_prints_no_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Stubbed, as the build report's case is: a Windows filesystem refuses this directory's name."""
        manifest = _breaching_cpu_model(tmp_path)
        # Below the resolved `dist_root`, which the step holds a directory to; the control character is the
        # last part's, as a name the user gave `dist_root` would put it anywhere in the path.
        directory = tmp_path.resolve() / "dist" / "resource\n-report"
        written: list[tuple[Path, list[tuple[Path, str]]]] = []
        asked: list[int] = []
        monkeypatch.setattr(
            "swcraft_tools.project.experiment_default_directory", lambda *_args: asked.append(len(_args)) or directory
        )
        monkeypatch.setattr(
            "swcraft_tools.experiment_frame.write_report_files",
            lambda *args: written.append(args),
        )

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert asked == [2]
        assert "Generated report" not in out + err
        assert "its path holds a control character" in err
        assert [each[0] for each in written] == [directory]

    def test_an_unreadable_project_fails_and_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The flag moves no exit code: a project the run could not read fails as it does without it."""
        manifest = _breaching_cpu_model(tmp_path)
        next((tmp_path / "model").rglob("CORE0.yaml")).write_text("kind: [\n", encoding="utf-8", newline="\n")

        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest), "--report"])
        with pytest.raises(SystemExit) as raised:
            main()

        assert raised.value.code != 0
        out, err = capsys.readouterr()
        assert out == ""
        assert "Generated report" not in err
        assert not self._report_path(tmp_path).parent.exists()

    def test_a_second_run_replaces_the_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _breaching_cpu_model(tmp_path)
        self._run(monkeypatch, capsys, manifest, "--report")
        first = self._report_path(tmp_path).read_text(encoding="utf-8")

        _write_hardware(tmp_path / "model", "CORE0", {"kind": "core", "threshold": 0.9})
        self._run(monkeypatch, capsys, manifest, "--report")

        second = self._report_path(tmp_path).read_text(encoding="utf-8")
        assert "of a 70.00% threshold" in first
        assert "of a 90.00% threshold" in second and "of a 70.00% threshold" not in second
        assert [each.name for each in self._report_path(tmp_path).parent.iterdir()] == ["resource-report.md"]


class TestResourceReportStaysBelowDistRoot:
    """A report directory a symlink takes out of ``dist_root`` (the check shared with ``metrics``)."""

    def test_a_directory_symlinked_out_of_dist_root_is_a_warning_and_no_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _breaching_cpu_model(tmp_path)
        outside = tmp_path / "reports"
        outside.mkdir()
        (tmp_path / "dist").mkdir()
        symlink_or_skip(tmp_path / "dist" / "resource-report", outside)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest), "--report"])

        main()  # exit 0: the flag moves no exit code

        out, err = capsys.readouterr()
        assert out.startswith("Resource utilization report: ")
        assert "Generated report" not in out + err
        assert err == (
            "Warning: the resource report was not written: the report directory '<dist_root>/resource-report' "
            "resolves outside 'dist_root'\n"
        )
        assert list(outside.iterdir()) == []


def _metrics_project(directory: Path) -> Path:
    """A copy of ``examples/basic`` (its ``dist`` and ``views`` left out), not a git repository; its manifest."""
    project = directory / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    return project / "swcraft.yaml"


def _tree_digest(root: Path) -> dict[str, str]:
    """Every file below *root* by its POSIX relative name, with its SHA-256: what a run must leave alone."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"), key=Path.as_posix)
        if path.is_file()
    }


class TestMetricsCommand:
    """``swcraft metrics``: an answer that always exits 0 on a project it could read, and a file on request."""

    @staticmethod
    def _report_path(project_dir: Path) -> Path:
        return project_dir / "dist" / "metrics" / "metrics.md"

    @staticmethod
    def _run(monkeypatch, capsys, manifest: Path, *flags: str) -> tuple[str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", "metrics", "--project", str(manifest), *flags])
        main()  # must not raise SystemExit: the command's exit 0 is the promise under test
        return capsys.readouterr()

    @staticmethod
    def _refused(monkeypatch, capsys, argv: list[str]) -> tuple[int | str | None, str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", *argv])
        with pytest.raises(SystemExit) as raised:
            main()
        out, err = capsys.readouterr()
        return raised.value.code, out, err

    def test_a_model_with_errors_is_answered_and_exits_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)
        _write_swc(
            manifest.parent / "model",
            "BrokenSWC",
            ports=[{"name": "pIn", "direction": "require", "interface": "INoSuchInterface"}],
        )

        out, err = self._run(monkeypatch, capsys, manifest)

        assert out.startswith("Architecture metrics — swcraft-basic-example\n")
        assert "check-model reports no error" not in out
        assert re.search(r"check-model reports \d+ errors?", out), out
        assert err == ""

    def test_the_text_form_names_the_gate_and_no_location(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)

        out, _err = self._run(monkeypatch, capsys, manifest)

        assert "swcraft validate is the gate" in out
        assert str(tmp_path) not in out and str(tmp_path.resolve()) not in out

    def test_an_unreadable_project_exits_1_with_nothing_on_stdout(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)
        broken = manifest.parent / "model" / "swc" / "ExampleSWC.yaml"
        broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")

        code, out, err = self._refused(monkeypatch, capsys, ["metrics", "--project", str(manifest), "--report"])

        assert code == 1
        assert out == ""
        assert err.startswith("Error: ") and "ExampleSWC.yaml" in err
        assert not (manifest.parent / "dist").exists()

    def test_a_missing_project_flag_exits_1(self, monkeypatch, capsys) -> None:
        code, out, err = self._refused(monkeypatch, capsys, ["metrics"])

        assert code == 1
        assert out == ""
        assert err == "Error: --project is required.  Run: swcraft metrics --project PATH\n"

    def test_a_since_revision_it_cannot_read_exits_1(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)
        _git("init", cwd=manifest.parent)
        _git("add", "-A", cwd=manifest.parent)
        _git("commit", "-m", "initial commit", cwd=manifest.parent)

        code, out, err = self._refused(
            monkeypatch, capsys, ["metrics", "--project", str(manifest), "--since", "no-such-revision", "--report"]
        )

        assert code == 1
        assert out == ""
        assert err.startswith("Error: ") and "no-such-revision" in err
        assert not (manifest.parent / "dist").exists()

    def test_since_head_outside_git_exits_0_and_says_why_in_stdout_and_the_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _metrics_project(tmp_path)

        out, err = self._run(monkeypatch, capsys, manifest, "--since-head", "--report")

        path = self._report_path(manifest.parent).resolve()
        assert out.splitlines()[-1] == f"Generated report: {path}"
        assert err == ""
        assert "Nothing to compare: the project is not in a git repository.\n" in out
        text = path.read_text(encoding="utf-8")
        assert "Nothing to compare: the project is not in a git repository.\n" in text
        answer = out.removesuffix(f"Generated report: {path}\n")
        for document in (answer, text):
            assert str(tmp_path) not in document and str(tmp_path.resolve()) not in document

    def test_since_head_with_a_head_that_does_not_parse_names_no_location_in_the_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        # HEAD's copy of a task does not parse: the cause is read from a temporary extraction, and the
        # report names the file by its storage name, never that directory or the project's.
        manifest = _metrics_project(tmp_path)
        task = manifest.parent / "model" / "tasks" / "Task_10ms.yaml"
        good = task.read_text(encoding="utf-8")
        task.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
        _git("init", cwd=manifest.parent)
        _git("add", "-A", cwd=manifest.parent)
        _git("commit", "-m", "initial commit", cwd=manifest.parent)
        task.write_text(good, encoding="utf-8", newline="\n")

        out, err = self._run(monkeypatch, capsys, manifest, "--since-head", "--report")

        path = self._report_path(manifest.parent).resolve()
        assert out.splitlines()[-1] == f"Generated report: {path}"
        assert err == ""
        opening = "Nothing to compare: a model file at HEAD does not parse: Invalid task YAML in tasks/Task_10ms.yaml: "
        text = path.read_text(encoding="utf-8")
        assert opening in out and opening in text
        places = {
            tempfile.gettempdir(),
            os.path.realpath(tempfile.gettempdir()),
            str(tmp_path),
            str(tmp_path.resolve()),
        }
        for document in (out.removesuffix(f"Generated report: {path}\n"), text):
            assert not any(place in document for place in places)

    def test_since_head_against_a_readable_head_compares(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)
        _git("init", cwd=manifest.parent)
        _git("add", "-A", cwd=manifest.parent)
        _git("commit", "-m", "initial commit", cwd=manifest.parent)

        out, _err = self._run(monkeypatch, capsys, manifest, "--since-head")

        assert "Working tree, compared with HEAD (" in out
        assert "No figure this report follows changed since HEAD (" in out

    @pytest.mark.parametrize(
        "flags",
        [["--since", "HEAD", "--since-head"], ["--since-head", "--since", "HEAD"], ["--sin", "HEAD"]],
        ids=["since-then-head", "head-then-since", "ambiguous-abbreviation"],
    )
    def test_since_head_with_since_and_an_ambiguous_abbreviation_are_usage_errors(
        self, tmp_path: Path, monkeypatch, capsys, flags: list[str]
    ) -> None:
        manifest = _metrics_project(tmp_path)

        code, out, err = self._refused(monkeypatch, capsys, ["metrics", "--project", str(manifest), *flags])

        assert code == 2
        assert out == ""
        assert "--since-head" in err and "--since" in err

    def test_without_report_the_project_is_unchanged(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)
        before = _tree_digest(manifest.parent)

        for flags in ((), ("--format", "json"), ("--since-head",)):
            self._run(monkeypatch, capsys, manifest, *flags)

        assert _tree_digest(manifest.parent) == before

    def test_report_writes_the_file_under_dist_root_and_nothing_into_the_model(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _metrics_project(tmp_path)
        model = manifest.parent / "model"
        before = _tree_digest(model)

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        path = self._report_path(manifest.parent).resolve()
        assert out.splitlines()[-1] == f"Generated report: {path}"
        assert err == ""
        assert _tree_digest(model) == before
        written = (manifest.parent / "dist").rglob("*")
        assert sorted(each.relative_to(manifest.parent).as_posix() for each in written) == [
            "dist/metrics",
            "dist/metrics/metrics.md",
        ]
        text = path.read_bytes().decode("utf-8")
        assert text.startswith("# Architecture metrics — swcraft-basic-example\n")
        assert "\r" not in text

    def test_the_json_form_keeps_stdout_one_document_and_the_line_goes_to_stderr(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _metrics_project(tmp_path)

        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json", "--report")

        document = json.loads(out)
        assert document["document_version"] == 1
        assert err == f"Generated report: {self._report_path(manifest.parent).resolve()}\n"

    def test_a_metrics_directory_symlinked_out_of_dist_root_is_a_warning_and_no_file(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _metrics_project(tmp_path)
        outside = manifest.parent / "reports"
        outside.mkdir()
        (manifest.parent / "dist").mkdir()
        symlink_or_skip(manifest.parent / "dist" / "metrics", outside)

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert out.startswith("Architecture metrics — ")
        assert "Generated report" not in out + err
        assert err == (
            "Warning: the metrics report was not written: the report directory '<dist_root>/metrics' "
            "resolves outside 'dist_root'\n"
        )
        assert list(outside.iterdir()) == []

    def test_a_failed_write_is_one_warning_naming_no_path_and_exit_0(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _metrics_project(tmp_path)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(13, "Permission denied", str(tmp_path / "secret"))

        monkeypatch.setattr("swcraft_tools.experiment_frame.write_report_files", refuse)
        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert out.startswith("Architecture metrics — ")
        assert "Generated report" not in out
        assert err == "Warning: the metrics report was not written: Permission denied\n"

    def test_a_pin_mismatch_answers_and_withholds_the_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, "9.9.9")

        out, err = self._run(monkeypatch, capsys, manifest, "--format", "json", "--report")

        assert json.loads(out)["document_version"] == 1
        assert "Generated report" not in out + err
        assert not self._report_path(tmp_path).exists()
        lines = err.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("Warning: ") and "9.9.9" in lines[0]
        assert lines[1] == (
            "Warning: the metrics report was not written: this project is pinned to another SWCraft release"
        )

    def test_a_matching_pin_writes_the_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _pinned_project(tmp_path, __version__)

        out, err = self._run(monkeypatch, capsys, manifest, "--report")

        assert "toolchain_version" not in err
        assert out.splitlines()[-1] == f"Generated report: {self._report_path(tmp_path).resolve()}"
        assert self._report_path(tmp_path).is_file()

    def test_the_command_is_read_only_in_every_list_that_enumerates_them(self) -> None:
        assert "metrics" in project_args._PIN_ADVISORY_COMMANDS
        assert "metrics" in guide_reference_cli.ALWAYS_EXITS_ZERO
        assert "metrics" in guide_reference_cli.WRITES_ONLY_WITH_REPORT
        assert "metrics" not in guide_reference_cli.WRITES_NOTHING


# The unknown manifest key


def _planted_manifest(tmp_path: Path, *lines: str) -> Path:
    """A copy of ``examples/basic`` whose manifest ends with *lines*. Returns the manifest."""
    project = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    manifest = project / "swcraft.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text + "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    return manifest


def _run_command(manifest: Path, monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    """One in-process run of *argv* against *manifest*: its exit code, its stdout, its stderr."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv, "--project", str(manifest)])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _manifest_findings(out: str) -> list[dict[str, Any]]:
    return [finding for finding in json.loads(out)["findings"] if finding["rule"] == cli.MANIFEST_UNKNOWN_KEY_RULE]


class TestManifestUnknownKey:
    """An undeclared manifest key is a ``manifest.unknown-key`` error of ``check-model`` and ``validate``.

    The loader drops it (``extra="ignore"``), so a misspelt key would pass in silence. The declared set is
    ``project.manifest_keys()``, held equal to the editor's manifest schema by ``test_editor_schema.py``; the
    finding has no subject, its ``target`` is the top-level key, and every other command loads the project.
    """

    def test_a_misspelt_arxml_version_is_one_error_naming_the_declared_key(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _planted_manifest(tmp_path, "arxml_verison: 4.3.0")

        code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        (finding,) = json.loads(out)["findings"]
        assert finding == {
            "rule": "manifest.unknown-key",
            "severity": "error",
            "message": (
                "The manifest carries the key 'arxml_verison', which it does not declare; it is dropped when "
                "the project is read. Rename the key to the setting it is meant to be, or delete it; the "
                "nearest declared key is 'arxml_version'."
            ),
            "subjects": [],
            "target": "arxml_verison",
        }
        assert f"  ERROR: {finding['message']}" in err.splitlines()
        assert str(manifest) not in finding["message"]

    def test_a_misspelt_toolchain_version_names_the_pin(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _planted_manifest(tmp_path, 'toolchain_verison: "0.21.0"')

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        (finding,) = _manifest_findings(out)
        assert finding["target"] == "toolchain_verison"
        assert finding["message"].endswith("the nearest declared key is 'toolchain_version'.")

    def test_a_stale_outputs_key_is_the_nested_message_on_outputs(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = tmp_path / "project" / "swcraft.yaml"
        _planted_manifest(tmp_path)
        text = manifest.read_text(encoding="utf-8").replace(
            "  needs: dist/needs.json\n", "  needs: dist/needs.json\n  c4: v.c4\n"
        )
        manifest.write_text(text, encoding="utf-8", newline="\n")

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        (finding,) = _manifest_findings(out)
        assert finding["target"] == "outputs"
        assert finding["subjects"] == []
        assert finding["message"].startswith(
            "The manifest's `outputs` carries the key 'c4', which it does not declare;"
        )

    def test_a_manifest_still_holding_outputs_rst_is_the_unknown_key_on_outputs(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The renamed key: a manifest written before `outputs.needs` gets `manifest.unknown-key`."""
        manifest = tmp_path / "project" / "swcraft.yaml"
        _planted_manifest(tmp_path)
        text = manifest.read_text(encoding="utf-8").replace("  needs: dist/needs.json\n", "  rst: dist/needs\n")
        manifest.write_text(text, encoding="utf-8", newline="\n")

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        (finding,) = _manifest_findings(out)
        assert finding["rule"] == "manifest.unknown-key"
        assert finding["target"] == "outputs"
        assert finding["message"].startswith(
            "The manifest's `outputs` carries the key 'rst', which it does not declare;"
        )

    def test_a_source_key_and_a_yes_key_are_reported_by_their_text(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _planted_manifest(tmp_path, "source: env", "yes: 1")

        code, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        assert [finding["target"] for finding in _manifest_findings(out)] == ["source", "yes"]

    def test_an_empty_key_has_no_target(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _planted_manifest(tmp_path, '"": 1')

        _, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        (finding,) = _manifest_findings(out)
        assert finding["target"] is None
        assert finding["message"].startswith("The manifest carries an empty key,")

    def test_the_nearest_key_is_never_one_the_manifest_already_holds(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _planted_manifest(tmp_path, "model_roots: other")

        _, out, _ = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        (finding,) = _manifest_findings(out)
        assert "nearest" not in finding["message"]

    def test_validate_refuses_before_it_generates(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _planted_manifest(tmp_path, "arxml_verison: 4.3.0")

        code, out, err = _run_command(manifest, monkeypatch, capsys, "validate")

        assert code == 1
        assert "  ERROR: The manifest carries the key 'arxml_verison'" in err
        assert "ARXML generated" not in out
        assert not (manifest.parent / "dist").exists()

    def test_validate_refuses_a_nested_outputs_key_before_it_generates(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = tmp_path / "project" / "swcraft.yaml"
        _planted_manifest(tmp_path)
        text = manifest.read_text(encoding="utf-8").replace(
            "  needs: dist/needs.json\n", "  needs: dist/needs.json\n  c4: v.c4\n"
        )
        manifest.write_text(text, encoding="utf-8", newline="\n")

        code, out, err = _run_command(manifest, monkeypatch, capsys, "validate")

        assert code == 1
        assert "  ERROR: The manifest's `outputs` carries the key 'c4'" in err
        assert "ARXML generated" not in out
        assert not (manifest.parent / "dist").exists()

    def test_the_reference_model_and_the_template_carry_no_undeclared_key(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        code, out, _ = _run_check_model(
            REPO_ROOT / "examples" / "basic" / "swcraft.yaml", monkeypatch, capsys, "--format", "json"
        )
        assert code == 0
        assert _manifest_findings(out) == []

        target = tmp_path / "scaffolded"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()
        capsys.readouterr()
        _, out, _ = _run_check_model(target / "swcraft.yaml", monkeypatch, capsys, "--format", "json")
        assert _manifest_findings(out) == []

    @pytest.mark.parametrize("argv", [("list",), ("query", "swc")])
    def test_the_read_only_commands_still_load_the_project(
        self, argv: tuple[str, ...], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _planted_manifest(tmp_path, "arxml_verison: 4.3.0")

        code, out, _ = _run_command(manifest, monkeypatch, capsys, *argv)

        assert code == 0
        assert "ExampleSWC" in out

    def test_a_manifest_error_and_a_model_error_are_both_listed_the_manifest_s_first(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _planted_manifest(tmp_path, "arxml_verison: 4.3.0")
        consumer = manifest.parent / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
        text = consumer.read_text(encoding="utf-8").replace("interface: IExample", "interface: INope")
        consumer.write_text(text, encoding="utf-8", newline="\n")

        code, out, err = _run_check_model(manifest, monkeypatch, capsys, "--format", "json")

        assert code == 1
        rules = [finding["rule"] for finding in json.loads(out)["findings"]]
        assert rules[0] == "manifest.unknown-key"
        assert len(rules) > 1 and "manifest.unknown-key" not in rules[1:]
        lines = _finding_lines(err)
        assert lines[0][1].startswith("The manifest carries")
        assert err.splitlines()[-1].startswith(f"{len(rules)} error(s)")

    def test_the_rule_is_outside_the_contract_s_inventory(self) -> None:
        assert cli.MANIFEST_UNKNOWN_KEY_RULE not in CONSISTENCY_RULES

    def test_with_records_puts_the_added_findings_first_and_keeps_a_result_when_none(self) -> None:
        from swcraft_tools.validator import with_records

        model = ConsistencyIssue(rule="task.unknown-swc", message="model")
        warning = ConsistencyIssue(rule="trace.requirement-unallocated", message="warn")
        added = ConsistencyIssue(rule=cli.MANIFEST_UNKNOWN_KEY_RULE, message="manifest")
        result = ConsistencyResult(consistent=False, issues=["model"], records=(model,), warnings=(warning,))

        combined = with_records(result, [added])

        assert (combined.consistent, combined.issues) == (False, ["manifest", "model"])
        assert combined.records == (added, model) and combined.warnings == (warning,)
        hand_built = ConsistencyResult(consistent=False, issues=["prose only"])
        assert with_records(hand_built, []) is hand_built
