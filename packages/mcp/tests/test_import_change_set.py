"""`import_component` plans; only `change_set` mutates.

`import_component` returns a **draft change-set request** and writes nothing: no model bytes, no audit bytes. The
client submits the draft -- with any companion operations, such as a requirement trace link for a new SWC --
through `change_set`, where the review boundary, the approved scope, the delta gate and the lifecycle records apply.
The tool does not commit itself because there is one mutation surface: accepting companion operations would make it
a second generic write tool.

The CLI's `swcraft import component` still applies the same plan itself.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
import swcraft_mcp.server as server
import yaml
from conftest import model_element_path
from fastmcp.exceptions import ToolError
from protocol_client import DECLINE, ScriptedHuman, audit_records, call, call_reviewed
from swcraft_tools import component_exchange, writer
from swcraft_tools.arxml_import.registry import stub_name
from swcraft_tools.editing.audit import LIFECYCLE_OPERATIONS
from swcraft_tools.model_state import load_layer

from tests.test_approved_scope_channel import set_scope
from tests.test_review_mode import set_mode, state

#: The SWC the *foreign* export introduces; it satisfies no requirement of the target yet.
FOREIGN_SWC = "TempSensor"
FOREIGN_INTERFACE = "ITemp"
FOREIGN_TYPE = "int16"


def _manifest(root: Path) -> Path:
    path = root / "swcraft.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": root.name,
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _declare_no_support(model: Path, group: str, swc: str) -> None:
    """Give the component *swc* ``handle_termination_and_restart: no_support``.

    Without it the ARXML writer refuses the component; ``writer.create_swc`` takes no such parameter, so the line
    is appended to the file it has just written.
    """
    path = model / "swc" / group / f"{swc}.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "handle_termination_and_restart: no_support\n",
        encoding="utf-8",
        newline="\n",
    )


def _target_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small, cross-layer-**consistent** project, and the server pointed at it.

    Pass D validates the whole staged model, so a target that already broke a rule would fail every change set.
    Hence the processor and core (``hardware.no-processor``) and the root composition placing the one component
    (``composition.no-root``).
    """
    model = root / "model"
    model.mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)
    target = str(model)
    writer.create_data_type(
        target, "float32_base", "base_type", size=32, encoding="IEEE754", native_declaration="float"
    )
    writer.create_data_type(target, "float32", "value_type", base="float32_base")
    writer.create_interface(target, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    writer.create_swc(target, "SpeedSensor", "sensors")
    _declare_no_support(model, "sensors", "SpeedSensor")
    writer.add_port(target, "SpeedSensor", "pSpeed", "provide", "ISpeed")
    writer.create_requirement(target, "REQ_001", "Speed sensing", "The system shall measure vehicle speed.")
    writer.add_trace_link(target, "REQ_001", "SpeedSensor")
    writer.create_hardware_element(target, "CPU", "processor")
    writer.create_hardware_element(target, "CORE0", "core", threshold=0.7)
    # Written as bytes: `create_composition` creates an empty composition by design.
    (model / "compositions").mkdir(parents=True, exist_ok=True)
    (model / "compositions" / "Root.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "Root",
                "title": "Root composition",
                "root": True,
                "members": [{"name": "SpeedSensor", "type": "SpeedSensor"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    manifest = _manifest(root)
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))
    # The manifest moved, and `_get_project()` caches the loaded project.
    server._PROJECT = None
    return root


def _foreign_export(root: Path) -> str:
    """A component export from a **different** project: an SWC the target lacks."""
    model = root / "source_model"
    model.mkdir(parents=True, exist_ok=True)
    source = str(model)
    writer.create_data_type(
        source, f"{FOREIGN_TYPE}_base", "base_type", size=16, encoding="NONE", native_declaration="int16_t"
    )
    writer.create_data_type(source, FOREIGN_TYPE, "value_type", base=f"{FOREIGN_TYPE}_base")
    writer.create_interface(source, FOREIGN_INTERFACE, [{"name": "celsius", "implementation_type": FOREIGN_TYPE}])
    writer.create_swc(source, FOREIGN_SWC, "body")
    _declare_no_support(model, "body", FOREIGN_SWC)
    writer.add_port(source, FOREIGN_SWC, "pTemp", "provide", FOREIGN_INTERFACE)
    export = root / f"{FOREIGN_SWC}.arxml"
    component_exchange.export_component(source, FOREIGN_SWC, str(export))
    return str(export)


def _own_export(project: Path) -> str:
    """The target project's own `SpeedSensor`, exported — the `update` case."""
    export = project / "SpeedSensor.arxml"
    component_exchange.export_component(str(project / "model"), "SpeedSensor", str(export))
    return str(export)


def _diverge(project: Path) -> None:
    """Make the target's own `SpeedSensor` diverge from the exported one."""
    path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runnables"] = [{"name": "Extra", "min_start_interval": 0.0, "events": []}]
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _audit(project: Path) -> list[str]:
    log = project / "dist" / "audit.jsonl"
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _baseline() -> str:
    return call("discover").structured["baseline"]


def _plan(project: Path, source: str, **kwargs: Any) -> dict[str, Any]:
    """`import_component` at the current baseline, asserted successful."""
    result = server.import_component(source=source, baseline=_baseline(), **kwargs)
    assert result["success"] is True, result
    return result


def _trace_link(swc: str = FOREIGN_SWC, requirement: str = "REQ_001") -> dict[str, Any]:
    """A companion operation the user may name for a new SWC: a requirement trace link.

    One link is a `create` of the member `<requirement>@1` in the component's `satisfies`, not an `update` of the
    whole list, which would ask a reviewer to approve every other link again.
    """
    member = f"{requirement}@1"
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": swc,
            "path": [{"field": "satisfies"}, {"member": member}],
        },
        "value": member,
    }


def _submit(draft: dict[str, Any], *extra: dict[str, Any], human: ScriptedHuman | None = None) -> Any:
    """Submit a returned draft, plus any companion operations, through `change_set`."""
    named: dict[str, Any] = {
        "baseline": draft["baseline"],
        "operations": [*draft["operations"], *extra],
    }
    if draft.get("intent"):
        named["intent"] = draft["intent"]
    if human is None:
        return call("change_set", **named).structured
    return call_reviewed(human, "change_set", **named).structured


# ---------------------------------------------------------------------------
# The planning envelope
# ---------------------------------------------------------------------------


class TestThePlanningEnvelope:
    def test_the_callers_baseline_is_copied_unchanged_into_the_draft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        baseline = _baseline()

        result = server.import_component(source=source, baseline=baseline)

        assert result["change_set"]["operation"] == "change_set"
        assert result["change_set"]["baseline"] == baseline
        assert state(project) == state(project)  # sanity: the digest is stable

    @pytest.mark.parametrize(("version", "expected"), [(None, "4.2.2"), ("4.3.0", "4.3.0")])
    def test_the_planner_is_handed_the_project_s_arxml_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None, expected: str
    ) -> None:
        """The asynchronous call rules judge an update at the project's edition, as the CLI's."""
        project = _target_project(tmp_path / "project", monkeypatch)
        if version is not None:
            manifest = project / "swcraft.yaml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
            )
            server._PROJECT = None
        source = _foreign_export(tmp_path)
        handed: list[Any] = []
        real = component_exchange.plan_component_import

        def recording(*arguments: Any, **keywords: Any) -> Any:
            handed.append(keywords["arxml_version"])
            return real(*arguments, **keywords)

        monkeypatch.setattr(component_exchange, "plan_component_import", recording)

        _plan(project, source)

        assert handed == [expected]

    @pytest.mark.parametrize(("version", "named"), [(None, True), ("4.3.0", False)])
    def test_the_answer_names_an_asynchronous_call_the_import_leaves_without_a_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None, named: bool
    ) -> None:
        """The plan's report carries the CLI's line at the project's edition.

        A 4.3.0 export whose asynchronous call has no result point, planned into a project at the default 4.2.2
        (named) and at 4.3.0 (not).
        """
        import shutil

        from conftest import REPO_ROOT

        project = _target_project(tmp_path / "project", monkeypatch)
        if version is not None:
            manifest = project / "swcraft.yaml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
            )
            server._PROJECT = None
        source_model = tmp_path / "access_forms"
        shutil.copytree(
            REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model", source_model
        )
        controller = source_model / "swc" / "chassis" / "BrakeController.yaml"
        document = yaml.safe_load(controller.read_text(encoding="utf-8"))
        document["runnables"] = [r for r in document["runnables"] if r["name"] != "OnBlockWritten"]
        controller.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
        )
        export = tmp_path / "BrakeController.arxml"
        component_exchange.export_component(str(source_model), "BrakeController", str(export), version="4.3.0")

        result = _plan(project, str(export))

        line = (
            "runnable 'ComputeBrakeTorque' calls operation 'WriteBlock' of port 'rNvStorage' asynchronously and "
            "nothing fetches its result: rule swc.call.async-without-result, [constr_2006] of AUTOSAR R4.2.2"
        )
        assert (line in result["output"]) is named
        assert result["change_set"] is not None

    def test_the_answer_names_an_asynchronous_call_finding_an_update_introduces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plan's report carries the CLI's line for the other asynchronous call rules.

        An update in the carried regime that keeps the target's ``asynchronous`` beside a call the file adds
        (``swc.call.mixed-synchrony``).
        """
        import shutil

        from conftest import REPO_ROOT

        project = _target_project(tmp_path / "project", monkeypatch)
        fixture = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
        first = tmp_path / "first.arxml"
        component_exchange.export_component(str(fixture), "BrakeController", str(first))
        component_exchange.import_component(str(first), str(project / "model"))
        server._PROJECT = None

        source_model = tmp_path / "access_forms"
        shutil.copytree(fixture, source_model)
        controller = source_model / "swc" / "chassis" / "BrakeController.yaml"
        document = yaml.safe_load(controller.read_text(encoding="utf-8"))
        for runnable in document["runnables"]:
            runnable["events"] = [e for e in runnable.get("events", []) if e["type"] != "call_returned"]
            for entry in runnable.get("calls", []):
                for key in ("asynchronous", "poll", "result", "timeout"):
                    entry.pop(key, None)
            if runnable["name"] == "Diagnose":
                runnable["calls"].append({"port": "rNvStorage", "operation": "WriteBlock"})
        controller.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
        )
        export = tmp_path / "BrakeController.arxml"
        component_exchange.export_component(str(source_model), "BrakeController", str(export))
        text = export.read_text(encoding="utf-8")
        text = re.sub(r'\n *<SD GID="ASYNCHRONOUS-CALLS">[^<]*</SD>', "", text)
        export.write_text(text, encoding="utf-8", newline="\n")

        result = _plan(project, str(export), update=True)

        assert "ASYNCHRONOUS CALL FINDINGS (1;" in result["output"]
        assert "rule swc.call.mixed-synchrony: SWC 'BrakeController' calls operation 'WriteBlock'" in result["output"]

    def test_the_answer_names_an_implicit_read_finding_a_carried_update_introduces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plan's report carries the CLI's lines for an implicit read ``check-model`` refuses.

        The carried regime writes the target's ``by: implicit`` back onto a read whose port's com-spec the file
        now flags ``enable_update`` (``[constr_1103]``).
        """
        import shutil

        from conftest import REPO_ROOT

        project = _target_project(tmp_path / "project", monkeypatch)
        fixture = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
        first = tmp_path / "first.arxml"
        component_exchange.export_component(str(fixture), "BrakeController", str(first))
        component_exchange.import_component(str(first), str(project / "model"))
        server._PROJECT = None

        source_model = tmp_path / "access_forms"
        shutil.copytree(fixture, source_model)
        controller = source_model / "swc" / "chassis" / "BrakeController.yaml"
        document = yaml.safe_load(controller.read_text(encoding="utf-8"))
        for runnable in document["runnables"]:
            for entry in (*runnable.get("reads", []), *runnable.get("writes", [])):
                entry.pop("by", None)
        for port in document["ports"]:
            if port["name"] == "rWheelSpeed":
                port["com_specs"] = [{"data_element": "speed", "enable_update": True}]
        controller.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
        )
        export = tmp_path / "BrakeController.arxml"
        component_exchange.export_component(str(source_model), "BrakeController", str(export))
        text = export.read_text(encoding="utf-8")
        text = re.sub(r'\n *<SD GID="IMPLICIT-ACCESS">[^<]*</SD>', "", text)
        export.write_text(text, encoding="utf-8", newline="\n")

        result = _plan(project, str(export), update=True)

        assert "IMPLICIT ACCESS FINDINGS (2;" in result["output"]
        assert (
            "rule swc.access.implicit-read-enable-update: SWC 'BrakeController' runnable 'ComputeBrakeTorque' reads "
            "data element 'speed' through port 'rWheelSpeed' implicitly"
        ) in result["output"]

    def test_the_tool_does_not_validate_the_baseline_it_is_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The submitting `change_set` is the authoritative check; a token accepted here and refused there applies
        # nothing on either side.
        _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)

        result = server.import_component(source=source, baseline="rev1:AAAAAAAAAAAAAAAAAAAAAA")

        assert result["success"] is True
        assert result["change_set"]["baseline"] == "rev1:AAAAAAAAAAAAAAAAAAAAAA"

    def test_a_stale_draft_is_refused_by_the_subsequent_change_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        draft = _plan(project, source)["change_set"]
        writer.create_requirement(str(project / "model"), "REQ_002", "Later", "Something else moved the model.")
        before = state(project)

        response = _submit(draft, _trace_link())

        assert response["error"] == "stale-baseline"
        assert state(project) == before

    def test_the_baseline_argument_is_required_at_the_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Required so that a client must call `discover` first.
        _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)

        with pytest.raises(ToolError):
            call("import_component", source=source)

    def test_the_misleading_dry_run_argument_is_gone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The tool only plans, so a `dry_run` argument would mean nothing; the CLI keeps `--dry-run`.
        _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)

        with pytest.raises(ToolError):
            call("import_component", source=source, baseline=_baseline(), dry_run=True)

        assert "dry_run" not in inspect.signature(server.import_component).parameters

    def test_absent_targets_derive_one_element_create_each(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)

        draft = _plan(project, source)["change_set"]

        assert [(op["operation"], op["address"]) for op in draft["operations"]] == [
            ("create", {"layer": "swc", "element": FOREIGN_SWC}),
            ("create", {"layer": "interfaces", "element": FOREIGN_INTERFACE}),
            # The base type is in the `base_types` layer, whose creates follow `implementation_types`'.
            ("create", {"layer": "implementation_types", "element": FOREIGN_TYPE}),
            ("create", {"layer": "base_types", "element": f"{FOREIGN_TYPE}_base"}),
        ]
        # the registry-declared placement value travels with the element
        assert draft["operations"][0]["value"]["group"] == "components"

    def test_an_identical_reimport_is_a_successful_no_op_with_no_change_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Never an empty change set: `operations` has `minItems: 1`, so it would be a schema violation.
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)

        result = _plan(project, source)

        assert result["change_set"] is None
        assert result["errors"] == []
        assert "unchanged" in result["output"]

    @pytest.mark.parametrize(
        "entry",
        [{"data_element": "speed"}, {"data_element": "speed", "uses_end_to_end_protection": False}],
        ids=["attribute-less", "silent-value"],
    )
    @pytest.mark.parametrize("update", [False, True], ids=["plain", "update"])
    def test_a_silent_com_spec_spelling_is_no_change_set_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict[str, Any], update: bool
    ) -> None:
        # A com-spec value at the writer's silent value exports as nothing: the project's own export is
        # `unchanged`, never a conflict or a draft that removes the entry.
        project = _target_project(tmp_path / "project", monkeypatch)
        path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        (port,) = data["ports"]
        port["com_specs"] = [entry]
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")
        source = _own_export(project)

        result = _plan(project, source, update=update)

        assert result["change_set"] is None
        assert result["errors"] == []
        assert "unchanged" in result["output"]

    def test_a_planning_error_is_unsuccessful_with_no_change_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)

        result = server.import_component(source=str(tmp_path / "nope.arxml"), baseline=_baseline())

        assert result["success"] is False
        assert result["change_set"] is None
        assert result["errors"] and result["errors"][0]["message"]
        assert result["output"] == ""
        assert state(project) == state(project)

    def test_a_conflicting_import_is_unsuccessful_with_no_change_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)
        _diverge(project)

        result = server.import_component(source=source, baseline=_baseline())

        assert result["success"] is False
        assert result["change_set"] is None
        assert "SpeedSensor" in result["errors"][0]["message"]

    def test_the_envelope_always_carries_the_same_four_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)

        planned = server.import_component(source=source, baseline=_baseline())
        failed = server.import_component(source=str(tmp_path / "nope.arxml"), baseline=_baseline())
        noop = server.import_component(source=_own_export(project), baseline=_baseline())

        for result in (planned, failed, noop):
            assert list(result) == ["success", "errors", "output", "change_set"], result

    def test_every_planning_call_leaves_the_model_and_the_audit_log_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        foreign = _foreign_export(tmp_path)
        own = _own_export(project)
        before_model, before_audit = state(project), _audit(project)

        server.import_component(source=foreign, baseline=_baseline())
        server.import_component(source=own, baseline=_baseline())
        server.import_component(source=str(tmp_path / "nope.arxml"), baseline=_baseline())

        assert state(project) == before_model
        assert _audit(project) == before_audit


# ---------------------------------------------------------------------------
# An existing element: leaf operations, and the payload replayed
# ---------------------------------------------------------------------------


class TestAnExistingElementDerivesLeafOperations:
    def test_an_update_never_derives_an_illegal_element_level_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A protocol `update` addresses a field, member or map entry, never a whole element.
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)
        _diverge(project)

        draft = _plan(project, source, update=True)["change_set"]

        assert draft["operations"], "a divergent update must derive at least one operation"
        for operation in draft["operations"]:
            assert operation["address"]["element"] == "SpeedSensor"
            if operation["operation"] == "update":
                assert operation["address"].get("path"), operation

    def test_an_update_derives_no_operation_on_a_silent_com_spec_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The file cannot tell a com-spec value at the writer's silent value from silence; an update of something
        # else keeps the target's spelling, so the draft removes nothing.
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)
        _diverge(project)
        path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        (port,) = data["ports"]
        port["com_specs"] = [{"data_element": "speed", "handle_out_of_range": "none"}]
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")

        draft = _plan(project, source, update=True)["change_set"]

        assert draft["operations"], "the divergent runnable is still an operation"
        assert "com_specs" not in json.dumps(draft), draft

    def test_the_derived_operations_are_the_leaf_the_local_change_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)
        _diverge(project)

        draft = _plan(project, source, update=True)["change_set"]

        assert draft["operations"] == [
            {
                "operation": "remove",
                "address": {
                    "layer": "swc",
                    "element": "SpeedSensor",
                    "path": [{"field": "runnables"}, {"member": "Extra"}],
                },
            }
        ]

    def test_committing_the_draft_reproduces_the_cli_import_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The derived operations replay the planner's payload: the CLI's path and the protocol's leave the same bytes.
        applied = _target_project(tmp_path / "cli", monkeypatch)
        cli_source = _own_export(applied)
        _diverge(applied)
        component_exchange.import_component(cli_source, str(applied / "model"), update=True)
        expected = state(applied)

        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export(project)
        _diverge(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source, update=True)["change_set"]

        response = _submit(draft, human=ScriptedHuman(True))

        assert response["status"] == "committed"
        assert state(project) == expected

    def test_an_update_carries_the_target_access_lists_and_derives_no_operation_over_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unmarked export (the `carried` regime) keeps a target runnable's `reads`/`writes`/`calls`.

        That holds when the export's runnable carries none: the leaf operations name the real divergence only, the
        report says what was carried, and the commit leaves the model the CLI's path leaves.

        The bytes differ by one line: the hand-dumped fixture states `min_start_interval: 0.0`, which the CLI's
        whole-file write omits at the default and a change set, editing only what it removes, keeps. The tree is
        compared byte for byte once that line is accounted for."""

        def _sense_then_diverge(project: Path) -> None:
            path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["runnables"] = [
                {
                    "name": "Sense",
                    "min_start_interval": 0.0,
                    "events": [],
                    "writes": [{"port": "pSpeed", "element": "speed"}],
                },
                {"name": "Extra", "min_start_interval": 0.0, "events": []},
            ]
            path.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
            )

        def _with_sense(project: Path) -> str:
            writer.add_runnable(str(project / "model"), "SpeedSensor", "Sense", min_start_interval=0.0)
            return _own_export(project)

        applied = _target_project(tmp_path / "cli", monkeypatch)
        cli_source = _with_sense(applied)
        _sense_then_diverge(applied)
        component_exchange.import_component(cli_source, str(applied / "model"), update=True)
        expected = state(applied)

        project = _target_project(tmp_path / "project", monkeypatch)
        source = _with_sense(project)
        _sense_then_diverge(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        result = _plan(project, source, update=True)
        draft = result["change_set"]

        assert "ACCESS LISTS preserved from the target project" in result["output"]
        assert "Sense: writes=1" in result["output"]
        assert draft["operations"] == [
            {
                "operation": "remove",
                "address": {
                    "layer": "swc",
                    "element": "SpeedSensor",
                    "path": [{"field": "runnables"}, {"member": "Extra"}],
                },
            }
        ]

        response = _submit(draft, human=ScriptedHuman(True))

        assert response["status"] == "committed"
        assert load_layer(project / "model", "swc") == load_layer(applied / "model", "swc")
        kept_file = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
        kept_text = kept_file.read_bytes().decode("utf-8")
        kept = yaml.safe_load(kept_text)
        assert kept["runnables"][0]["writes"] == [{"port": "pSpeed", "element": "speed"}]

        replan = _plan(project, source, update=True)
        assert replan["change_set"] is None
        assert "unchanged" in replan["output"]

        explicit_default = "- name: Sense\n  min_start_interval: 0.0\n"
        assert kept_text.count(explicit_default) == 1
        kept_file.write_bytes(kept_text.replace(explicit_default, "- name: Sense\n").encode("utf-8"))
        assert state(project) == expected

    def test_the_replace_keyword_reaches_the_planner_and_the_lists_go(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`replace_access_lists` reaches the planner the CLI calls, so both decide an update's access lists alike.

        On an unmarked export (no access list, so no access-points marker) the keyword makes the change set remove
        the target's own list instead of carrying it.
        """

        def _sense_then_diverge(project: Path) -> None:
            path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["runnables"] = [
                {
                    "name": "Sense",
                    "min_start_interval": 0.02,
                    "events": [],
                    "writes": [{"port": "pSpeed", "element": "speed"}],
                }
            ]
            path.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
            )

        project = _target_project(tmp_path / "project", monkeypatch)
        writer.add_runnable(str(project / "model"), "SpeedSensor", "Sense", min_start_interval=0.0)
        source = _own_export(project)
        assert "ACCESS-POINTS" not in Path(source).read_text(encoding="utf-8"), "the export must carry no access marker"
        _sense_then_diverge(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        result = _plan(project, source, update=True, replace_access_lists=True)

        assert "ACCESS LISTS regime: replace-flag" in result["output"]
        assert "ACCESS LISTS preserved" not in result["output"]
        response = _submit(result["change_set"], human=ScriptedHuman(True))

        assert response["status"] == "committed"
        written = yaml.safe_load(
            (project / "model" / "swc" / "sensors" / "SpeedSensor.yaml").read_text(encoding="utf-8")
        )
        assert written["runnables"][0].get("writes") is None

    def test_the_replace_keyword_without_update_decides_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without `update` the keyword is ignored.

        The defect: the keyword selected the `replace-flag` regime regardless of `update`, so an unmarked export
        whose access lists were the only divergence planned a refusal instead of a no-op. The CLI refuses the
        combination outright; the tool's planner ignores it.
        """

        def _declare_a_write(project: Path) -> None:
            path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["runnables"] = [
                {
                    "name": "Sense",
                    "min_start_interval": 0.0,
                    "events": [],
                    "writes": [{"port": "pSpeed", "element": "speed"}],
                }
            ]
            path.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
            )

        project = _target_project(tmp_path / "project", monkeypatch)
        writer.add_runnable(str(project / "model"), "SpeedSensor", "Sense", min_start_interval=0.0)
        source = _own_export(project)
        assert "ACCESS-POINTS" not in Path(source).read_text(encoding="utf-8"), "the export must carry no access marker"
        # the lists are now the only difference between the export and the target
        _declare_a_write(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        result = _plan(project, source, replace_access_lists=True)

        assert result["change_set"] is None, result
        assert "SWC SpeedSensor: unchanged" in result["output"]
        assert "ACCESS LISTS regime" not in result["output"]
        kept = yaml.safe_load((project / "model" / "swc" / "sensors" / "SpeedSensor.yaml").read_text(encoding="utf-8"))
        assert kept["runnables"][0]["writes"] == [{"port": "pSpeed", "element": "speed"}]


# ---------------------------------------------------------------------------
# The cross-layer acceptance pair
# ---------------------------------------------------------------------------


class TestTheCrossLayerAcceptancePair:
    """The planned change set without and with its trace link.

    Both commit, `validate` lists nothing after either, and only the component's own `satisfies` tells them apart.
    """

    def test_a_new_swc_without_its_trace_link_commits_and_validate_lists_no_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]
        human = ScriptedHuman(True)

        response = _submit(draft, human=human)

        assert response["status"] == "committed", response
        assert human.questions, "the commit went through the review boundary"
        # The committed response carries no warning; a warning would be `validate`'s to list.
        assert set(response) == {"response", "status", "baseline", "delta", "audit_ref"}
        assert "warning" not in json.dumps(response).lower()
        component_file = project / "model" / "swc" / "components" / f"{FOREIGN_SWC}.yaml"
        assert component_file.is_file()
        assert "satisfies" not in yaml.safe_load(component_file.read_text(encoding="utf-8"))
        verdict = server.validate()
        assert verdict["success"] is True, verdict["errors"]
        assert verdict["errors"] == []
        assert verdict["warnings"] == []

    def test_the_same_draft_with_the_trace_link_companion_commits_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]

        response = _submit(draft, _trace_link(), human=ScriptedHuman(True))

        assert response["status"] == "committed"
        model = project / "model"
        assert (model / "swc" / "components" / f"{FOREIGN_SWC}.yaml").is_file()
        assert (model / "interfaces" / f"{FOREIGN_INTERFACE}.yaml").is_file()
        assert (model_element_path(model, FOREIGN_TYPE, kind="value_type")).is_file()
        component = yaml.safe_load((model / "swc" / "components" / f"{FOREIGN_SWC}.yaml").read_text(encoding="utf-8"))
        assert component["satisfies"] == ["REQ_001@1"]
        verdict = server.validate()
        assert verdict["success"] is True, verdict["errors"]
        assert verdict["warnings"] == []

    def test_the_importer_owned_files_are_the_ones_the_cli_import_would_have_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        applied = _target_project(tmp_path / "cli", monkeypatch)
        source = _foreign_export(tmp_path)
        component_exchange.import_component(source, str(applied / "model"))
        # The CLI side links the component the way an architect would after `import component`.
        writer.add_trace_link(str(applied / "model"), "REQ_001", FOREIGN_SWC)

        project = _target_project(tmp_path / "project", monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]

        assert _submit(draft, _trace_link(), human=ScriptedHuman(True))["status"] == "committed"

        for relative in (
            Path("swc") / "components" / f"{FOREIGN_SWC}.yaml",
            Path("interfaces") / f"{FOREIGN_INTERFACE}.yaml",
            Path("data_types") / "implementation" / f"{FOREIGN_TYPE}.yaml",
        ):
            assert (project / "model" / relative).read_bytes() == (applied / "model" / relative).read_bytes(), relative


class TestAFileNameAnotherElementHoldsIsDecidedBehindTheBoundary:
    """A create whose plain file name another element holds: the same file, decided in two places.

    The CLI's report names the suffixed file; the MCP tool hands the protocol elements, never paths, and the commit
    decides the file by the same rule -- so the plan names no file and the committed tree is the CLI's.
    """

    @staticmethod
    def _hold_the_foreign_type_name(project: Path) -> None:
        """``Int16.yaml`` holds ``int16.yaml``'s name case-insensitively on every filesystem.

        A value type, since only one in ``data_types/implementation/`` holds the incoming ``int16``'s name.
        """
        writer.create_data_type(str(project / "model"), "Int16", "value_type", base="float32_base")
        # A sub-package: in one package the two names, equal ignoring case, would refuse the import, and this case
        # is about the file the name takes.
        holder = model_element_path(project / "model", "Int16", kind="value_type")
        holder.write_text(holder.read_text(encoding="utf-8") + "package: Legacy\n", encoding="utf-8", newline="\n")

    def test_the_plan_names_no_file_and_the_commit_writes_the_file_the_cli_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        applied = _target_project(tmp_path / "cli", monkeypatch)
        self._hold_the_foreign_type_name(applied)
        source = _foreign_export(tmp_path)
        cli_report = component_exchange.import_component(source, str(applied / "model"))
        suffixed = Path("data_types") / "implementation" / f"{FOREIGN_TYPE}~1.yaml"
        assert [line for line in cli_report.file_suffix_lines if suffixed.as_posix() in line], (
            cli_report.file_suffix_lines
        )

        project = _target_project(tmp_path / "project", monkeypatch)
        self._hold_the_foreign_type_name(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        planned = _plan(project, source)

        assert "~1" not in planned["output"]
        assert "~1" not in json.dumps(planned["change_set"])
        assert _submit(planned["change_set"], _trace_link(), human=ScriptedHuman(True))["status"] == "committed"
        assert (project / "model" / suffixed).read_bytes() == (applied / "model" / suffixed).read_bytes()
        assert (model_element_path(project / "model", "Int16", kind="value_type")).read_bytes() == (
            model_element_path(applied / "model", "Int16", kind="value_type")
        ).read_bytes()


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _two_physical_exports(root: Path) -> tuple[str, str]:
    """Two components of one project naming one mapping set, each speaking its own physical type.

    Each export carries the set narrowed to its own closure under the set's one
    name, so the second one imported brings a map the target's set lacks.
    """
    model = root / "physical_model"
    for data_type in (
        {"name": "u8b", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        {"name": "u8", "kind": "value_type", "base": "u8b"},
        {"name": "Rate_T", "kind": "application_type", "unit": "rpm"},
        {"name": "Level_T", "kind": "application_type", "unit": "deg"},
    ):
        _dump_yaml(model_element_path(model, data_type["name"], kind=data_type["kind"]), data_type)
    _dump_yaml(
        model_element_path(model, "SharedMaps", layer="mapping_sets"),
        {
            "name": "SharedMaps",
            "maps": [
                {"application": "Rate_T", "implementation": "u8"},
                {"application": "Level_T", "implementation": "u8"},
            ],
        },
    )
    for swc, interface, element, data_type in (
        ("RateSwc", "IRate", "rate", "Rate_T"),
        ("LevelSwc", "ILevel", "level", "Level_T"),
    ):
        _dump_yaml(
            model / "interfaces" / f"{interface}.yaml",
            {
                "name": interface,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, "application_type": data_type}],
            },
        )
        _dump_yaml(
            model / "swc" / "body" / f"{swc}.yaml",
            {
                "name": swc,
                "type": "application",
                "ports": [{"name": f"p{element}", "direction": "provide", "interface": interface}],
                "data_type_mappings": ["SharedMaps"],
                "handle_termination_and_restart": "no_support",
            },
        )
    exports = []
    for swc in ("RateSwc", "LevelSwc"):
        export = root / f"{swc}.arxml"
        component_exchange.export_component(str(model), swc, str(export))
        exports.append(str(export))
    return exports[0], exports[1]


class TestASharedMappingSetIsExtendedThroughTheBoundary:
    """A multi-component update on the MCP path.

    The second component brings a map the target's set lacks; the plan edits the existing set, and the commit leaves
    the set the CLI import writes, byte for byte.
    """

    def test_the_second_component_commits_and_the_set_is_the_clis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rate, level = _two_physical_exports(tmp_path)
        applied = _target_project(tmp_path / "cli", monkeypatch)
        component_exchange.import_component(rate, str(applied / "model"))
        cli_report = component_exchange.import_component(level, str(applied / "model"))
        assert cli_report.extended_mapping_sets == {"SharedMaps": ["Level_T -> u8"]}

        project = _target_project(tmp_path / "project", monkeypatch)
        component_exchange.import_component(rate, str(project / "model"))
        writer.add_trace_link(str(project / "model"), "REQ_001", "RateSwc")
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        planned = _plan(project, level)

        assert "EXTENDED 1 mapping set(s)" in planned["output"]
        assert "SharedMaps: adds Level_T -> u8" in planned["output"]
        touched = [op["address"] for op in planned["change_set"]["operations"]]
        assert any(address["layer"] == "mapping_sets" for address in touched), touched
        response = _submit(planned["change_set"], _trace_link(swc="LevelSwc"), human=ScriptedHuman(True))
        assert response["status"] == "committed", response
        relative = Path("data_types") / "mapping_sets" / "SharedMaps.yaml"
        assert (project / "model" / relative).read_bytes() == (applied / "model" / relative).read_bytes()


def _element_naming_export(root: Path) -> str:
    """A component whose physical type names a unit, a linear compu method and a data constraint.

    The type states none of the three inline, so the export's closure carries the three elements and the import
    plans one create per element.
    """
    model = root / "element_model"
    for document, where in (
        ({"name": "ProbeRevsPerMinute", "display_name": "rpm"}, {"layer": "units"}),
        (
            {"name": "ProbeRateScale", "kind": "linear", "unit": "ProbeRevsPerMinute", "factor": 4.0},
            {"layer": "compu_methods"},
        ),
        ({"name": "ProbeRateLimits", "min": 0.0, "max": 8000.0}, {"layer": "data_constraints"}),
        (
            {"name": "u8b", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
            {"kind": "base_type"},
        ),
        ({"name": "u8", "kind": "value_type", "base": "u8b"}, {"kind": "value_type"}),
        (
            {
                "name": "ProbeRate_T",
                "kind": "application_type",
                "unit_ref": "ProbeRevsPerMinute",
                "compu_method_ref": "ProbeRateScale",
                "data_constr_ref": "ProbeRateLimits",
            },
            {"kind": "application_type"},
        ),
        (
            {"name": "ProbeRateMaps", "maps": [{"application": "ProbeRate_T", "implementation": "u8"}]},
            {"layer": "mapping_sets"},
        ),
    ):
        _dump_yaml(model_element_path(model, document["name"], **where), document)
    _dump_yaml(
        model / "interfaces" / "IProbeRate.yaml",
        {
            "name": "IProbeRate",
            "kind": "sender_receiver",
            "data_elements": [{"name": "rate", "application_type": "ProbeRate_T"}],
        },
    )
    _dump_yaml(
        model / "swc" / "body" / "ProbeRateSwc.yaml",
        {
            "name": "ProbeRateSwc",
            "type": "application",
            "ports": [{"name": "pRate", "direction": "provide", "interface": "IProbeRate"}],
            "data_type_mappings": ["ProbeRateMaps"],
            "handle_termination_and_restart": "no_support",
        },
    )
    export = root / "ProbeRateSwc.arxml"
    component_exchange.export_component(str(model), "ProbeRateSwc", str(export))
    return str(export)


class TestTheDataDescriptionElementsTravelThroughTheDraft:
    """The data description elements on the MCP import path.

    The draft creates each element in its own layer, addressed by layer and name; it commits, and each element's file
    is the one the CLI import writes, byte for byte. What it does not reach: a texttable on this path and a shared
    element that diverges (both in `test_component_exchange_tools.py`).
    """

    ELEMENTS = (
        ("units", "ProbeRevsPerMinute"),
        ("compu_methods", "ProbeRateScale"),
        ("data_constraints", "ProbeRateLimits"),
    )

    def test_the_draft_creates_each_element_in_its_layer_and_commits_the_clis_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _element_naming_export(tmp_path)
        applied = _target_project(tmp_path / "cli", monkeypatch)
        component_exchange.import_component(source, str(applied / "model"))

        project = _target_project(tmp_path / "project", monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]

        created = {
            (op["address"]["layer"], op["address"]["element"])
            for op in draft["operations"]
            if op["operation"] == "create"
        }
        assert set(self.ELEMENTS) <= created, created
        for operation in draft["operations"]:
            assert set(operation["address"]) == {"layer", "element"}, operation["address"]
        response = _submit(draft, _trace_link(swc="ProbeRateSwc"), human=ScriptedHuman(True))
        assert response["status"] == "committed", response

        for layer, name in self.ELEMENTS:
            relative = model_element_path(applied / "model", name, layer=layer).relative_to(applied / "model")
            assert (project / "model" / relative).read_bytes() == (applied / "model" / relative).read_bytes(), name
        rate = call("read", address={"layer": "application_types", "element": "ProbeRate_T"}).structured["value"]
        assert (rate["unit_ref"], rate["compu_method_ref"], rate["data_constr_ref"]) == (
            "ProbeRevsPerMinute",
            "ProbeRateScale",
            "ProbeRateLimits",
        )


# ---------------------------------------------------------------------------
# A withheld behaviour travels through the same boundary
# ---------------------------------------------------------------------------


def _reading_export(root: Path) -> str:
    """A foreign component reading `celsius` through `rTemp`, exported, then with `ITemp` and every com-spec cut.

    The file names `/PortInterfaces/ITemp` and declares it nowhere, as when the receiving project is meant to hold
    the interface, and states no com-spec, so the read alone names the member.
    """
    model = root / "reading_model"
    model.mkdir(parents=True, exist_ok=True)
    source = str(model)
    writer.create_data_type(
        source, f"{FOREIGN_TYPE}_base", "base_type", size=16, encoding="NONE", native_declaration="int16_t"
    )
    writer.create_data_type(source, FOREIGN_TYPE, "value_type", base=f"{FOREIGN_TYPE}_base")
    writer.create_interface(source, FOREIGN_INTERFACE, [{"name": "celsius", "implementation_type": FOREIGN_TYPE}])
    _dump_yaml(
        model / "swc" / "body" / f"{FOREIGN_SWC}.yaml",
        {
            "name": FOREIGN_SWC,
            "type": "application",
            "ports": [{"name": "rTemp", "direction": "require", "interface": FOREIGN_INTERFACE}],
            "runnables": [
                {
                    "name": "Run",
                    "events": [{"type": "timing", "period": 0.01}],
                    "reads": [{"port": "rTemp", "element": "celsius"}],
                }
            ],
            "handle_termination_and_restart": "no_support",
        },
    )
    export = root / f"{FOREIGN_SWC}_reading.arxml"
    component_exchange.export_component(source, FOREIGN_SWC, str(export))
    text = export.read_text(encoding="utf-8")
    text, cut = re.subn(
        rf"\s*<SENDER-RECEIVER-INTERFACE>\s*<SHORT-NAME>{FOREIGN_INTERFACE}</SHORT-NAME>.*?</SENDER-RECEIVER-INTERFACE>",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    assert cut == 1
    text = re.sub(r"\s*<(REQUIRED|PROVIDED)-COM-SPECS>.*?</\1-COM-SPECS>", "", text, flags=re.DOTALL)
    export.write_text(text, encoding="utf-8", newline="\n")
    return str(export)


class TestAWithheldBehaviourCommitsThroughTheBoundary:
    """A verdict withholds `runnables` (and an unresolved mapping set); the rest is a draft pass D accepts."""

    def test_the_withheld_create_commits_and_the_reply_names_no_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        # The target holds the interface the file names, without the member the read names.
        writer.create_interface(
            str(project / "model"), FOREIGN_INTERFACE, [{"name": "kelvin", "implementation_type": "float32"}]
        )
        source = _reading_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        planned = _plan(project, source)

        output = planned["output"]
        lines = output.splitlines()
        heading = next(line for line in lines if line.startswith("References the project does not have (1;"))
        verdict = lines[lines.index(heading) + 1]
        assert verdict == (
            f"  - swc '{FOREIGN_SWC}' runnable 'Run' read through port 'rTemp' names the data element 'celsius', "
            f"which the interfaces element '{FOREIGN_INTERFACE}' does not declare"
        )
        # The source is the client's own argument; nothing else in the reply names a location.
        assert str(tmp_path) not in output.replace(source, "")
        assert str(tmp_path) not in json.dumps(planned["change_set"])

        response = _submit(planned["change_set"], _trace_link(), human=ScriptedHuman(True))

        assert response["status"] == "committed", response
        written = yaml.safe_load(
            (project / "model" / "swc" / "components" / f"{FOREIGN_SWC}.yaml").read_text(encoding="utf-8")
        )
        assert written["ports"] == [{"name": "rTemp", "direction": "require", "interface": FOREIGN_INTERFACE}]
        assert written.get("runnables", []) == []
        assert written.get("data_type_mappings", []) == []
        assert written["handle_termination_and_restart"] == "no_support"


class TestAStubbedPortCommitsThroughTheBoundary:
    """A port on a path neither the file nor the project answers is kept on a minted stub; pass D accepts it."""

    def test_the_stub_and_the_withheld_create_commit_and_the_reply_names_no_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        # Outside the writer's root packages, where a stub is admitted.
        export = Path(_reading_export(tmp_path))
        export.write_text(
            export.read_text(encoding="utf-8").replace(
                f"/PortInterfaces/{FOREIGN_INTERFACE}", f"/Elsewhere/{FOREIGN_INTERFACE}"
            ),
            encoding="utf-8",
            newline="\n",
        )
        source = str(export)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        planned = _plan(project, source)

        output = planned["output"]
        stub = stub_name(FOREIGN_INTERFACE, 0, {"ISpeed"})[0]
        assert any(line.startswith("References the project does not have (1;") for line in output.splitlines())
        assert str(tmp_path) not in output.replace(source, "")
        assert str(tmp_path) not in json.dumps(planned["change_set"])

        response = _submit(planned["change_set"], _trace_link(), human=ScriptedHuman(True))

        assert response["status"] == "committed", response
        model = project / "model"
        written = yaml.safe_load((model / "swc" / "components" / f"{FOREIGN_SWC}.yaml").read_text(encoding="utf-8"))
        assert written["ports"] == [{"name": "rTemp", "direction": "require", "interface": stub}]
        assert written.get("runnables", []) == []
        interface = yaml.safe_load((model / "interfaces" / f"{stub}.yaml").read_text(encoding="utf-8"))
        assert interface["kind"] == "external_interface"
        assert interface["external_ref"] == f"/Elsewhere/{FOREIGN_INTERFACE}"


def _own_export_with_a_set_nobody_declares(project: Path) -> str:
    """`SpeedSensor`'s own export naming the mapping set `/Elsewhere/Gone`: a verdict, so its behaviour is withheld."""
    export = Path(_own_export(project))
    text = export.read_text(encoding="utf-8")
    assert text.count("<HANDLE-TERMINATION-AND-RESTART>") == 1
    text = text.replace(
        "<HANDLE-TERMINATION-AND-RESTART>",
        '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Gone'
        "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
    )
    export.write_text(text, encoding="utf-8", newline="\n")
    return str(export)


def _edit_speed_sensor(project: Path, edit: Any) -> None:
    path = project / "model" / "swc" / "sensors" / "SpeedSensor.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    _dump_yaml(path, data)


class TestAWithheldUpdateTravelsThroughTheBoundary:
    """A withheld update through `change_set`.

    The project's behaviour is kept under the file's descriptive part, or refused.
    """

    def test_the_overlay_commits_and_keeps_the_projects_runnables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        # The file states a description the project's component does not: a descriptive difference.
        _edit_speed_sensor(project, lambda data: data.update(description="Measures the vehicle speed."))
        source = _own_export_with_a_set_nobody_declares(project)
        sample = {
            "name": "Sample",
            "events": [{"type": "timing", "period": 0.01}],
            "writes": [{"port": "pSpeed", "element": "speed"}],
        }

        def edit(data: dict[str, Any]) -> None:
            data.pop("description")
            data["runnables"] = [sample]

        _edit_speed_sensor(project, edit)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        planned = _plan(project, source, update=True)

        assert "References the project does not have (1;" in planned["output"]
        assert str(tmp_path) not in planned["output"].replace(source, "")
        assert str(tmp_path) not in json.dumps(planned["change_set"])
        response = _submit(planned["change_set"], human=ScriptedHuman(True))

        assert response["status"] == "committed", response
        written = yaml.safe_load(
            (project / "model" / "swc" / "sensors" / "SpeedSensor.yaml").read_text(encoding="utf-8")
        )
        assert written["description"] == "Measures the vehicle speed."
        assert written["runnables"] == [
            {"name": "Sample", "events": [{"type": "timing", "period": 0.01}], "writes": [sample["writes"][0]]}
        ]

    def test_an_overlay_breaking_a_kept_reference_is_refused_and_names_no_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _own_export_with_a_set_nobody_declares(project)

        # After the export: a port the file does not carry, which a kept runnable writes through.
        def edit(data: dict[str, Any]) -> None:
            data["ports"].append({"name": "pOld", "direction": "provide", "interface": "ISpeed"})
            data["runnables"] = [
                {
                    "name": "Sample",
                    "events": [{"type": "timing", "period": 0.01}],
                    "writes": [{"port": "pOld", "element": "speed"}],
                }
            ]

        _edit_speed_sensor(project, edit)

        result = server.import_component(source=source, baseline=_baseline(), update=True)

        assert result["success"] is False
        assert result["change_set"] is None
        messages = " ".join(error["message"] for error in result["errors"])
        assert "keeps the project's own behaviour" in messages
        assert "pOld" in messages
        assert str(tmp_path) not in json.dumps(result).replace(source, "")


# ---------------------------------------------------------------------------
# The boundaries the submission — and only the submission — applies
# ---------------------------------------------------------------------------


class TestTheSubmissionIsWhereEveryBoundaryApplies:
    def test_the_default_stage_only_mode_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, None)
        draft = _plan(project, source)["change_set"]
        before = state(project)

        response = _submit(draft, _trace_link())

        assert response["status"] == "staged"
        assert state(project) == before

    def test_a_declined_review_changes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]
        before = state(project)

        response = _submit(draft, _trace_link(), human=ScriptedHuman(DECLINE))

        assert response["status"] == "abandoned"
        assert state(project) == before

    def test_an_uncovered_derived_operation_is_refused_by_gate_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]
        # The SWC's create covers its trace link member; the derived interface and data type are not approved.
        set_scope(
            monkeypatch,
            [{"operation": "create", "address": {"layer": "swc", "element": FOREIGN_SWC}}],
        )
        before = state(project)
        human = ScriptedHuman(True)

        response = _submit(draft, _trace_link(), human=human)

        assert response["error"] == "unauthorized-delta"
        assert FOREIGN_INTERFACE in json.dumps(response)
        assert human.questions == []
        assert state(project) == before

    def test_an_uncovered_companion_operation_is_refused_by_gate_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]
        set_scope(
            monkeypatch,
            [
                {"operation": "create", "address": {"layer": "swc", "element": FOREIGN_SWC}},
                {"operation": "create", "address": {"layer": "interfaces", "element": FOREIGN_INTERFACE}},
                {"operation": "create", "address": {"layer": "implementation_types", "element": FOREIGN_TYPE}},
            ],
        )
        before = state(project)
        # The SWC's create covers the trace link; the uncovered companion is the requirement's revision.
        revision = {
            "operation": "update",
            "address": {"layer": "requirements", "element": "REQ_001", "path": [{"field": "version"}]},
            "value": 2,
        }

        response = _submit(draft, _trace_link(), revision, human=ScriptedHuman(True))

        assert response["error"] == "unauthorized-delta"
        assert "REQ_001" in json.dumps(response)
        assert state(project) == before

    def test_the_six_lifecycle_records_share_one_change_set_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _target_project(tmp_path / "project", monkeypatch)
        source = _foreign_export(tmp_path)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        draft = _plan(project, source)["change_set"]

        assert _submit(draft, _trace_link(), human=ScriptedHuman(True))["status"] == "committed"

        lifecycle = [entry for entry in audit_records(project) if entry["operation"] in LIFECYCLE_OPERATIONS]
        assert {entry["args"]["change_set_id"] for entry in lifecycle} == {lifecycle[0]["args"]["change_set_id"]}
        assert {entry["operation"] for entry in lifecycle} == {
            "change_set_staged",
            "change_set_approved",
            "change_set_commit_intent",
            "change_set_committed",
        }


# ---------------------------------------------------------------------------
# The structural boundary: this tool cannot mutate, whatever its body says
# ---------------------------------------------------------------------------


class TestTheSingleMutationSurfaceBoundary:
    """`import_component` plans; no name in its body may make it a writer.

    A behavioural test shows only that today's body does not write; this one keeps a future body from starting, so
    there stays one mutation surface. The forbidden names are the ways to grow a second one: driving the engine
    (`handle_request`), writing model files (the applying entry point) and recording a write nobody staged
    (`AuditLog`).
    """

    @staticmethod
    def _body() -> ast.FunctionDef:
        source = inspect.getsource(server.import_component)
        tree = ast.parse(source.lstrip())
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef)
        return function

    @staticmethod
    def _names(function: ast.FunctionDef) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    @pytest.mark.parametrize(
        "forbidden",
        ["handle_request", "AuditLog", "log_write", "import_component", "_atomic_apply"],
    )
    def test_the_tool_body_names_no_mutation_entry_point(self, forbidden: str) -> None:
        # `component_exchange.import_component` is the applying entry point; the planning one is
        # `plan_component_import`.
        assert forbidden not in self._names(self._body())

    def test_the_tool_body_plans_and_derives(self) -> None:
        names = self._names(self._body())
        assert "plan_component_import" in names
        assert "derive_change_set" in names
