"""The functional harness itself: the loader's refusals, each expectation term, the steps, the fixtures.

- **Each expectation term** holds and fails; a failure names the scenario, the variant, the
  expectation and, for a threshold, its ``because``.
- **The loader refuses**, naming the file: a schema violation, an undeclared reference, a specimen
  the pin file does not pin, a tier on the wrong specimen, a repeated key, NaN and Infinity, and a
  scenario on the shared project with a step that writes into it; the tool sets and reviewed flags
  are held to the server's tool list and the CLI's parser.
- **The steps**: recipes by title and request index, targets filled in, a failed step failing its
  own expectations only, wire bytes against a hand-worked figure, text answers (impact summary,
  refusal), a CLI timeout, hook steps' ``stdin`` and JSON stdout.
- **A continued page**: an unusable cursor and a replay answering old rows or none fail, and the
  paged scenarios fail under mutations of the real answers.
- **The fixtures** end to end on the ``example-minimal`` specimen, targets held to an independent
  reading of the copied YAML; **the tiers** and the large bench's pinned digest; **the export
  faults**; and :class:`ScenarioLocator` on hand-written models.

Does not reach the scenarios' own expectations, nor whether the importer reads a fault correctly
(the export-fault scenario pins that).
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import inspect
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml
from conftest import copy_specimen, specimen_generator_module, specimen_passport
from protocol_client import tool_names
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.copilot_guard import CODEX_HOOK_FILE
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.guide_reference.cli import WRITES_NOTHING, WRITES_ONLY_WITH_REPORT
from swcraft_tools.passport import Locator, compute_passport

from tests.functional import conftest as functional_conftest
from tests.functional import harness
from tests.functional import test_scenarios as scenarios_module
from tests.functional.conftest import TEMPLATE_CODEX_HOOK, dev_assignment, large_bench_project_at
from tests.functional.export_faults import FaultError, add_conflicts, write_faulted_export
from tests.functional.harness import (
    FunctionalProject,
    Run,
    ScenarioError,
    StepFailed,
    StepResult,
    call_tool,
    count_at,
    evaluate,
    finding_pairs,
    impact_summary_document,
    load_scenario,
    parse_scenario,
    recipe_request,
    refusal_document,
    report_merged,
    report_referrers,
    resolve_targets,
    run_cli,
    run_steps,
    select,
)
from tests.functional.scenario_locator import ScenarioLocator

#: The specimen of the passport contract's ``examples/minimal.json``, and its source passport.
MINIMAL_SPECIMEN = "example-minimal"
MINIMAL = specimen_passport(MINIMAL_SPECIMEN)

#: The specimen whose every sender-receiver port is on a connector (no hub, no spare).
TWO_ROWS_SPECIMEN = "harness-two-rows"

#: The specimen the metrics scenarios read: many connectors over fewer distinct dependencies.
METRICS_SPECIMEN = "scenario-impact-summary-bounded"

PAGE_BECAUSE = "the recipes' page (test only)"
SPILL_BECAUSE = "the host's spill figure (test only)"


def _document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": "probe",
        "function": "MCP `read`",
        "tier": "generated-small",
        "class": "normal",
        "specimen": MINIMAL_SPECIMEN,
        "target": {
            "component": "largest_component",
            "components": "largest_components",
            "interface": "heaviest_interface",
        },
        "thresholds": {
            "page_rows": {"value": 2, "because": PAGE_BECAUSE},
            "host_spill_bytes": {"value": 100, "because": SPILL_BECAUSE},
        },
        "steps": [{"id": "ports", "mcp": "read", "arguments": {"address": {"layer": "swc"}, "form": "index"}}],
        "expectations": [{"step": "ports", "at": "value.identities", "rows_at_most": "page_rows"}],
    }
    document.update(changes)
    return document


def _large_document(**changes: Any) -> dict[str, Any]:
    """:func:`_document` on the large bench: the tier and the specimen set."""
    return _document(tier="large-bench", specimen="large-bench", **changes)


def _yaml_documents(model_root: Path, layer_directory: str) -> list[dict[str, Any]]:
    """Every element under *model_root* / *layer_directory*, read by ``yaml.safe_load``, not the kernel.

    The independent reading a fixture's figure is held to, in sorted POSIX order.
    """
    paths = sorted((model_root / layer_directory).rglob("*.yaml"), key=lambda path: path.as_posix())
    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]


def _yaml_components(model_root: Path) -> dict[str, dict[str, Any]]:
    """The model's components by name, read independently (:func:`_yaml_documents`)."""
    return {document["name"]: document for document in _yaml_documents(model_root, "swc")}


def _parse(document: dict[str, Any]) -> Any:
    return parse_scenario(json.dumps(document), source="probe.json")


class _Locator:
    """A stand-in locator: two components, one interface named by both ports."""

    def largest_component(self) -> str:
        return "Big"

    def largest_components(self) -> tuple[str, ...]:
        return ("Big", "Small")

    def heaviest_interface(self) -> str:
        return "IShared"

    def port_count(self, component: str) -> int:
        return {"Big": 3, "Small": 1}[component]

    def connector_count(self, component: str) -> int:
        return {"Big": 4, "Small": 1}.get(component, 0)

    def unconnected_ports(self, component: str) -> tuple[str, ...]:
        return ("p1", "p2") if component == "Big" else ()

    def unconnected_pairs(self) -> tuple[tuple[str, str], ...]:
        return (("Big", "p1"), ("Big", "p2"), ("Small", "q1"))

    def heaviest_sender_receiver_interfaces(self) -> tuple[str, ...]:
        return ("IShared", "IOwn")

    def most_unconnected_component(self) -> str:
        return "Big"

    def ports_naming(self, interface: str) -> tuple[tuple[str, str], ...]:
        return {"IShared": (("Big", "pOut"), ("Small", "rIn")), "IOwn": (("Big", "pOwn"),)}.get(interface, ())

    def most_read_sender_receiver_interface(self) -> str:
        return "IShared"

    def most_read_data_element(self) -> str:
        return "de0"

    def commonest_level(self) -> str:
        return "QM"

    def readers_count(self, interface: str) -> int:
        return {"IShared": 3, "IOwn": 1}[interface]

    def declared_level_count(self, level: str) -> int:
        return {"QM": 5}.get(level, 0)

    def sender_receiver_rows(self) -> int:
        return 7

    def threshold_gap_memory(self) -> str:
        return "RAM"

    def threshold_gap_bytes(self) -> int:
        return 7066

    def over_size_bytes(self) -> int:
        return 12801

    def declared_resources(self) -> tuple[str, ...]:
        return ("CORE0", "RAM0")

    def model_components(self) -> int:
        return 2

    def model_compositions(self) -> int:
        return 1

    def model_component_ports(self) -> int:
        return 4

    def model_connectors(self) -> int:
        return 5

    def model_runnables(self) -> int:
        return 3

    def model_dependencies(self) -> int:
        return 1


#: A target naming the first two sender-receiver interfaces (the stand-in's IShared and IOwn).
CONFLICTED = {"question": "heaviest_sender_receiver_interfaces", "first": 2}


def _run(expectation: dict[str, Any], result: StepResult, *, variants: dict[str, Any] | None = None) -> Run:
    steps = [{"id": "s", "cli": ["check-model"]}]
    changes: dict[str, Any] = {"steps": steps, "expectations": [expectation]}
    changes["target"] = {
        **_document()["target"],
        "conflicted": CONFLICTED,
        "level": "commonest_level",
        "resources": "declared_resources",
    }
    if variants:
        changes["variants"] = variants
    scenario = _parse(_document(**changes))
    project = FunctionalProject(
        root=Path("."),
        manifest=Path("swcraft.yaml"),
        passport={"components": {"unconnected": {"total": 3, "max_on_one": 2}}},
        locator=_Locator(),
    )
    run = Run(scenario, scenario.variant_names()[0], project, resolve_targets(scenario, _Locator()))
    run.results["s"] = result
    return run


def _build_report(
    *,
    resources: tuple[str, ...] = ("CORE0", "RAM0"),
    arxml: str = "No ARXML element changed against `dist/model_old.arxml`.",
    spans: bool = True,
) -> str:
    """A build report written by hand in the shape ``build_report.render_markdown`` gives (never its output).

    *resources* are the Resources table's first cells, each set in a code span unless *spans* is false;
    an empty tuple writes the section's no-resource sentence instead of a table.
    """
    rows = [
        f"| {f'`{name}`' if spans else name} | core | 0.00% | 70.00% | 70.00% | 100.00% | within budget |"
        for name in resources
    ]
    table = (
        [
            "| Resource | Kind | Used | Limit | Headroom | Physical headroom | Status |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *rows,
        ]
        if resources
        else ["The model declares no core and no memory."]
    )
    lines = [
        "# Build Report",
        "",
        "functional-scenario · AUTOSAR 4.3.0",
        "",
        "## Findings",
        "",
        "The model check found no warning.",
        "",
        "## Resources",
        "",
        *table,
        "",
        "## ARXML changes",
        "",
        *([arxml, ""] if arxml else []),
        "---",
        "",
        "Generated by `swcraft validate --report`.",
    ]
    return "\n".join(lines) + "\n"


def _failure(run: Run) -> str:
    with pytest.raises(AssertionError) as caught:
        evaluate(run)
    return str(caught.value)


def _findings(*pairs: tuple[str, str, str | None]) -> str:
    findings = []
    for rule, element, member in pairs:
        path = [{"field": "ports"}, {"member": member}, {"field": "interface"}] if member else []
        findings.append({"rule": rule, "subjects": [{"layer": "swc", "element": element, "path": path}]})
    return json.dumps({"schemaVersion": 1, "findings": findings})


def _report(*unresolved: tuple[str, list[str]], merged: tuple[str, ...] = ()) -> str:
    """An import report with the *unresolved* references and the *merged* declarations.

    Each unresolved entry is (interface, [Component/port]); *merged* names interfaces.
    """
    lines = ["Import report: export.arxml", "", "CONFLICTING 2 duplicate name group(s)", ""]
    if merged:
        lines.append(f"Merged declarations ({len(merged)}; one name declared more than once and identical):")
        for k, interface in enumerate(merged):
            lines.append(
                f"  - interfaces/SENDER-RECEIVER-INTERFACE/{interface} declared at /PortInterfaces/{interface}, "
                f"/WlConflict{k}/{interface} -- kept as one element"
            )
            lines.append(f"      description not carried, from /WlConflict{k}/{interface}: A second declaration.")
        lines.append("")
    if unresolved:
        lines.append(f"Unresolved references ({len(unresolved)} target(s); check-model reports every one):")
        for interface, referrers in unresolved:
            lines.append(
                f"  - /PortInterfaces/{interface} (DEST SENDER-RECEIVER-INTERFACE) was declared more than once"
                f" -- see the conflicting declaration; {len(referrers)} referring element(s):"
            )
            lines.extend(
                f"      /ComponentTypes/{referrer} (P-PORT-PROTOTYPE PROVIDED-INTERFACE-TREF -> swc.port.interface)"
                for referrer in referrers
            )
        lines.append("")
    lines.append("Skipped elements, one line per reason (--verbose lists each element):")
    lines.append("  - 2 × SWC-IMPLEMENTATION: SWC-IMPLEMENTATION has no SWCraft representation")
    return "\n".join(lines) + "\n"


#: An export-fault step over the stand-in's two sender-receiver interfaces.
FAULT_STEP: dict[str, Any] = {
    "id": "faults",
    "export_faults": {
        "interfaces": {"$target": "conflicted"},
        "kind": "value",
        "source": "dist/model.arxml",
        "output": "faulted.arxml",
    },
}

#: The same step naming a component, which no export declares as an interface.
NOT_AN_INTERFACE = {**FAULT_STEP["export_faults"], "interfaces": {"$target": "component"}}

#: A small export: two sender-receiver interfaces (IShared with a description of its own), a
#: client-server one and two data types, laid out as the writer lays an export out.
ARXML = """\
<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>DataTypes</SHORT-NAME>
      <ELEMENTS>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>TAlpha</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
        </IMPLEMENTATION-DATA-TYPE>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>TBeta</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
        </IMPLEMENTATION-DATA-TYPE>
      </ELEMENTS>
    </AR-PACKAGE>
    <AR-PACKAGE>
      <SHORT-NAME>PortInterfaces</SHORT-NAME>
      <ELEMENTS>
        <CLIENT-SERVER-INTERFACE>
          <SHORT-NAME>IService</SHORT-NAME>
        </CLIENT-SERVER-INTERFACE>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>IShared</SHORT-NAME>
          <DESC>
            <L-2 L="EN">The shared signal.</L-2>
          </DESC>
          <IS-SERVICE>false</IS-SERVICE>
          <DATA-ELEMENTS>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>de0</SHORT-NAME>
              <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/TAlpha</TYPE-TREF>
            </VARIABLE-DATA-PROTOTYPE>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>de1</SHORT-NAME>
              <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/TAlpha</TYPE-TREF>
            </VARIABLE-DATA-PROTOTYPE>
          </DATA-ELEMENTS>
        </SENDER-RECEIVER-INTERFACE>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>IOwn</SHORT-NAME>
          <IS-SERVICE>false</IS-SERVICE>
          <DATA-ELEMENTS>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>de0</SHORT-NAME>
              <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/TBeta</TYPE-TREF>
            </VARIABLE-DATA-PROTOTYPE>
          </DATA-ELEMENTS>
        </SENDER-RECEIVER-INTERFACE>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

_NS = {"ar": "http://autosar.org/schema/r4.0"}


def _declarations(arxml: str, name: str) -> list[tuple[str, ElementTree.Element]]:
    """``(the declaring package's name, the interface element)`` for each declaration of *name*."""
    root = ElementTree.fromstring(arxml.encode("utf-8"))
    found = []
    for package in root.iterfind(".//ar:AR-PACKAGE", _NS):
        for interface in package.iterfind("ar:ELEMENTS/ar:SENDER-RECEIVER-INTERFACE", _NS):
            if interface.findtext("ar:SHORT-NAME", namespaces=_NS) == name:
                found.append((package.findtext("ar:SHORT-NAME", namespaces=_NS), interface))
    return found


def _canonical(element: ElementTree.Element) -> bytes:
    element = copy.deepcopy(element)
    element.tail = None
    return ElementTree.tostring(element)


class TestTheExportFaults:
    @pytest.mark.parametrize("names", [["IShared"], ["IShared", "IOwn"]])
    def test_a_value_fault_adds_one_declaration_per_interface_differing_in_the_type_alone(
        self, names: list[str]
    ) -> None:
        faulted = add_conflicts(ARXML, names, "value")
        for k, name in enumerate(names):
            (first_package, first), (second_package, second) = _declarations(faulted, name)
            assert (first_package, second_package) == ("PortInterfaces", f"WlConflict{k}")
            first_ref = first.find(".//ar:TYPE-TREF", _NS)
            second_ref = second.find(".//ar:TYPE-TREF", _NS)
            assert second_ref.text != first_ref.text
            # The type it now names is one the export declares.
            assert second_ref.text in {"/DataTypes/TAlpha", "/DataTypes/TBeta"}
            second_ref.text, second_ref.attrib = first_ref.text, dict(first_ref.attrib)
            assert _canonical(second) == _canonical(first)
        assert len(_declarations(faulted, "IOwn")) == (2 if "IOwn" in names else 1)

    @pytest.mark.parametrize("name", ["IShared", "IOwn"])
    def test_a_description_fault_differs_in_the_description_alone(self, name: str) -> None:
        # IShared declares a description, which the fault changes; IOwn declares none, and gains one.
        faulted = add_conflicts(ARXML, [name], "description")
        (_, first), (_, second) = _declarations(faulted, name)
        first_desc = first.find("ar:DESC", _NS)
        second_desc = second.find("ar:DESC", _NS)
        assert second_desc is not None
        assert first_desc is None or ElementTree.tostring(first_desc) != ElementTree.tostring(second_desc)
        # The DESC sits where the standard orders it: right after the SHORT-NAME.
        assert [child.tag.split("}")[1] for child in second][:2] == ["SHORT-NAME", "DESC"]
        second.remove(second_desc)
        if first_desc is not None:
            first.remove(first_desc)
        assert _canonical(second) == _canonical(first)

    @pytest.mark.parametrize("kind", ["value", "description"])
    def test_the_export_outside_the_added_packages_is_untouched(self, kind: str) -> None:
        faulted = add_conflicts(ARXML, ["IShared", "IOwn"], kind)
        start = faulted.index("    <AR-PACKAGE>\n      <SHORT-NAME>WlConflict0</SHORT-NAME>")
        end = faulted.index("  </AR-PACKAGES>")
        assert faulted[:start] + faulted[end:] == ARXML
        assert add_conflicts(ARXML, [], kind) == ARXML

    def test_the_source_file_is_never_written(self, tmp_path: Path) -> None:
        source, output = tmp_path / "model.arxml", tmp_path / "faulted.arxml"
        source.write_text(ARXML, encoding="utf-8", newline="\n")
        before = source.read_bytes()
        write_faulted_export(source, output, ["IOwn"], "value")
        assert source.read_bytes() == before
        assert output.read_bytes() == add_conflicts(ARXML, ["IOwn"], "value").encode("utf-8")

    @pytest.mark.parametrize(
        ("names", "kind", "message"),
        [
            (["IService"], "value", "no sender-receiver interface is named 'IService'"),
            (["IShared", "IShared"], "value", "'IShared' is named twice"),
            (["IShared"], "type", "no fault kind 'type'"),
        ],
        ids=["client-server", "twice", "kind"],
    )
    def test_a_fault_it_cannot_make_is_refused(self, names: list[str], kind: str, message: str) -> None:
        with pytest.raises(FaultError, match=message):
            add_conflicts(ARXML, names, kind)


class TestTheTerms:
    def test_rows_at_most(self) -> None:
        expectation = {"step": "s", "at": "rows", "rows_at_most": "page_rows"}
        evaluate(_run(expectation, StepResult(body={"rows": [1, 2]})))
        text = _failure(_run(expectation, StepResult(body={"rows": [1, 2, 3]})))
        assert "scenario probe [default]" in text
        assert '"rows_at_most": "page_rows"' in text
        assert "3 at 'rows', expected <= page_rows = 2" in text
        assert PAGE_BECAUSE in text

    def test_bytes_at_most(self) -> None:
        expectation = {"step": "s", "bytes_at_most": "host_spill_bytes"}
        evaluate(_run(expectation, StepResult(texts=("x" * 98,))))
        text = _failure(_run(expectation, StepResult(texts=("x" * 50, "y" * 50))))
        assert "104 wire bytes, expected <= host_spill_bytes = 100" in text
        assert SPILL_BECAUSE in text

    def test_count_above(self) -> None:
        expectation = {"step": "s", "at": "count", "count_above": "page_rows"}
        evaluate(_run(expectation, StepResult(body={"count": 3})))
        text = _failure(_run(expectation, StepResult(body={"count": 2})))
        assert "2 at 'count', expected > page_rows = 2" in text
        assert PAGE_BECAUSE in text

    def test_count_equals_a_passport_leaf(self) -> None:
        expectation = {"step": "s", "at": "rows", "count_equals": {"passport": "components.unconnected.max_on_one"}}
        evaluate(_run(expectation, StepResult(body={"rows": [1, 2]})))
        text = _failure(_run(expectation, StepResult(body={"rows": [1, 2, 3]})))
        assert "3 at 'rows', expected passport components.unconnected.max_on_one = 2" in text

    @pytest.mark.parametrize(
        ("figure", "holds", "fails"),
        [
            ("port_count", 3, 2),
            ("unconnected_count", 2, 3),
            ("connector_count", 4, 3),
            ("ports_naming_count", 2, 1),
            ("components_naming_count", 2, 1),
            ("readers_count", 3, 2),
            ("declared_level_count", 5, 4),
        ],
    )
    def test_count_equals_a_locator_figure(self, figure: str, holds: int, fails: int) -> None:
        of = {"readers_count": "interface", "declared_level_count": "level"}.get(
            figure, "interface" if figure.endswith("naming_count") else "component"
        )
        expectation = {"step": "s", "at": "count", "count_equals": {"locator": figure, "of": of}}
        evaluate(_run(expectation, StepResult(body={"count": holds})))
        text = _failure(_run(expectation, StepResult(body={"count": fails})))
        assert f"{fails} at 'count', expected locator {figure} of {of} = {holds}" in text

    def test_count_equals_the_models_sender_receiver_rows(self) -> None:
        # The one figure of the whole model: it names no target, and the failure says so.
        expectation = {"step": "s", "at": "count", "count_equals": {"locator": "sender_receiver_rows"}}
        evaluate(_run(expectation, StepResult(body={"count": 7})))
        text = _failure(_run(expectation, StepResult(body={"count": 6})))
        assert "6 at 'count', expected locator sender_receiver_rows of the model = 7" in text

    @pytest.mark.parametrize(
        ("figure", "holds"),
        [
            ("model_components", 2),
            ("model_compositions", 1),
            ("model_component_ports", 4),
            ("model_connectors", 5),
            ("model_runnables", 3),
            ("model_dependencies", 1),
        ],
    )
    def test_count_equals_a_figure_of_the_whole_model(self, figure: str, holds: int) -> None:
        # The architecture metrics' figures name no target, as the sender-receiver rows do.
        assert figure in harness.MODEL_FIGURES
        expectation = {"step": "s", "at": "count", "count_equals": {"locator": figure}}
        evaluate(_run(expectation, StepResult(body={"count": holds})))
        text = _failure(_run(expectation, StepResult(body={"count": holds + 1})))
        assert f"{holds + 1} at 'count', expected locator {figure} of the model = {holds}" in text

    def test_a_model_figure_the_model_refuses_is_a_failure_naming_why(self) -> None:
        class _Refusing(_Locator):
            def model_dependencies(self) -> int:
                raise ValueError("connector K has an end on a composition")

        run = _run(
            {"step": "s", "at": "count", "count_equals": {"locator": "model_dependencies"}},
            StepResult(body={"count": 1}),
        )
        run.project = dataclasses.replace(run.project, locator=_Refusing())
        assert "locator model_dependencies of the model: connector K has an end on a composition" in _failure(run)

    def test_readers_count_of_a_name_that_is_no_interface_is_a_failure(self) -> None:
        expectation = {"step": "s", "at": "count", "count_equals": {"locator": "readers_count", "of": "component"}}
        text = _failure(_run(expectation, StepResult(body={"count": 3})))
        assert "locator readers_count of component: the model has no sender-receiver interface 'Big'" in text

    @pytest.mark.parametrize(
        "source",
        [
            {"locator": "sender_receiver_rows", "of": "component"},
            {"locator": "readers_count"},
            {"locator": "no_such_figure", "of": "component"},
        ],
        ids=["model-figure-with-a-target", "element-figure-without-one", "unknown-figure"],
    )
    def test_a_locator_figure_of_the_wrong_shape_is_refused_by_the_schema(self, source: dict[str, str]) -> None:
        document = _document(expectations=[{"step": "ports", "at": "count", "count_equals": source}])
        with pytest.raises(ScenarioError, match="refused by the scenario schema"):
            _parse(document)

    @pytest.mark.parametrize(
        "expectation",
        [
            {"step": "ports", "at": "fits", "literal_equals": 3, "because": "a number is a threshold's"},
            {"step": "ports", "at": "fits", "literal_equals": None, "because": "null is no literal"},
            {"step": "ports", "at": "fits", "literal_equals": True},
        ],
        ids=["number", "null", "no-because"],
    )
    def test_a_literal_that_is_a_number_or_lacks_its_because_is_refused(self, expectation: dict[str, Any]) -> None:
        with pytest.raises(ScenarioError, match="refused by the scenario schema"):
            _parse(_document(expectations=[expectation]))

    def test_components_naming_count_counts_each_component_once(self) -> None:
        class TwoPortsOnBig(_Locator):
            def ports_naming(self, interface: str) -> tuple[tuple[str, str], ...]:
                return (("Big", "pOut"), ("Big", "pOut2"), ("Small", "rIn"))

        expectation = {
            "step": "s",
            "at": "count",
            "count_equals": {"locator": "components_naming_count", "of": "interface"},
        }
        run = _run(expectation, StepResult(body={"count": 2}))
        run.project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, TwoPortsOnBig())
        evaluate(run)
        run.results["s"] = StepResult(body={"count": 3})
        assert "3 at 'count', expected locator components_naming_count of interface = 2" in _failure(run)

    def test_values_equal_a_targets_names_in_order(self) -> None:
        expectation = {"step": "s", "at": "results.*.first", "values_equal": {"target": "components"}}
        evaluate(_run(expectation, StepResult(body={"results": [{"first": "Big"}, {"first": "Small"}]})))
        text = _failure(_run(expectation, StepResult(body={"results": [{"first": "Small"}, {"first": "Big"}]})))
        assert "['Small', 'Big'] at 'results.*.first', expected target components = ['Big', 'Small']" in text
        text = _failure(_run(expectation, StepResult(body={"results": [{"first": "Big"}]})))
        assert "['Big'] at 'results.*.first', expected target components = ['Big', 'Small']" in text

    def test_values_equal_a_target_naming_one_element(self) -> None:
        expectation = {"step": "s", "at": "first", "values_equal": {"target": "component"}}
        evaluate(_run(expectation, StepResult(body={"first": "Big"})))
        text = _failure(_run(expectation, StepResult(body={"first": "Small"})))
        assert "['Small'] at 'first', expected target component = ['Big']" in text

    def test_count_equals_a_threshold(self) -> None:
        # An exact page: fewer rows than the threshold fail as more do, so an empty answer cannot pass.
        expectation = {"step": "s", "at": "rows", "count_equals": {"threshold": "page_rows"}}
        evaluate(_run(expectation, StepResult(body={"rows": [1, 2]})))
        text = _failure(_run(expectation, StepResult(body={"rows": []})))
        assert f"0 at 'rows', expected page_rows = 2 (because: {PAGE_BECAUSE})" in text

    def test_count_equals_another_steps_count(self) -> None:
        expectation = {"step": "s", "at": "page.total", "count_equals": {"step": "count", "at": "count"}}
        steps = [{"id": "count", "cli": ["check-model"]}, {"id": "s", "cli": ["check-model"]}]
        scenario = _parse(_document(steps=steps, expectations=[expectation]))
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())

        def run(total: int, count: StepResult) -> Run:
            return Run(
                scenario, "default", project, {}, {"count": count, "s": StepResult(body={"page": {"total": total}})}
            )

        evaluate(run(9, StepResult(body={"count": 9})))
        assert "8 at 'page.total', expected step count at 'count' = 9" in _failure(
            run(8, StepResult(body={"count": 9}))
        )
        assert "step 'count' failed: refused" in _failure(run(9, StepResult(failure="refused")))

    def test_rows_among_a_components_unconnected_ports(self) -> None:
        expectation = {"step": "s", "at": "rows", "rows_among": {"locator": "unconnected_ports", "of": "component"}}

        def rows(*pairs: tuple[str, str]) -> StepResult:
            return StepResult(body={"rows": [{"element": element, "member": member} for element, member in pairs]})

        evaluate(_run(expectation, rows(("Big", "p2"), ("Big", "p1"))))
        assert "1 row(s) at 'rows' appear more than once, first 'Big/p1'" in _failure(
            _run(expectation, rows(("Big", "p1"), ("Big", "p1")))
        )
        # A port of another component -- an unconnected one of the model, even -- is not the target's.
        assert "1 of 2 rows at 'rows' are not among locator unconnected_ports of component, first 'Small/q1'" in (
            _failure(_run(expectation, rows(("Big", "p1"), ("Small", "q1"))))
        )
        assert "a row at 'rows' names no element and member: 'p1'" in _failure(
            _run(expectation, StepResult(body={"rows": ["p1"]}))
        )
        # An empty page holds every membership vacuously, so the term refuses it.
        assert "no rows at 'rows', expected rows among locator unconnected_ports of component" in _failure(
            _run(expectation, rows())
        )

    def test_rows_among_the_models_unconnected_ports(self) -> None:
        expectation = {"step": "s", "at": "rows", "rows_among": {"locator": "unconnected_ports"}}
        both = [{"element": "Big", "member": "p1"}, {"element": "Small", "member": "q1"}]
        evaluate(_run(expectation, StepResult(body={"rows": both})))
        connected = [*both, {"element": "Big", "member": "pOut"}]
        assert "1 of 3 rows at 'rows' are not among locator unconnected_ports of the model, first 'Big/pOut'" in (
            _failure(_run(expectation, StepResult(body={"rows": connected})))
        )

    def test_present(self) -> None:
        expectation = {"step": "s", "at": "page.next", "present": True}
        evaluate(_run(expectation, StepResult(body={"page": {"next": {"element": "A"}}})))
        assert "null at 'page.next', expected a value" in _failure(
            _run(expectation, StepResult(body={"page": {"next": None}}))
        )
        assert "the answer has nothing at 'page.next'" in _failure(_run(expectation, StepResult(body={"page": {}})))

    @pytest.mark.parametrize("literal", [True, False, "fits physically"], ids=["true", "false", "string"])
    def test_literal_equals(self, literal: bool | str) -> None:
        expectation = {"step": "s", "at": "verdicts.*.fits", "literal_equals": literal, "because": "probe (test only)"}
        evaluate(_run(expectation, StepResult(body={"verdicts": [{"fits": literal}, {"fits": literal}]})))
        other = "other" if isinstance(literal, str) else not literal
        text = _failure(_run(expectation, StepResult(body={"verdicts": [{"fits": literal}, {"fits": other}]})))
        assert (
            f"{json.dumps([literal, other])} at 'verdicts.*.fits', expected {json.dumps(literal)} "
            "(because: probe (test only))"
        ) in text

    @pytest.mark.parametrize(("literal", "value"), [(True, 1), (False, 0), (False, None), ("1", 1)])
    def test_literal_equals_holds_the_literals_type(self, literal: bool | str, value: Any) -> None:
        # `True == 1` in Python, and a null verdict is not a "no": each is a failure, not a match.
        expectation = {"step": "s", "at": "fits", "literal_equals": literal, "because": "probe (test only)"}
        assert f"expected {json.dumps(literal)}" in _failure(_run(expectation, StepResult(body={"fits": value})))

    def test_absent(self) -> None:
        expectation = {"step": "s", "at": "hookSpecificOutput", "absent": True}
        evaluate(_run(expectation, StepResult(body={})))
        evaluate(_run(expectation, StepResult(body={"other": 1})))
        deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
        assert "{'permissionDecision': 'deny'} at 'hookSpecificOutput', expected nothing there" in _failure(
            _run(expectation, StepResult(body=deny))
        )
        # A null there is a value at the path: absent is select's miss, not present's null.
        assert "None at 'hookSpecificOutput', expected nothing there" in _failure(
            _run(expectation, StepResult(body={"hookSpecificOutput": None}))
        )

    def test_absent_over_no_body_fails(self) -> None:
        expectation = {"step": "s", "at": "hookSpecificOutput", "absent": True}
        assert "the step answered no body to look in for 'hookSpecificOutput'" in _failure(
            _run(expectation, StepResult(exit_code=0, stdout="{}"))
        )

    def test_exit_code(self) -> None:
        expectation = {"step": "s", "exit_code": 0}
        evaluate(_run(expectation, StepResult(exit_code=0)))
        text = _failure(_run(expectation, StepResult(exit_code=1, stderr="refused")))
        assert "exit code 1, expected 0" in text and "refused" in text

    def test_findings_exactly(self) -> None:
        rule = "port.undeclared-interface"
        expectation = {
            "step": "s",
            "findings_exactly": {"pairs": [{"rule": rule, "ports_naming": "interface"}]},
        }
        held = _findings((rule, "Big", "pOut"), (rule, "Small", "rIn"))
        evaluate(_run(expectation, StepResult(stdout=held)))
        text = _failure(
            _run(expectation, StepResult(stdout=_findings((rule, "Big", "pOut"), ("other.rule", "Big", None))))
        )
        assert f"missing [('{rule}', 'Small/rIn')]" in text
        assert "extra [('other.rule', 'Big')]" in text

    def test_findings_exactly_less_another_variant(self) -> None:
        rule = "port.undeclared-interface"
        expectation = {
            "step": "s",
            "findings_exactly": {"pairs": [{"rule": rule, "ports_naming": "interface"}], "minus_variant": "normal"},
        }
        variants = {
            "normal": {"expectations": [{"step": "s", "exit_code": 0}]},
            "conflict": {"expectations": [expectation]},
        }
        baseline = _findings(("model.baseline", "Big", None))
        run = _run(expectation, StepResult(stdout=baseline), variants=variants)
        run = Run(run.scenario, "conflict", run.project, run.targets, run.results)
        normal = Run(run.scenario, "normal", run.project, run.targets, {"s": StepResult(stdout=baseline)})
        run.results["s"] = StepResult(
            stdout=_findings(("model.baseline", "Big", None), (rule, "Big", "pOut"), (rule, "Small", "rIn"))
        )
        run.other_variant = lambda name: normal
        evaluate(run)
        run.results["s"] = StepResult(stdout=_findings((rule, "Big", "pOut")))
        assert "missing [('port.undeclared-interface', 'Small/rIn')]" in _failure(run)

    def test_findings_exactly_over_a_target_naming_several_interfaces(self) -> None:
        rule = "port.undeclared-interface"
        expectation = {"step": "s", "findings_exactly": {"pairs": [{"rule": rule, "ports_naming": "conflicted"}]}}
        held = _findings((rule, "Big", "pOut"), (rule, "Small", "rIn"), (rule, "Big", "pOwn"))
        evaluate(_run(expectation, StepResult(stdout=held)))
        text = _failure(_run(expectation, StepResult(stdout=_findings((rule, "Big", "pOut"), (rule, "Small", "rIn")))))
        assert f"missing [('{rule}', 'Big/pOwn')]" in text

    def test_report_referrers_exactly(self) -> None:
        expectation = {"step": "s", "report_referrers_exactly": {"ports_naming": "conflicted"}}
        held = _report(("IShared", ["Big/pOut", "Small/rIn"]), ("IOwn", ["Big/pOwn"]))
        evaluate(_run(expectation, StepResult(stdout=held)))
        text = _failure(_run(expectation, StepResult(stdout=_report(("IShared", ["Big/pOut", "Other/pX"])))))
        assert "missing ['Big/pOwn', 'Small/rIn']" in text
        assert "extra ['Other/pX']" in text
        assert "the import report's unresolved references" in text

    def test_report_merged_exactly(self) -> None:
        expectation = {"step": "s", "report_merged_exactly": {"interfaces": "conflicted"}}
        evaluate(_run(expectation, StepResult(stdout=_report(merged=("IShared", "IOwn")))))
        text = _failure(_run(expectation, StepResult(stdout=_report(merged=("IShared",)))))
        assert "the import report's merged declarations name ['IShared']" in text
        assert "missing ['IOwn'], extra []" in text
        # A report that merged nothing -- the importer ignoring the second declarations -- fails too.
        assert "missing ['IOwn', 'IShared']" in _failure(_run(expectation, StepResult(stdout=_report())))

    @staticmethod
    def _on_file(expectation: dict[str, Any], root: Path, text: str | None) -> Run:
        """:func:`_run` over a project at *root* whose ``dist/build-report.md`` is *text* (absent when ``None``)."""
        if text is not None:
            (root / "dist").mkdir(parents=True, exist_ok=True)
            (root / "dist" / "build-report.md").write_text(text, encoding="utf-8", newline="\n")
        run = _run(expectation, StepResult(exit_code=0))
        run.project = dataclasses.replace(run.project, root=root)
        return run

    _FIRST_LINE = {
        "step": "s",
        "file": "dist/build-report.md",
        "heading": "ARXML changes",
        "report_first_line": "No ARXML element changed against `dist/model_old.arxml`.",
        "because": "probe (test only)",
    }

    def test_report_first_line(self, tmp_path: Path) -> None:
        evaluate(self._on_file(self._FIRST_LINE, tmp_path, _build_report()))
        moved = _build_report(arxml="No ARXML element moved against `dist/model_old.arxml`.")
        text = _failure(self._on_file(self._FIRST_LINE, tmp_path, moved))
        assert (
            "dist/build-report.md under '## ARXML changes': the first line is "
            "'No ARXML element moved against `dist/model_old.arxml`.', expected "
            "'No ARXML element changed against `dist/model_old.arxml`.' (because: probe (test only))"
        ) in text

    def test_report_first_line_reads_past_the_blank_line_and_stops_at_the_section(self, tmp_path: Path) -> None:
        # The section's first paragraph follows a blank line (the import report's rule would find
        # none); a section with no line of its own does not borrow the next section's or the footer's.
        expectation = {
            **self._FIRST_LINE,
            "heading": "Findings",
            "report_first_line": "The model check found no warning.",
        }
        evaluate(self._on_file(expectation, tmp_path, _build_report()))
        empty = _build_report(arxml="")
        text = _failure(self._on_file(self._FIRST_LINE, tmp_path, empty))
        assert "the first line is None" in text

    def test_report_first_line_of_an_absent_file_or_heading_fails(self, tmp_path: Path) -> None:
        text = _failure(self._on_file(self._FIRST_LINE, tmp_path, None))
        assert "dist/build-report.md cannot be read: " in text
        # A heading is matched whole: "## ARXML" is no "## ARXML changes".
        text = _failure(self._on_file({**self._FIRST_LINE, "heading": "ARXML"}, tmp_path, _build_report()))
        assert "dist/build-report.md under '## ARXML': no such heading" in text

    _NAMES = {
        "step": "s",
        "file": "dist/build-report.md",
        "heading": "Resources",
        "report_names_exactly": {"$target": "resources"},
        "because": "probe (test only)",
    }

    def test_report_names_exactly(self, tmp_path: Path) -> None:
        evaluate(self._on_file(self._NAMES, tmp_path, _build_report(resources=("RAM0", "CORE0"))))
        many = _failure(self._on_file(self._NAMES, tmp_path, _build_report(resources=("CORE0", "RAM0", "RAM1"))))
        assert "missing [], extra ['RAM1']" in many
        missing = _failure(self._on_file(self._NAMES, tmp_path, _build_report(resources=("CORE0",))))
        assert (
            "dist/build-report.md under '## Resources': the table names ['CORE0'], expected exactly target "
            "resources's ['CORE0', 'RAM0'] (missing ['RAM0'], extra []; because: probe (test only))"
        ) in missing

    def test_report_names_exactly_reads_a_double_backtick_span_and_an_escaped_pipe(self, tmp_path: Path) -> None:
        class Odd(_Locator):
            def declared_resources(self) -> tuple[str, ...]:
                return ("A`B", "C|D")

        run = self._on_file(self._NAMES, tmp_path, _build_report(resources=("`` A`B ``", "`C\\|D`"), spans=False))
        run.targets["resources"] = Odd().declared_resources()
        evaluate(run)

    def test_report_names_exactly_reads_every_fence_code_span_writes(self, tmp_path: Path) -> None:
        # The writer's own spans, not hand-written ones: a run of two backticks takes a three-backtick fence.
        from swcraft_tools.markdown_cells import code_span

        names = ("A``B", "C```D", "E`F", "G|H")

        class Odd(_Locator):
            def declared_resources(self) -> tuple[str, ...]:
                return names

        run = self._on_file(
            self._NAMES, tmp_path, _build_report(resources=tuple(code_span(name) for name in names), spans=False)
        )
        run.targets["resources"] = Odd().declared_resources()
        evaluate(run)

    @pytest.mark.parametrize("delimiter", ["|  | --- |", "| --- | x |", "| --- | --- ", "--- | --- |"])
    def test_report_names_exactly_refuses_a_malformed_delimiter_row(self, tmp_path: Path, delimiter: str) -> None:
        report = _build_report().replace("| --- | --- | --- | --- | --- | --- | --- |", delimiter)
        text = _failure(self._on_file(self._NAMES, tmp_path, report))
        assert "a table with no header and delimiter row" in text

    def test_report_names_exactly_over_a_section_with_no_table_fails(self, tmp_path: Path) -> None:
        text = _failure(self._on_file(self._NAMES, tmp_path, _build_report(resources=())))
        assert "dist/build-report.md under '## Resources': no table" in text
        plain = _failure(self._on_file(self._NAMES, tmp_path, _build_report(resources=("CORE0",), spans=False)))
        assert "a first cell that is no code span: 'CORE0'" in plain

    def test_report_names_exactly_of_an_empty_target_holds_over_a_section_with_no_table(self, tmp_path: Path) -> None:
        run = self._on_file(self._NAMES, tmp_path, _build_report(resources=()))
        run.targets["resources"] = ()
        evaluate(run)
        run = self._on_file(self._NAMES, tmp_path, _build_report(resources=("CORE0",)))
        run.targets["resources"] = ()
        assert "missing [], extra ['CORE0']" in _failure(run)

    def test_report_names_exactly_fails_on_a_name_listed_twice(self, tmp_path: Path) -> None:
        text = _failure(self._on_file(self._NAMES, tmp_path, _build_report(resources=("CORE0", "RAM0", "RAM0"))))
        assert "dist/build-report.md under '## Resources': the table names ['RAM0'] more than once" in text

    def test_report_names_exactly_of_an_absent_file_or_heading_fails(self, tmp_path: Path) -> None:
        assert "cannot be read" in _failure(self._on_file(self._NAMES, tmp_path, None))
        text = _failure(self._on_file({**self._NAMES, "heading": "Memories"}, tmp_path, _build_report()))
        assert "dist/build-report.md under '## Memories': no such heading" in text

    @pytest.mark.parametrize(
        "changes",
        [{"file": "../outside.md"}, {"file": "/abs/report.md"}, {"file": "dist/./report.md"}, {"because": None}],
        ids=["parent", "absolute", "dot-segment", "no-because"],
    )
    def test_a_report_expectation_outside_the_project_or_without_its_because_is_refused(
        self, changes: dict[str, Any]
    ) -> None:
        expectation = {**self._NAMES, "step": "ports", **changes}
        expectation = {key: value for key, value in expectation.items() if value is not None}
        with pytest.raises(ScenarioError, match="refused by the scenario schema"):
            _parse(_document(target={"resources": "declared_resources"}, expectations=[expectation]))

    def test_findings_exactly_less_a_variant_whose_step_failed_names_that_failure(self) -> None:
        rule = "port.undeclared-interface"
        expectation = {
            "step": "s",
            "findings_exactly": {"pairs": [{"rule": rule, "ports_naming": "interface"}], "minus_variant": "normal"},
        }
        variants = {"normal": {}, "conflict": {"expectations": [expectation]}}
        run = _run(
            expectation, StepResult(stdout=_findings((rule, "Big", "pOut"), (rule, "Small", "rIn"))), variants=variants
        )
        run = Run(run.scenario, "conflict", run.project, run.targets, run.results)
        normal = Run(run.scenario, "normal", run.project, run.targets, {"s": StepResult(failure="the export failed")})
        run.other_variant = lambda name: normal
        assert "variant normal's step 's' failed: the export failed" in _failure(run)

    @pytest.mark.parametrize(
        ("expectation", "result", "message"),
        [
            (
                {"step": "s", "findings_exactly": {"pairs": []}},
                StepResult(stdout='{"schemaVersion": 1}'),
                "the step's output has no findings list",
            ),
            (
                {"step": "s", "at": "count", "count_equals": {"passport": "components.unconnected.*"}},
                StepResult(body={"count": 2}),
                "the passport has 2 values at 'components.unconnected.*', not one",
            ),
            (
                {"step": "s", "at": "count", "count_equals": {"passport": "components.unconnected"}},
                StepResult(body={"count": 2}),
                "the passport's value at 'components.unconnected' is not a count",
            ),
        ],
        ids=["no-findings", "passport-path-of-several-values", "passport-path-of-a-section"],
    )
    def test_a_malformed_answer_is_a_failure_in_the_report_not_a_crash(
        self, expectation: dict[str, Any], result: StepResult, message: str
    ) -> None:
        assert message in _failure(_run(expectation, result))

    def test_a_locator_figure_for_a_component_the_model_lacks_is_a_failure(self) -> None:
        run = _run(
            {"step": "s", "at": "count", "count_equals": {"locator": "port_count", "of": "component"}},
            StepResult(body={"count": 1}),
        )
        run.targets["component"] = "Gone"
        assert "locator port_count of component: the model has no component 'Gone'" in _failure(run)

    def test_every_failure_is_reported_not_only_the_first(self) -> None:
        scenario = _parse(
            _document(
                steps=[{"id": "s", "cli": ["check-model"]}],
                expectations=[
                    {"step": "s", "exit_code": 0},
                    {"step": "s", "bytes_at_most": "host_spill_bytes"},
                    {"step": "s", "at": "missing", "rows_at_most": "page_rows"},
                ],
            )
        )
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        run = Run(scenario, "default", project, {}, {"s": StepResult(exit_code=2, texts=("z" * 200,))})
        text = _failure(run)
        assert "3 expectation(s) failed" in text
        assert "the answer has nothing at 'missing'" in text


class TestTheLoader:
    def test_a_well_formed_scenario_loads(self) -> None:
        scenario = _parse(_document())
        assert scenario.id == "probe"
        assert scenario.variant_names() == ("default",)
        assert scenario.thresholds["page_rows"].because == PAGE_BECAUSE

    @pytest.mark.parametrize(
        ("changes", "where"),
        [
            ({"expectations": [{"step": "ports", "at": "value", "rows_below": "page_rows"}]}, "expectations/0"),
            ({"thresholds": {"page_rows": {"value": 20}}}, "thresholds/page_rows"),
            ({"tier": "real-export"}, "tier"),
            ({"specimen": None}, "specimen"),
            ({"expectations": [{"step": "ports", "at": "value", "rows_at_most": 20}]}, "expectations/0"),
            ({"specimen": "Example Minimal"}, "specimen"),
            (
                {"steps": [{"id": "ports", "mcp": "impact_of", "arguments": {}, "text": "impact-full"}]},
                "steps/0",
            ),
            ({"steps": [{"id": "ports", "recipe": "Tasks that run on a core", "request": -2}]}, "steps/0"),
            ({"expectations": [{"step": "ports", "at": "value", "values_equal": ["Big"]}]}, "expectations/0"),
            ({"target": {"conflicted": {"question": "heaviest_sender_receiver_interfaces", "first": 0}}}, "target"),
            ({"target": {"conflicted": {"question": "largest_component", "first": 1}}}, "target"),
            (
                {"steps": [{"id": "ports", "export_faults": {**FAULT_STEP["export_faults"], "kind": "type"}}]},
                "steps/0",
            ),
            (
                {
                    "steps": [
                        {"id": "ports", "export_faults": {**FAULT_STEP["export_faults"], "output": "../out.arxml"}}
                    ]
                },
                "steps/0",
            ),
            ({"expectations": [{"step": "ports", "report_referrers_exactly": ["Big/pOut"]}]}, "expectations/0"),
            ({"steps": [{"id": "ports", "cli": ["guard"], "stdin": '{"tool_name": "Bash"}'}]}, "steps/0"),
            ({"expectations": [{"step": "ports", "at": "hookSpecificOutput", "absent": False}]}, "expectations/0"),
        ],
        ids=[
            "unknown-term",
            "threshold-without-because",
            "tier",
            "specimen-not-a-string",
            "numeric-literal-threshold",
            "specimen-not-a-name",
            "unknown-text-reading",
            "request-below-minus-one",
            "values-equal-a-literal",
            "target-first-zero",
            "target-first-of-a-single-name",
            "fault-kind",
            "fault-output-outside-the-project",
            "report-referrers-a-literal",
            "stdin-not-an-object",
            "absent-false",
        ],
    )
    def test_the_schema_refuses(self, changes: dict[str, Any], where: str) -> None:
        with pytest.raises(ScenarioError, match=rf"^probe\.json: refused by the scenario schema at {where}"):
            _parse(_document(**changes))

    @pytest.mark.parametrize(
        ("tier", "specimen", "where"),
        [
            ("generated-small", None, r"\(root\)"),
            ("large-bench", MINIMAL_SPECIMEN, "specimen"),
            ("generated-small", "large-bench", "specimen"),
        ],
        ids=["without-specimen", "large-on-another-specimen", "small-on-the-large-bench"],
    )
    def test_the_tier_decides_the_specimen(self, tier: str, specimen: str | None, where: str) -> None:
        document = _document(tier=tier, specimen=specimen)
        if specimen is None:
            del document["specimen"]
        with pytest.raises(ScenarioError, match=rf"^probe\.json: refused by the scenario schema at {where}"):
            _parse(document)

    def test_a_configuration_is_refused(self) -> None:
        # The specimen's committed passport is the model's shape; a scenario carries none of its own.
        with pytest.raises(ScenarioError, match=r"^probe\.json: refused by the scenario schema at \(root\)"):
            _parse(_document(configuration=copy.deepcopy(MINIMAL)))

    def test_a_specimen_the_pin_file_does_not_pin_is_refused(self) -> None:
        with pytest.raises(
            ScenarioError, match=r"^probe\.json: specimen 'no-such-specimen' is not a key of the pin file$"
        ):
            _parse(_document(specimen="no-such-specimen"))

    def test_the_reasons_are_the_pinned_because(self) -> None:
        pinned = specimen_generator_module().load_pins(harness.REPO_ROOT)["specimens"]
        scenario = _parse(_document(specimen="scenario-level-list-paged"))
        assert scenario.because == pinned["scenario-level-list-paged"]["because"]
        assert "layers.swc" in scenario.because
        assert _parse(_document()).because == pinned[MINIMAL_SPECIMEN].get("because", {})

    def test_a_pinned_because_off_the_passport_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned = {**harness.pinned_specimens(), MINIMAL_SPECIMEN: {"because": {"identifiers.mean_length": "a reason"}}}
        monkeypatch.setattr(harness, "pinned_specimens", lambda: pinned)
        with pytest.raises(
            ScenarioError,
            match=r"^probe\.json: specimen 'example-minimal': because 'identifiers\.mean_length' is not a leaf "
            r"of its passport$",
        ):
            _parse(_document())

    def test_a_recipe_step_may_name_its_last_request(self) -> None:
        document = _document(steps=[{"id": "ports", "recipe": "Ports that nothing connects", "request": -1}])
        assert _parse(document).steps("default")[0]["request"] == -1

    def test_a_repeated_key_is_refused(self) -> None:
        text = json.dumps(_document())[:-1] + ', "id": "again"}'
        with pytest.raises(ScenarioError, match=r"^probe\.json: a repeated key: id$"):
            parse_scenario(text, source="probe.json")

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_nan_and_infinity_are_refused(self, constant: str) -> None:
        text = json.dumps(_document()).replace('"value": 2,', f'"value": {constant},')
        with pytest.raises(ScenarioError, match=rf"^probe\.json: {constant} is not a JSON number$"):
            parse_scenario(text, source="probe.json")

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            (
                {"expectations": [{"step": "ports", "at": "value", "rows_at_most": "undeclared"}]},
                "threshold 'undeclared' is not declared",
            ),
            (
                {"steps": [{"id": "ports", "mcp": "read", "arguments": {"address": {"$target": "nobody"}}}]},
                "target 'nobody' is not declared",
            ),
            ({"expectations": [{"step": "other", "exit_code": 0}]}, "names step 'other', not a step"),
            (
                {
                    "steps": [{"id": "a", "cli": ["x"]}, {"id": "a", "cli": ["y"]}],
                    "expectations": [{"step": "a", "exit_code": 0}],
                },
                "a step id twice: a",
            ),
            (
                {"expectations": [{"step": "ports", "findings_exactly": {"pairs": [], "minus_variant": "normal"}}]},
                "minus_variant 'normal' is not a variant",
            ),
            (
                {
                    "expectations": [
                        {"step": "ports", "at": "value", "count_equals": {"locator": "port_count", "of": "x"}}
                    ]
                },
                "target 'x' is not declared",
            ),
            (
                {"expectations": [{"step": "ports", "at": "value", "values_equal": {"target": "nobody"}}]},
                "values_equal: target 'nobody' is not declared",
            ),
            (
                {
                    "expectations": [
                        {"step": "ports", "at": "value", "rows_among": {"locator": "unconnected_ports", "of": "x"}}
                    ]
                },
                "rows_among: target 'x' is not declared",
            ),
            (
                {"steps": [FAULT_STEP], "expectations": [{"step": "faults", "exit_code": 0}]},
                "step faults: target 'conflicted' is not declared",
            ),
            (
                {
                    "expectations": [
                        {"step": "ports", "report_referrers_exactly": {"ports_naming": "nobody"}},
                    ]
                },
                "report_referrers_exactly: target 'nobody' is not declared",
            ),
            (
                {"expectations": [{"step": "ports", "at": "value", "count_equals": {"threshold": "undeclared"}}]},
                "count_equals: threshold 'undeclared' is not declared",
            ),
            (
                {"expectations": [{"step": "ports", "at": "value", "count_equals": {"step": "other", "at": "count"}}]},
                "count_equals names step 'other', not a step",
            ),
            (
                {"expectations": [{"step": "ports", "report_merged_exactly": {"interfaces": "nobody"}}]},
                "report_merged_exactly: target 'nobody' is not declared",
            ),
            (
                {
                    "expectations": [
                        {
                            "step": "ports",
                            "file": "dist/build-report.md",
                            "heading": "Resources",
                            "report_names_exactly": {"$target": "nobody"},
                            "because": "probe (test only)",
                        }
                    ]
                },
                "report_names_exactly: target 'nobody' is not declared",
            ),
            (
                {
                    "variants": {
                        "normal": {
                            "steps": [{"id": "other", "cli": ["check-model"]}],
                            "expectations": [{"step": "other", "exit_code": 0}],
                        },
                        "conflict": {
                            "expectations": [
                                {"step": "ports", "findings_exactly": {"pairs": [], "minus_variant": "normal"}}
                            ]
                        },
                    }
                },
                "minus_variant 'normal' has no step 'ports'",
            ),
            (
                {
                    "steps": [{"id": "ports", "cli": ["generate", "arxml"]}],
                    "expectations": [{"step": "ports", "exit_code": 0}],
                },
                r"step ports: it writes into the project \(swcraft generate\), and the scenario's project is the "
                "shared one",
            ),
            (
                {
                    "steps": [{"id": "ports", "mcp": "change_set", "arguments": {}}],
                    "expectations": [{"step": "ports", "exit_code": 0}],
                },
                r"step ports: it writes into the project \(tool change_set\)",
            ),
            (
                {
                    "steps": [{"id": "ports", "mcp": "validate", "arguments": {}}],
                    "expectations": [{"step": "ports", "exit_code": 0}],
                },
                r"step ports: it writes into the project \(tool validate\)",
            ),
            (
                {
                    "steps": [{"id": "ports", "cli": ["list", "--project", "{manifest}", "--output=out.txt"]}],
                    "expectations": [{"step": "ports", "exit_code": 0}],
                },
                r"step ports: it writes into the project \(swcraft --output=out.txt\)",
            ),
            (
                {
                    "target": {"conflicted": CONFLICTED},
                    "steps": [FAULT_STEP],
                    "expectations": [{"step": "faults", "exit_code": 0}],
                },
                "step faults: it writes into the project",
            ),
            (
                {
                    "steps": [
                        {"id": "a", "mcp": "read", "arguments": {}, "continues": "b"},
                        {"id": "b", "mcp": "read", "arguments": {}},
                    ],
                    "expectations": [{"step": "a", "exit_code": 0}],
                },
                "step a continues 'b', not an earlier step",
            ),
            (
                {"expectations": [{"step": "ports", "at": "value", "disjoint_from": "other"}]},
                "disjoint_from names step 'other', not a step",
            ),
        ],
        ids=[
            "threshold",
            "target",
            "step",
            "step-twice",
            "variant",
            "count-target",
            "values-target",
            "rows-among-target",
            "fault-target",
            "report-target",
            "count-threshold",
            "count-step",
            "merged-target",
            "names-target",
            "minus-variant-step",
            "shared-writing-command",
            "shared-writing-tool",
            "shared-validate",
            "shared-unreviewed-option",
            "shared-export-fault",
            "continues-a-later-step",
            "disjoint-from-no-step",
        ],
    )
    def test_an_undeclared_reference_is_refused(self, changes: dict[str, Any], message: str) -> None:
        with pytest.raises(ScenarioError, match=r"^probe\.json: .*" + message):
            _parse(_document(**changes))

    def test_a_recipe_judged_by_the_tool_its_request_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No recipe calls a writing tool today; one that did is refused on the shared project.
        @dataclass(frozen=True)
        class _Recipe:
            title: str
            tool: str
            requests: tuple[dict[str, Any], ...]

        recipes = {"Commit a change": _Recipe("Commit a change", "change_set", ({},))}
        monkeypatch.setattr(harness, "_recipes", lambda: recipes)
        document = _document(
            steps=[{"id": "ports", "recipe": "Commit a change", "request": 0}],
            expectations=[{"step": "ports", "exit_code": 0}],
        )
        with pytest.raises(ScenarioError, match=r"\(tool change_set \(its recipe's request\)\)"):
            _parse(document)
        assert _parse({**document, "project": "fresh"}).fresh

    def test_a_recipe_not_in_the_profile_is_left_to_its_step(self) -> None:
        # The loader cannot name its tool; running the step fails before calling anything.
        document = _document(
            steps=[{"id": "ports", "recipe": "No such recipe", "request": 0}],
            expectations=[{"step": "ports", "exit_code": 0}],
        )
        assert not _parse(document).fresh

    def test_the_tool_classification_is_the_servers_tool_list(self) -> None:
        assert not harness.READ_ONLY_TOOLS & harness.WRITING_TOOLS
        assert harness.READ_ONLY_TOOLS | harness.WRITING_TOOLS == tool_names()

    def test_the_read_only_commands_are_the_ones_the_cli_declares_writing_nothing(self) -> None:
        assert harness.READ_ONLY_COMMANDS == set(WRITES_NOTHING) | set(WRITES_ONLY_WITH_REPORT)

    def test_the_reviewed_flags_are_the_clis_options(self) -> None:
        # The options that do write a file, reviewed and left out of the flags, so `_writing_option` names them.
        writing_flags = {"metrics": {"--report"}, "resource-report": {"--report"}}
        subcommands = next(
            action for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction)
        )
        for command, flags in harness.READ_ONLY_COMMAND_FLAGS.items():
            parser = subcommands.choices[command]
            options = {
                option
                for action in parser._actions
                if not isinstance(action, argparse._HelpAction)
                for option in action.option_strings
            }
            assert options == flags | writing_flags.get(command, set()), command

    @pytest.mark.parametrize("command", ["metrics", "resource-report"])
    @pytest.mark.parametrize("option", ["--report", "--rep", "--r"])
    def test_a_writing_option_of_a_read_only_command_is_named(self, command: str, option: str) -> None:
        assert harness._writing_option([command, "--project", "p", option]) == option

    @pytest.mark.parametrize("argv", [["--since", "HEAD"], ["--since=HEAD", "--since", "v1"], ["--since-head"]])
    def test_the_metrics_base_options_write_nothing(self, argv: list[str]) -> None:
        assert harness._writing_option(["metrics", "--project", "p", *argv]) is None

    def test_the_reviewed_positionals_are_the_clis(self) -> None:
        subcommands = next(
            action for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction)
        )
        assert set(harness.READ_ONLY_COMMAND_POSITIONALS) == harness.READ_ONLY_COMMANDS
        for command, positionals in harness.READ_ONLY_COMMAND_POSITIONALS.items():
            parser = subcommands.choices[command]
            assert tuple(action.dest for action in parser._actions if not action.option_strings) == positionals, command

    @pytest.mark.parametrize("locator", [ScenarioLocator, _Locator], ids=["scenario", "stand-in"])
    def test_a_locator_answers_every_question_the_harness_asks(self, locator: type) -> None:
        asked = [name for name in vars(harness.TargetLocator) if not name.startswith("_")]
        assert asked
        assert [name for name in asked if not callable(getattr(locator, name, None))] == []

    def test_the_scenario_locator_delegates_every_question_the_passport_answers(self) -> None:
        # Every TargetLocator question the passport's Locator answers is ScenarioLocator's by
        # delegation; the rest are exactly the scenarios' own sixteen.
        asked = {name for name in vars(harness.TargetLocator) if not name.startswith("_")}
        passports = {name for name in asked if callable(getattr(Locator, name, None))}
        assert asked - passports == {
            "most_read_sender_receiver_interface",
            "most_read_data_element",
            "commonest_level",
            "readers_count",
            "declared_level_count",
            "sender_receiver_rows",
            "threshold_gap_memory",
            "threshold_gap_bytes",
            "over_size_bytes",
            "declared_resources",
            *harness.MODEL_FIGURES[1:],
        }

    def test_the_target_questions_are_the_schemas_enum(self) -> None:
        schema = json.loads(harness.SCENARIO_SCHEMA_PATH.read_text(encoding="utf-8"))
        questions = schema["properties"]["target"]["additionalProperties"]["oneOf"][0]["enum"]
        asked = {name for name in vars(harness.TargetLocator) if not name.startswith("_")}
        assert set(questions) <= asked
        for question in questions:
            assert not inspect.signature(getattr(_Locator, question)).parameters.keys() - {"self"}, question

    def test_a_large_bench_scenario_names_the_large_bench(self) -> None:
        scenario = _parse(_large_document())
        assert scenario.tier == "large-bench"
        assert scenario.specimen == "large-bench"
        assert not scenario.fresh

    def test_a_large_bench_scenario_on_a_fresh_project_is_refused(self) -> None:
        with pytest.raises(
            ScenarioError,
            match=r'^probe\.json: a large-bench scenario reads the one shared large bench, so "project": "fresh" '
            r"is refused$",
        ):
            _parse(_large_document(project="fresh"))

    def test_a_large_bench_scenario_that_writes_is_refused(self) -> None:
        document = _large_document(steps=[{"id": "ports", "cli": ["generate", "arxml"]}])
        with pytest.raises(ScenarioError, match=r"step ports: it writes into the project \(swcraft generate\)"):
            _parse(document)

    def test_a_fresh_scenario_may_write_into_its_project(self) -> None:
        document = _document(
            project="fresh",
            steps=[{"id": "ports", "cli": ["generate", "arxml"]}],
            expectations=[{"step": "ports", "exit_code": 0}],
        )
        assert _parse(document).fresh

    def test_a_question_the_model_cannot_answer_is_a_failure_naming_the_target(self) -> None:
        class _Empty(_Locator):
            def heaviest_interface(self) -> str:
                raise LookupError("no component port names an interface")

        with pytest.raises(
            StepFailed, match="^target interface: heaviest_interface: no component port names an interface$"
        ):
            resolve_targets(_parse(_document()), _Empty())

    def test_a_target_may_take_the_first_answers_of_a_question(self) -> None:
        scenario = _parse(_document(target={"conflicted": CONFLICTED, "one": {**CONFLICTED, "first": 1}}))
        assert resolve_targets(scenario, _Locator()) == {"conflicted": ("IShared", "IOwn"), "one": ("IShared",)}
        greedy = _parse(_document(target={"conflicted": {**CONFLICTED, "first": 3}}))
        with pytest.raises(StepFailed, match="target conflicted: heaviest_sender_receiver_interfaces answers 2, not 3"):
            resolve_targets(greedy, _Locator())

    def test_the_id_is_the_file_stem(self, tmp_path: Path) -> None:
        path = tmp_path / "another-name.json"
        path.write_text(json.dumps(_document()), encoding="utf-8", newline="\n")
        with pytest.raises(ScenarioError, match="id 'probe' is not the file's stem"):
            load_scenario(path)

    def test_a_variant_overrides_steps_and_expectations(self) -> None:
        other = [{"step": "ports", "exit_code": 1}]
        scenario = _parse(
            _document(
                variants={
                    "a": {"steps": [{"id": "ports", "cli": ["check-model"]}]},
                    "b": {"expectations": other},
                    "c": {},
                }
            )
        )
        assert scenario.variant_names() == ("a", "b", "c")
        assert scenario.steps("a") == [{"id": "ports", "cli": ["check-model"]}]
        assert scenario.expectations("a") == scenario.document["expectations"]
        assert scenario.expectations("b") == other
        # An empty variant is the top-level steps and expectations under its name.
        assert scenario.steps("c") == scenario.document["steps"]
        assert scenario.expectations("c") == scenario.document["expectations"]


class TestTheSteps:
    def test_select_walks_lists_and_mappings(self) -> None:
        answer = {"results": {"a": {"candidates": [1, 2]}, "b": {"candidates": [3]}}}
        assert select(answer, "results.*.candidates") == [[1, 2], [3]]
        assert count_at(answer, "results.*.candidates") == 3
        with pytest.raises(StepFailed, match="nothing at 'results.c'"):
            select(answer, "results.c")

    def test_select_takes_a_list_item_by_its_index(self) -> None:
        answer = {"results": [{"candidates": [1, 2]}, {"candidates": [3]}, {"candidates": "none"}]}
        assert select(answer, "results.*.candidates.0") == [1, 3]
        assert select(answer, "results.0.candidates.1") == [2]
        with pytest.raises(StepFailed, match=r"nothing at 'results\.\*\.candidates\.2'"):
            select(answer, "results.*.candidates.2")

    def test_a_recipe_in_neither_place(self) -> None:
        with pytest.raises(StepFailed, match=r"^recipe in neither the profile nor a How-to page: 'No such question'$"):
            recipe_request("No such question", 0, {}, (), {})

    def test_a_recipe_is_filled_from_the_targets_and_its_arguments_dropped(self) -> None:
        tool, arguments = recipe_request(
            "Tasks that run on a core", 0, {"<CORE>": "core"}, ("derived",), {"core": "CORE7"}
        )
        assert tool == "read"
        assert "derived" not in arguments
        assert arguments["where"] == [{"field": "derived.core.core", "equals": "CORE7"}]

    def test_request_minus_one_is_the_recipes_last_request(self) -> None:
        # A recipe sends its count first and its page last; -1 names the page whichever it is.
        last = recipe_request("How many components are at a level", 1, {"<LEVEL>": "level"}, (), {"level": "B"})
        assert (
            recipe_request("How many components are at a level", -1, {"<LEVEL>": "level"}, (), {"level": "B"}) == last
        )
        only = recipe_request("Elements without a description", 0, {}, (), {})
        assert recipe_request("Elements without a description", -1, {}, (), {}) == only

    def test_a_failed_step_fails_its_expectations_and_the_others_are_still_evaluated(self) -> None:
        scenario = _parse(
            _document(
                steps=[
                    {"id": "missing", "recipe": "No such question", "request": 0},
                    {"id": "ports", "mcp": "read", "arguments": {"address": {"layer": "swc"}, "form": "index"}},
                ],
                expectations=[
                    {"step": "missing", "at": "count", "count_equals": {"passport": "layers.swc"}},
                    {"step": "ports", "at": "value.identities", "rows_at_most": "page_rows"},
                ],
            )
        )
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {"layers": {"swc": 2}}, _Locator())
        run = Run(scenario, "default", project, {})
        answer = StepResult(body={"value": {"identities": [1, 2, 3]}})
        run_steps(run, tool_caller=lambda name, arguments: answer)
        text = _failure(run)
        assert "2 expectation(s) failed" in text
        assert "step 'missing' failed: recipe in neither the profile nor a How-to page: 'No such question'" in text
        assert "3 at 'value.identities', expected <= page_rows = 2" in text

    def test_a_number_target_reaches_a_nested_argument_as_it_is(self) -> None:
        target = {"memory": "threshold_gap_memory", "gap": "threshold_gap_bytes", "over": "over_size_bytes"}
        scenario = _parse(
            _document(
                target=target,
                steps=[
                    {
                        "id": "gap",
                        "mcp": "resource_report",
                        "arguments": {"fits": {"memory": {"$target": "memory"}, "bytes": {"$target": "gap"}}},
                    },
                    {"id": "over", "mcp": "resource_report", "arguments": {"fits": {"bytes": {"$target": "over"}}}},
                ],
                expectations=[{"step": "gap", "at": "fits", "literal_equals": False, "because": "probe (test only)"}],
            )
        )
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        sent: list[Any] = []

        def caller(tool: str, arguments: Mapping[str, Any]) -> StepResult:
            sent.append(arguments)
            return StepResult(body={"fits": False})

        run_steps(Run(scenario, "default", project, resolve_targets(scenario, _Locator())), tool_caller=caller)
        assert sent == [{"fits": {"memory": "RAM", "bytes": 7066}}, {"fits": {"bytes": 12801}}]
        assert all(type(arguments["fits"]["bytes"]) is int for arguments in sent)

    def test_a_number_target_names_nothing_to_values_equal(self) -> None:
        expectation = {"step": "s", "at": "first", "values_equal": {"target": "gap"}}
        steps = [{"id": "s", "cli": ["check-model"]}]
        scenario = _parse(_document(target={"gap": "threshold_gap_bytes"}, steps=steps, expectations=[expectation]))
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        run = Run(scenario, "default", project, resolve_targets(scenario, _Locator()))
        run.results["s"] = StepResult(body={"first": 7066})
        assert "the target answers the number 7066, not names" in _failure(run)

    def test_an_unfilled_placeholder_is_refused(self) -> None:
        with pytest.raises(StepFailed, match="placeholder <CORE> is not filled"):
            recipe_request("Tasks that run on a core", 0, {}, (), {})

    def test_the_wire_bytes_are_the_escaped_text_blocks_summed(self) -> None:
        # Each block as a JSON string in UTF-8: 10 characters, four quotes escaped, a two-byte é and
        # the two quotes around it are 17; six characters, an escaped newline and the quotes are 10.
        assert StepResult(texts=('{"a": "é"}', "a note\n")).wire_bytes == 27

    def test_a_cli_run_past_its_timeout_is_a_failed_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415 - patched for this test alone

        def hang(argv: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", hang)
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        with pytest.raises(StepFailed, match=r"^swcraft check-model ran longer than 60 s$"):
            run_cli(project, ["check-model"])
        # A generator argv names the command too: the argv is read once, before the run.
        with pytest.raises(StepFailed, match=r"^swcraft check-model ran longer than 60 s$"):
            run_cli(project, (arg for arg in ["check-model"]))

    @staticmethod
    def _captured_run(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[dict[str, Any]]:
        """``subprocess.run`` replaced by one that records its keywords and answers *stdout*."""
        import subprocess  # noqa: PLC0415 - patched for this test alone

        calls: list[dict[str, Any]] = []

        def answer(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append({"argv": argv, **kwargs})
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", answer)
        return calls

    def test_run_steps_hands_a_hook_steps_stdin_to_the_command_serialized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._captured_run(monkeypatch, '{"hookSpecificOutput": {"permissionDecision": "deny"}}')
        payload = {"cwd": "{project}", "tool_input": {"command": "ls {project}/x", "n": 1}, "{project}": ["{project}"]}
        scenario = _parse(
            _document(
                project="fresh",
                steps=[{"id": "hook", "cli": ["guard", "--host", "codex"], "stdin": payload}],
                expectations=[{"step": "hook", "at": "hookSpecificOutput", "present": True}],
            )
        )
        project = FunctionalProject(Path("/work/p"), Path("/work/p/swcraft.yaml"), {}, _Locator())
        run = run_steps(Run(scenario, "default", project, {}))
        root = project.root.as_posix()
        spelled = {"cwd": root, "tool_input": {"command": f"ls {root}/x", "n": 1}, "{project}": [root]}
        assert [call["argv"][3:] for call in calls] == [["guard", "--host", "codex"]]
        assert json.loads(calls[0]["input"]) == spelled
        assert run.results["hook"].failure is None
        assert run.results["hook"].body == {"hookSpecificOutput": {"permissionDecision": "deny"}}

    def test_a_cli_step_without_stdin_passes_no_input_and_has_no_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._captured_run(monkeypatch, "{}")
        result = run_cli(FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator()), ["check-model"])
        assert "input" not in calls[0]
        assert result.body is None
        assert result.stdout == "{}"

    @pytest.mark.parametrize(
        "stdout",
        ["", "not json", "[1, 2]", '{"a": 1} {"b": 2}', '{"a": 1, "a": 2}'],
        ids=["empty", "text", "a-list", "two-objects", "a-repeated-key"],
    )
    def test_a_hook_step_whose_stdout_is_not_one_json_object_is_a_failed_step(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        self._captured_run(monkeypatch, stdout)
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        with pytest.raises(StepFailed, match=r"^swcraft guard answered no JSON object on stdout: "):
            run_cli(project, ["guard"], {"tool_name": "Bash"})

    def test_a_json_stdout_step_reads_its_stdout_as_the_body_whatever_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess  # noqa: PLC0415 - patched for this test alone

        calls: list[dict[str, Any]] = []

        def answer(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            return subprocess.CompletedProcess(argv, 1, stdout='{"findings": [1]}', stderr="")

        monkeypatch.setattr(subprocess, "run", answer)
        scenario = _parse(
            _document(
                steps=[{"id": "check", "cli": ["check-model", "--format", "json"], "stdout": "json"}],
                expectations=[{"step": "check", "at": "findings", "count_equals": {"threshold": "page_rows"}}],
            )
        )
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        run = run_steps(Run(scenario, "default", project, {}))
        assert "input" not in calls[0], "a json_stdout step writes nothing to the command's input"
        assert run.results["check"].exit_code == 1
        assert run.results["check"].body == {"findings": [1]}

    @pytest.mark.parametrize("stdout", ["", "Error: no such revision", "[1]"], ids=["empty", "text", "a-list"])
    def test_a_json_stdout_step_whose_stdout_is_not_one_json_object_is_a_failed_step(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        self._captured_run(monkeypatch, stdout)
        project = FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator())
        with pytest.raises(StepFailed, match=r"^swcraft metrics answered no JSON object on stdout: "):
            run_cli(project, ["metrics"], json_stdout=True)

    def test_a_json_stdout_value_other_than_json_is_refused_by_the_schema(self) -> None:
        document = _document(steps=[{"id": "ports", "cli": ["check-model"], "stdout": "text"}])
        with pytest.raises(ScenarioError, match="refused by the scenario schema"):
            _parse(document)

    def test_a_text_refusal_is_read_as_its_message(self) -> None:
        result = StepResult(texts=("ERROR: git could not read this revision",))
        assert refusal_document(result) == {"refusal": "git could not read this revision"}

    @pytest.mark.parametrize(
        "result",
        [
            StepResult(body={"response": "metrics"}, texts=('{"response": "metrics"}',)),
            StepResult(texts=("No element named 'I' in any layer.",)),
            StepResult(texts=("ERROR: one", "ERROR: two")),
            StepResult(texts=()),
        ],
        ids=["an-answer", "another-text", "two-blocks", "no-text"],
    )
    def test_an_answer_that_is_not_one_text_refusal_is_a_failed_step(self, result: StepResult) -> None:
        with pytest.raises(StepFailed, match="the answer is not one text refusal"):
            refusal_document(result)

    def test_an_impact_summary_text_is_read_as_rows_and_its_footer(self) -> None:
        text = (
            "Impact of 'I' (summary):\n\ninterface:\n  swc (2): A, B\n  connector (1): C0\n\n"
            'swc:\n  (no dependents)\n\n(page: 3 of 9, truncated; in all: swc 4, connector 5; next: after="x")'
        )
        document = impact_summary_document((text,))
        assert document["rows"] == [
            {"kind": "swc", "name": "A"},
            {"kind": "swc", "name": "B"},
            {"kind": "connector", "name": "C0"},
        ]
        assert document["footer"].startswith("(page: 3 of 9")
        assert document["page"] == {"returned": 3, "total": 9, "truncated": True, "next": "x"}
        assert impact_summary_document(("Impact of 'I' (summary):\n\ninterface:\n  (no dependents)",)) == {
            "rows": [],
            "footer": "",
        }
        # A last page carries no cursor; a footer of the direction alone states no page.
        last = text.replace('; next: after="x"', "")
        assert "next" not in impact_summary_document((last,))["page"]
        scoped = text.rsplit("\n", 1)[0] + "\n(direction: dependencies)"
        assert "page" not in impact_summary_document((scoped,))

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("ERROR: the summary is 90 000 wire bytes ...", "not an impact summary"),
            ("No element named 'I' in any layer.", "not an impact summary"),
            ("Impact of 'I' (summary):\n\ninterface:\n  swc (3): A, B", "group of 3 lists 2 names"),
            ("Impact of 'I' (summary):\n\ninterface:\n  via port p", "no known shape"),
        ],
    )
    def test_a_text_that_is_not_an_impact_summary_is_a_failed_step(self, text: str, message: str) -> None:
        with pytest.raises(StepFailed, match=message):
            impact_summary_document((text,))

    def test_report_referrers_name_component_and_port(self) -> None:
        stdout = _report(("IShared", ["Big/pOut", "Small/rIn"]), ("IOwn", ["Big/pOwn"]))
        assert report_referrers(StepResult(stdout=stdout)) == {"Big/pOut", "Small/rIn", "Big/pOwn"}
        assert report_referrers(StepResult(stdout="Import report: x.arxml\n\nIMPORTED 3 interfaces\n")) == set()

    def test_report_merged_names_each_group_by_its_name(self) -> None:
        assert report_merged(StepResult(stdout=_report(merged=("IShared", "IOwn")))) == {"IShared", "IOwn"}
        assert report_merged(StepResult(stdout=_report(("IShared", ["Big/pOut"])))) == set()

    def test_a_report_that_counts_referrers_it_does_not_list_is_a_failed_step(self) -> None:
        stdout = _report(("IShared", ["Big/pOut"])).replace(
            " -> swc.port.interface)\n", " -> swc.port.interface)\n      … and 2 more\n", 1
        )
        with pytest.raises(StepFailed, match=r"counts referrers it does not list \(… and 2 more\): run it --verbose"):
            report_referrers(StepResult(stdout=stdout))

    def test_an_export_fault_step_writes_the_faulted_export(self, tmp_path: Path) -> None:
        tmp_path = tmp_path / "project"
        (tmp_path / "dist").mkdir(parents=True)
        (tmp_path / "dist" / "model.arxml").write_text(ARXML, encoding="utf-8", newline="\n")
        scenario = _parse(
            _document(
                project="fresh",
                target={"conflicted": CONFLICTED, "component": "largest_component"},
                steps=[FAULT_STEP, {**FAULT_STEP, "id": "unknown", "export_faults": NOT_AN_INTERFACE}],
                expectations=[{"step": "faults", "exit_code": 0}],
            )
        )
        project = FunctionalProject(tmp_path, tmp_path / "swcraft.yaml", {}, _Locator())
        run = run_steps(Run(scenario, "default", project, resolve_targets(scenario, _Locator())))
        assert run.results["faults"].body == {"interfaces": ["IShared", "IOwn"], "kind": "value"}
        assert run.results["faults"].failure is None
        written = (tmp_path / "faulted.arxml").read_text(encoding="utf-8")
        assert written == add_conflicts(ARXML, ["IShared", "IOwn"], "value")
        assert run.results["unknown"].failure == "export fault: no sender-receiver interface is named 'Big'"

    def test_finding_pairs_name_the_member(self) -> None:
        stdout = _findings(("r", "C", "p"), ("q", "D", None))
        assert finding_pairs(StepResult(stdout=stdout)) == {("r", "C/p"), ("q", "D")}
        # A layer-wide subject has no element: it is a pair of its own, never a KeyError.
        layer_wide = {"rule": "hardware.layer-absent", "subjects": [{"layer": "hardware"}]}
        document = json.loads(stdout)
        document["findings"].append(layer_wide)
        assert finding_pairs(StepResult(stdout=json.dumps(document))) == {
            ("r", "C/p"),
            ("q", "D"),
            ("hardware.layer-absent", "layer hardware"),
        }
        with pytest.raises(StepFailed, match="not check-model's JSON"):
            finding_pairs(StepResult(stdout="2 error(s)"))

    def test_finding_pairs_spell_a_composite_member_as_its_canonical_json(self) -> None:
        # The subject check-model prints for a task's runnable entry (diagnostics.Subject.to_json),
        # and a member named by its whole value: each one pair, never a TypeError.
        entry = Subject("tasks", "Task10ms").at(field_step("runnables"), member_step({"swc": "S", "runnable": "R"}))
        whole = Subject("swc", "C").at(field_step("tags"), member_step(7))
        document = {
            "schemaVersion": 1,
            "findings": [
                {"rule": "t", "subjects": [entry.to_json()]},
                {"rule": "w", "subjects": [whole.to_json()]},
            ],
        }
        assert finding_pairs(StepResult(stdout=json.dumps(document))) == {
            ("t", 'Task10ms/{"runnable":"R","swc":"S"}'),
            ("w", "C/7"),
        }


def _summary(names: list[str], footer: str) -> str:
    """An impact summary of *names* (all ``swc``) with the scope footer *footer*."""
    return f"Impact of 'I' (summary):\n\ninterface:\n  swc ({len(names)}): {', '.join(names)}\n\n{footer}"


def _continued_run(answers: dict[str, str]) -> Run:
    """A first page and its continuation, answered by *answers* (the page by ``after``, or ``""``)."""
    scenario = _parse(
        _document(
            steps=[
                {"id": "first", "mcp": "impact_of", "arguments": {"name": "I"}, "text": "impact-summary"},
                {
                    "id": "next",
                    "mcp": "impact_of",
                    "arguments": {"name": "I"},
                    "continues": "first",
                    "text": "impact-summary",
                },
            ],
            expectations=[{"step": "next", "at": "rows", "disjoint_from": "first"}],
        )
    )
    run = Run(scenario, "default", FunctionalProject(Path("."), Path("swcraft.yaml"), {}, _Locator()), {})
    return run_steps(run, tool_caller=lambda tool, arguments: StepResult(texts=(answers[arguments.get("after", "")],)))


TRUNCATED = '(page: 2 of 4, truncated; in all: swc 4; next: after="B")'
REST = "(page: 2 of 4, truncated; in all: swc 4)"


class TestTheContinuedPage:
    """A continued page: the cursor is refused unless usable, and the replay must answer new rows."""

    def test_a_cursor_that_continues_holds(self) -> None:
        run = _continued_run({"": _summary(["A", "B"], TRUNCATED), "B": _summary(["C", "D"], REST)})
        evaluate(run)

    @pytest.mark.parametrize(
        ("footer", "message"),
        [
            ('(page: 2 of 4, truncated; in all: swc 4; next: after="")', "carries no usable cursor: next = ''"),
            ("(page: 2 of 4, truncated; in all: swc 4)", "carries no usable cursor: next = None"),
            ('(page: 2 of 4, complete; next: after="B")', "does not say it is truncated"),
            ("(direction: dependencies)", "answered no page to continue"),
        ],
        ids=["empty-cursor", "no-cursor", "complete", "no-page"],
    )
    def test_an_unusable_cursor_fails_the_continuation(self, footer: str, message: str) -> None:
        run = _continued_run({"": _summary(["A", "B"], footer), "B": _summary(["C", "D"], REST)})
        assert message in _failure(run)

    def test_a_cursor_that_restarts_the_walk_fails(self) -> None:
        run = _continued_run({"": _summary(["A", "B"], TRUNCATED), "B": _summary(["B", "C"], REST)})
        assert "1 of 2 rows at 'rows' are step first's rows again, first {'kind': 'swc', 'name': 'B'}" in (
            _failure(run)
        )

    def test_a_continuation_that_answers_nothing_fails(self) -> None:
        run = _continued_run({"": _summary(["A", "B"], TRUNCATED), "B": "Impact of 'I' (summary):\n\n" + REST})
        assert "no rows at 'rows', expected rows past step first's" in _failure(run)

    def test_a_failed_first_page_fails_the_continuation(self) -> None:
        run = _continued_run({"": "ERROR: refused", "B": _summary(["C"], REST)})
        assert "step 'first' failed, so there is no page to continue" in _failure(run)

    def test_a_read_page_cursor_may_be_a_mapping(self) -> None:
        run = Run(_parse(_document()), "default", FunctionalProject(Path("."), Path("x"), {}, _Locator()), {})
        run.results["p"] = StepResult(body={"page": {"truncated": True, "next": {"element": "A"}}})
        assert harness.page_cursor(run, "p") == {"element": "A"}
        run.results["p"] = StepResult(body={"page": {"truncated": True, "next": {}}})
        with pytest.raises(StepFailed, match="carries no usable cursor"):
            harness.page_cursor(run, "p")


def _cursor_mutation(kind: str) -> Callable[[str, dict[str, Any]], StepResult]:
    """The real client, with each answer's page mutated by *kind*.

    The mutations: an emptied cursor, a page said to be complete, a replay whose cursor is dropped (so it
    restarts at the first page), or a replay with no row bound (its limit raised past the walk, so it
    answers every row after the cursor).
    """

    def caller(tool: str, arguments: Mapping[str, Any]) -> StepResult:
        arguments = dict(arguments)
        if kind == "wrong-cursor":
            arguments.pop("after", None)
        if kind == "unbounded" and "after" in arguments:
            arguments["limit"] = 10_000
        result = call_tool(tool, arguments)
        if kind == "empty-cursor":
            result.texts = tuple(re.sub(r'next: after="[^"]*"', 'next: after=""', text) for text in result.texts)
            if isinstance(result.body, dict) and isinstance(result.body.get("page"), dict):
                result.body["page"]["next"] = ""
        if kind == "complete":
            result.texts = tuple(text.replace(", truncated", ", complete") for text in result.texts)
            if isinstance(result.body, dict) and isinstance(result.body.get("page"), dict):
                result.body["page"]["truncated"] = False
        return result

    return caller


class TestTheScenarioCursorsAreReplayed:
    """The two paged scenarios, run with their pages mutated: each mutation fails the scenario."""

    @pytest.mark.functional
    @pytest.mark.parametrize("scenario_id", ["impact-summary-bounded", "unconnected-one-component"])
    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            ("empty-cursor", "carries no usable cursor"),
            ("complete", "does not say it is truncated"),
            ("wrong-cursor", "rows again"),
            ("unbounded", "expected <= page_rows = 20"),
        ],
    )
    def test_a_mutated_page_fails_the_scenario(
        self,
        scenario_id: str,
        kind: str,
        message: str,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        scenario = load_scenario(harness.SCENARIO_DIR / f"{scenario_id}.json")
        project = functional_project(scenario.specimen)
        mcp(project)
        run = Run(scenario, "default", project, resolve_targets(scenario, project.locator))
        run_steps(run, tool_caller=_cursor_mutation(kind))
        failure = _failure(run)
        assert message in failure
        if kind == "unbounded":
            # Every replay of the scenario fails its own row bound, not only the first one found.
            replays = [step["id"] for step in scenario.steps("default") if "continues" in step]
            assert replays
            for replay in replays:
                assert f'"rows_at_most": "page_rows", "step": "{replay}"}}' in failure, replay

    @pytest.mark.functional
    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            ("repeated", "appear more than once"),
            ("foreign", "are not among locator unconnected_ports of component"),
        ],
    )
    def test_a_page_of_the_wrong_ports_fails_the_scenario(
        self,
        kind: str,
        message: str,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        # Each page of the component keeps its count, total and cursor, and its rows are rewritten: one
        # row repeated, or every row moved onto a port of another component that is not unconnected.
        # The counts and the cross-page disjointness still hold; only the rows' identities fail.
        scenario = load_scenario(harness.SCENARIO_DIR / "unconnected-one-component.json")
        project = functional_project(scenario.specimen)
        mcp(project)
        run = Run(scenario, "default", project, resolve_targets(scenario, project.locator))
        component = run.targets["component"]
        unconnected = {(name, port) for name, port in project.locator.unconnected_pairs()}
        other = next(name for name in project.locator.largest_components() if name != component)
        elsewhere = [f"pElsewhere{index}" for index in range(100)]

        def rewritten(tool: str, arguments: Mapping[str, Any]) -> StepResult:
            result = call_tool(tool, arguments)
            value = result.body.get("value") if isinstance(result.body, dict) else None
            rows = value.get("identities") if isinstance(value, dict) else None
            if "join" not in arguments or not isinstance(rows, list) or not rows:
                return result
            if kind == "repeated":
                value["identities"] = [rows[0], *rows[:-1]]
            else:
                offset = 0 if "after" not in arguments else len(rows)
                for index, row in enumerate(rows):
                    row["element"], row["member"] = other, elsewhere[offset + index]
                    assert (row["element"], row["member"]) not in unconnected
            return result

        run_steps(run, tool_caller=rewritten)
        failure = _failure(run)
        assert "2 expectation(s) failed" in failure
        assert message in failure
        for step in ("component-page", "component-next"):
            assert f'"rows_among": {{"locator": "unconnected_ports", "of": "component"}}, "step": "{step}"}}' in (
                failure
            ), step

    @pytest.mark.functional
    def test_a_component_question_answered_about_the_interface_fails_the_scenario(
        self,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        # Every component request redirected to the interface target: the replies still agree with
        # each other, and the locator's model-derived figure for the component tells them apart.
        scenario = load_scenario(harness.SCENARIO_DIR / "impact-summary-bounded.json")
        project = functional_project(scenario.specimen)
        mcp(project)
        run = Run(scenario, "default", project, resolve_targets(scenario, project.locator))
        component, interface = run.targets["component"], run.targets["interface"]

        def redirected(tool: str, arguments: Mapping[str, Any]) -> StepResult:
            if arguments.get("name") == component:
                arguments = {**arguments, "name": interface}
            return call_tool(tool, arguments)

        run_steps(run, tool_caller=redirected)
        failure = _failure(run)
        assert "1 expectation(s) failed" in failure
        assert "expected locator connector_count of component" in failure


class TestTheFixtures:
    def test_a_project_is_copied_once_per_specimen(
        self, functional_project: Callable[[str], FunctionalProject]
    ) -> None:
        first = functional_project(MINIMAL_SPECIMEN)
        assert functional_project(MINIMAL_SPECIMEN) is first
        # The project's passport is the specimen's committed source passport, the figure it was
        # generated from; the specimen gate holds the generated model to it on every stated leaf.
        assert first.passport == MINIMAL
        assert first.root.name == MINIMAL_SPECIMEN
        assert first.manifest == first.root / "swcraft.yaml"
        assert repr(first.locator) == "ScenarioLocator()"

    def test_a_copied_project_carries_the_templates_codex_hook(
        self, fresh_project: Callable[[str], FunctionalProject]
    ) -> None:
        # ``swcraft guard --host codex`` guards only a directory holding both files (CODEX_HOOK_FILE).
        project = fresh_project(MINIMAL_SPECIMEN)
        assert (project.root / CODEX_HOOK_FILE).read_bytes() == TEMPLATE_CODEX_HOOK.read_bytes()
        assert TEMPLATE_CODEX_HOOK.is_file()
        # Outside the model directory, so no passport leaf sees it.
        assert (project.root / "model") not in (project.root / CODEX_HOOK_FILE).parents

    def test_a_fresh_project_is_never_shared(self, fresh_project: Callable[[str], FunctionalProject]) -> None:
        first, second = fresh_project(MINIMAL_SPECIMEN), fresh_project(MINIMAL_SPECIMEN)
        assert first.root != second.root
        assert (first.root.name, second.root.name) == (MINIMAL_SPECIMEN, f"{MINIMAL_SPECIMEN}-2")

    def test_a_scenario_runs_over_the_client_and_the_cli(
        self,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        scenario = _parse(
            _document(
                steps=[
                    {"id": "count", "mcp": "read", "arguments": {"address": {"layer": "swc"}, "form": "count"}},
                    {
                        "id": "ports",
                        "mcp": "read",
                        "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "ports"},
                    },
                    {"id": "check", "cli": ["check-model", "--project", "{manifest}", "--format", "json"]},
                ],
                expectations=[
                    {"step": "count", "at": "count", "count_equals": {"passport": "layers.swc"}},
                    {"step": "ports", "at": "value.identities", "count_equals": {"passport": "components.ports.total"}},
                    {"step": "ports", "at": "value.identities", "rows_at_most": "page_rows"},
                    {"step": "ports", "bytes_at_most": "host_spill_bytes"},
                    {"step": "check", "exit_code": 0},
                ],
                thresholds={
                    "page_rows": {"value": 2, "because": PAGE_BECAUSE},
                    "host_spill_bytes": {"value": 1000, "because": SPILL_BECAUSE},
                },
            )
        )
        project = functional_project(scenario.specimen)
        mcp(project)
        run = run_steps(Run(scenario, "default", project, resolve_targets(scenario, project.locator)))
        evaluate(run)
        # The targets are the locator's structural answers, held to an independent reading of the
        # copied YAML: the components ranked by their ports, the interfaces by the ports naming them,
        # ties broken by name in code-point order -- never a name the generator happens to write.
        components = _yaml_components(project.root / "model")
        ports = {name: len(document.get("ports", [])) for name, document in components.items()}
        ranked = sorted(ports, key=lambda name: (-ports[name], name))
        naming = Counter(port["interface"] for document in components.values() for port in document.get("ports", []))
        assert run.targets == {
            "component": ranked[0],
            "components": tuple(ranked),
            "interface": sorted(naming, key=lambda name: (-naming[name], name))[0],
        }
        assert len(ranked) == MINIMAL["layers"]["swc"]
        assert json.loads(run.results["check"].stdout)["findings"] == []

    def test_a_text_step_counts_the_summary_rows_over_the_client(
        self,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        scenario = _parse(
            _document(
                target={"component": "largest_component"},
                steps=[
                    {
                        "id": "whole",
                        "mcp": "impact_of",
                        "arguments": {"name": {"$target": "component"}},
                        "text": "impact-summary",
                    },
                    {
                        "id": "page",
                        "mcp": "impact_of",
                        "arguments": {"name": {"$target": "component"}, "limit": 1},
                        "text": "impact-summary",
                    },
                ],
                expectations=[{"step": "page", "at": "rows", "rows_at_most": "one"}],
                thresholds={"one": {"value": 1, "because": PAGE_BECAUSE}},
            )
        )
        project = functional_project(MINIMAL_SPECIMEN)
        mcp(project)
        run = run_steps(Run(scenario, "default", project, resolve_targets(scenario, project.locator)))
        evaluate(run)
        whole, page = run.results["whole"].body, run.results["page"].body
        assert len(whole["rows"]) > 1
        assert whole["footer"] == ""
        assert len(page["rows"]) == 1
        assert page["rows"][0] in whole["rows"]
        assert page["footer"].startswith(f"(page: 1 of {len(whole['rows'])}, truncated")

    def test_a_tool_error_is_a_failed_step(
        self,
        functional_project: Callable[[str], FunctionalProject],
        mcp: Callable[[FunctionalProject], None],
    ) -> None:
        mcp(functional_project(MINIMAL_SPECIMEN))
        with pytest.raises(StepFailed, match="read answered a tool error"):
            call_tool("read", {"no_such_argument": 1})


def _fast_gate_deselects() -> frozenset[str]:
    """The markers the fast gate deselects, read off ``scripts/dev.py``'s ``FAST_TEST_DESELECT``.

    ``"not a and not b"`` -> ``{"a", "b"}``.
    """
    return frozenset(re.findall(r"not (\w+)", str(dev_assignment("FAST_TEST_DESELECT"))))


class _Request:
    """A stand-in for pytest's ``request``: records each fixture asked for and answers a maker."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def getfixturevalue(self, name: str) -> Any:
        self.asked.append(name)
        return lambda specimen: name


class TestTheTiers:
    """A scenario's tier gives its case its marks and its project."""

    def test_the_tier_marks_are_the_schemas_tiers(self) -> None:
        schema = json.loads(harness.SCENARIO_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert set(scenarios_module.TIER_MARKS) == set(schema["properties"]["tier"]["enum"])

    def test_no_generated_small_case_carries_a_deselected_mark(self) -> None:
        deselected = _fast_gate_deselects()
        assert {"slow", "benchmark"} <= deselected
        cases = scenarios_module._cases()
        assert cases
        for case in cases:
            path, _ = case.values
            names = {mark.name for mark in case.marks}
            tier = load_scenario(path).tier
            if tier == harness.GENERATED_SMALL:
                assert names & deselected == set(), case.id
            else:
                assert "large_bench" in names, case.id
        assert {mark.name for mark in scenarios_module.TIER_MARKS[harness.GENERATED_SMALL]} & deselected == set()

    def test_a_fast_selection_case_never_asks_for_the_large_bench(self) -> None:
        # The test function names no large-bench fixture, so pytest sets none up for it; only
        # project_for asks for one, and only for a large-bench scenario.
        assert "large_bench_project" not in inspect.signature(scenarios_module.test_scenario).parameters
        for path in harness.scenario_paths():
            scenario = load_scenario(path)
            request = _Request()
            scenarios_module.project_for(scenario, request)()
            if scenario.tier == harness.GENERATED_SMALL:
                assert request.asked == ["fresh_project" if scenario.fresh else "functional_project"], path.name
            else:
                assert request.asked == ["large_bench_project"], path.name
        assert any(load_scenario(path).tier == harness.LARGE_BENCH for path in harness.scenario_paths())

    def test_a_large_bench_that_moved_fails_naming_both_digests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # example-minimal stands in for the large bench, so the fast gate copies no 3 700 files; the
        # cache's own take, which reads the real pins, runs before the pin is moved.
        generator = specimen_generator_module()
        expected = tmp_path / "expected"
        copy_specimen(MINIMAL_SPECIMEN, expected)
        found = generator.digest(expected / "model")
        # The fixture's own take answers from this process's memo of that take, so the moved pin
        # reaches the fixture's second digest alone; without the memo the take would regenerate.
        cache = Path(os.path.abspath(harness.REPO_ROOT / generator.SPECIMEN_CACHE)).as_posix()
        assert (cache, MINIMAL_SPECIMEN) in generator._TAKEN
        real_load_pins = generator.load_pins

        def moved_pin(repo_root: Path) -> dict[str, Any]:
            pins = copy.deepcopy(real_load_pins(repo_root))
            pins["specimens"][MINIMAL_SPECIMEN]["model_sha256"] = "0" * 64
            return pins

        monkeypatch.setattr(functional_conftest, "LARGE_BENCH_SPECIMEN", MINIMAL_SPECIMEN)
        monkeypatch.setattr(generator, "load_pins", moved_pin)
        with pytest.raises(pytest.fail.Exception, match=rf"digest is {found}, not the pin's model_sha256 {'0' * 64}"):
            large_bench_project_at(tmp_path / "probe")


def test_every_specimen_the_project_warms_is_a_pinned_one() -> None:
    """The ``mcp_server`` conftest's warming list names pinned specimens only.

    A misspelt name would be skipped by the warming and fail only at its first take.
    """
    from tests.conftest import MCP_SPECIMENS  # noqa: PLC0415 - this package's conftest, not the tools tree's

    pinned = set(specimen_generator_module().load_pins(harness.REPO_ROOT)["specimens"])
    assert sorted(set(MCP_SPECIMENS) - pinned) == []


def _write_element(path: Path, element: Mapping[str, Any]) -> None:
    """*element* written as one element file (JSON, which YAML reads as its flow form)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(element), encoding="utf-8", newline="\n")


def _component(name: str, ports: list[tuple[str, str, str]], **accesses: Any) -> dict[str, Any]:
    """A component of *ports* ``(name, direction, interface)``, one runnable with *accesses* (reads, writes)."""
    run = {
        "name": "Run",
        **{key: [{"port": port, "element": element} for port, element in value] for key, value in accesses.items()},
    }
    element: dict[str, Any] = {
        "name": name,
        "type": "application",
        "ports": [
            {"name": port, "direction": direction, "interface": interface} for port, direction, interface in ports
        ],
        "runnables": [run],
    }
    return element


def _hand_model(
    root: Path,
    components: list[dict[str, Any]],
    connectors: list[tuple[str, str, str, str]] = (),
    *,
    levels: Mapping[str, str] | None = None,
) -> ScenarioLocator:
    """A model written by hand under *root*: IA (x, y), IB (x, y), IC (a, b) and the client-server IS.

    *connectors* are ``(from component, from port, to component, to port)``; *levels* a component's
    declared ``asil``. The model lives under ``root / "hand"``, beside the suite's autouse project in
    ``tmp_path``. The locator is built as the conftest builds it.
    """
    root = root / "hand"
    for name, elements in (("IA", "xy"), ("IB", "xy"), ("IC", "ab")):
        _write_element(
            root / "interfaces" / f"{name}.yaml",
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, "implementation_type": "T"} for element in elements],
            },
        )
    _write_element(
        root / "interfaces" / "IS.yaml",
        {"name": "IS", "kind": "client_server", "operations": [{"name": "Op", "arguments": []}]},
    )
    for component in components:
        if levels and component["name"] in levels:
            component = {**component, "asil": levels[component["name"]]}
        _write_element(root / "swc" / "g" / f"{component['name']}.yaml", component)
    for index, (from_swc, from_port, to_swc, to_port) in enumerate(connectors):
        _write_element(
            root / "connectors" / f"K{index}.yaml",
            {"name": f"K{index}", "from": {"swc": from_swc, "port": from_port}, "to": {"swc": to_swc, "port": to_port}},
        )
    return ScenarioLocator(compute_passport(root).locator, root)


#: The hand model's components: IA's y and IB's y are read twice each (a tie between the two
#: interfaces, IA first by name), IA's x once; IC's a and b once each (a tie inside the interface,
#: a first by name); a read through the client-server port is not a sender-receiver read.
_READERS = [
    _component(
        "C1",
        [("rA", "require", "IA"), ("rB", "require", "IB"), ("rS", "require", "IS")],
        reads=[("rA", "x"), ("rA", "y"), ("rB", "y"), ("rS", "x")],
    ),
    _component("C2", [("rA", "require", "IA"), ("rB", "require", "IB")], reads=[("rA", "y"), ("rB", "y")]),
    _component("C3", [("rC", "require", "IC")], reads=[("rC", "a"), ("rC", "b")]),
    _component("P", [("pA", "provide", "IA")], writes=[("pA", "x"), ("pA", "y")]),
]


class TestTheScenarioLocator:
    """``ScenarioLocator``'s own questions, on hand-written models with known answers and on a generated one."""

    def test_the_data_access_questions_break_ties_by_name(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, _READERS)
        assert locator.most_read_sender_receiver_interface() == "IA"
        assert locator.most_read_data_element() == "y"
        assert (locator.readers_count("IA"), locator.readers_count("IB"), locator.readers_count("IC")) == (2, 2, 1)
        with pytest.raises(KeyError):
            locator.readers_count("IS")

    def test_a_tie_inside_an_interface_takes_the_first_element_by_name(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, [_READERS[2]])
        assert (locator.most_read_sender_receiver_interface(), locator.most_read_data_element()) == ("IC", "a")

    def test_a_model_no_runnable_reads_has_no_most_read_interface(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, [_READERS[3]])
        with pytest.raises(LookupError, match="no runnable reads a data element of a sender-receiver interface"):
            locator.most_read_sender_receiver_interface()
        assert locator.readers_count("IA") == 0

    @pytest.mark.parametrize(
        ("levels", "commonest"),
        [
            ({"C1": "B", "C2": "B(D)", "C3": "A"}, "B"),
            ({"C1": "B", "C3": "A"}, "A"),
            ({"C1": "QM", "C2": "D"}, "QM"),
            ({}, "QM"),
        ],
        ids=["most-frequent", "tie-in-level-order", "tie-with-qm", "none-declared"],
    )
    def test_the_commonest_level(self, tmp_path: Path, levels: dict[str, str], commonest: str) -> None:
        # A decomposed level counts as its X (B(D) is B), as the passport's by_declared_level does.
        # "none-declared" is the all-zero tie: QM by ASIL_LEVELS order, the tie-break's case.
        assert _hand_model(tmp_path, _READERS, levels=levels).commonest_level() == commonest

    def test_the_declared_level_count_counts_by_rating(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, _READERS, levels={"C1": "B", "C2": "B(D)", "C3": "A"})
        assert [locator.declared_level_count(level) for level in ("QM", "A", "B", "C", "D")] == [0, 1, 2, 0, 0]
        with pytest.raises(KeyError):
            locator.declared_level_count("E")

    def test_the_model_is_read_on_the_first_own_question_not_at_construction(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, _READERS)
        assert "_model" not in vars(locator)
        assert locator.largest_component()
        assert "_model" not in vars(locator), "a passport question reads nothing more"
        locator.commonest_level()
        assert "_model" in vars(locator)

    def test_the_sender_receiver_rows_are_counted_per_connector_by_port_direction(self, tmp_path: Path) -> None:
        # P.pA -> C1.rA: P writes x and y, C1 reads both through rA, two rows. C2.rA -> P.pA, drawn
        # from the requester's end: C2 reads y alone, one row, the provider found by its port's
        # direction. C1.rB -> C2.rB joins two require ports and has none. A hand count of three.
        locator = _hand_model(
            tmp_path, _READERS, [("P", "pA", "C1", "rA"), ("C2", "rA", "P", "pA"), ("C1", "rB", "C2", "rB")]
        )
        assert locator.sender_receiver_rows() == 3

    def test_a_connector_end_on_a_composition_is_refused(self, tmp_path: Path) -> None:
        locator = _hand_model(tmp_path, _READERS)
        # Asked before KD is written: the locator reads the model on its first own question.
        assert locator.sender_receiver_rows() == 0
        _write_element(
            tmp_path / "hand" / "connectors" / "KD.yaml",
            {"name": "KD", "from": {"composition": "Sub", "port": "pA"}, "to": {"swc": "C1", "port": "rA"}},
        )
        refusing = ScenarioLocator(compute_passport(tmp_path / "hand").locator, tmp_path / "hand")
        assert locator.sender_receiver_rows() == 0, "read once, on the first question"
        with pytest.raises(ValueError, match="connector KD has an end on a composition"):
            refusing.sender_receiver_rows()

    def test_a_generated_model_has_its_rows_by_an_independent_count_and_no_composition_end(
        self, functional_project: Callable[[str], FunctionalProject]
    ) -> None:
        # harness-two-rows has no hub and no spare, so every sender-receiver port is on a connector.
        # Re-derived by an independent count of the copied YAML's accesses, never by a formula over
        # the generator's wiring.
        project = functional_project(TWO_ROWS_SPECIMEN)
        model = project.root / "model"
        components = _yaml_components(model)
        connectors = _yaml_documents(model, "connectors")
        rows = 0
        for connector in connectors:
            ends = (connector["from"], connector["to"])
            assert all("swc" in end for end in ends), connector["name"]
            directions = [
                next(port["direction"] for port in components[end["swc"]]["ports"] if port["name"] == end["port"])
                for end in ends
            ]
            if sorted(directions) != ["provide", "require"]:
                continue
            provider, requester = ends if directions[0] == "provide" else ends[::-1]

            def accesses(end: Mapping[str, str], kind: str) -> list[str]:
                return [
                    access["element"]
                    for runnable in components[end["swc"]].get("runnables", [])
                    for access in runnable.get(kind, [])
                    if access["port"] == end["port"]
                ]

            read = Counter(accesses(requester, "reads"))
            rows += sum(read[element] for element in accesses(provider, "writes"))
        assert len(connectors) > 0
        assert rows > 0
        # The figure refuses a connector with an end on a composition, so its answering at all is
        # the witness, beside the independent reading's, that the specimen has none.
        assert project.locator.sender_receiver_rows() == rows

    def test_the_model_figures_are_counted_off_the_files(self, tmp_path: Path) -> None:
        # _READERS has four components, seven ports and four runnables; X is an external stub with one
        # provide port and one runnable, so it counts among the runnables alone. The dependencies:
        # P.pA -> C1.rA states (C1, P); C2.rA -> P.pA, drawn from the requester's end, states (C2, P);
        # the repeat of the first counts once; two require ports, and C1 to itself, state none;
        # X.pC -> C3.rC states (C3, X), a stub with a declared end, kept. A hand count of three.
        stub = {**_component("X", [("pC", "provide", "IC")]), "external": True}
        locator = _hand_model(
            tmp_path,
            [*_READERS, stub],
            [
                ("P", "pA", "C1", "rA"),
                ("C2", "rA", "P", "pA"),
                ("P", "pA", "C1", "rA"),
                ("C1", "rB", "C2", "rB"),
                ("C1", "rS", "C1", "rA"),
                ("X", "pC", "C3", "rC"),
            ],
        )
        _write_element(
            tmp_path / "hand" / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root", "root": True, "members": [{"name": "M1", "type": "C1"}]},
        )
        assert locator.model_components() == 4
        assert locator.model_component_ports() == 7
        assert locator.model_runnables() == 5
        assert locator.model_connectors() == 6
        assert locator.model_compositions() == 1
        assert locator.model_dependencies() == 3

    def test_a_pair_of_two_external_stubs_is_no_dependency(self, tmp_path: Path) -> None:
        stubs = [
            {**_component("X", [("pC", "provide", "IC")]), "external": True},
            {**_component("Y", [("rC", "require", "IC")]), "external": True},
        ]
        locator = _hand_model(tmp_path, stubs, [("X", "pC", "Y", "rC")])
        assert (locator.model_components(), locator.model_dependencies()) == (0, 0)

    def test_the_dependencies_refuse_a_connector_end_on_a_composition(self, tmp_path: Path) -> None:
        _hand_model(tmp_path, _READERS)
        _write_element(
            tmp_path / "hand" / "connectors" / "KD.yaml",
            {"name": "KD", "from": {"composition": "Sub", "port": "pA"}, "to": {"swc": "C1", "port": "rA"}},
        )
        refusing = ScenarioLocator(compute_passport(tmp_path / "hand").locator, tmp_path / "hand")
        with pytest.raises(ValueError, match="connector KD has an end on a composition"):
            refusing.model_dependencies()

    def test_a_generated_model_has_its_dependencies_by_an_independent_count(
        self, functional_project: Callable[[str], FunctionalProject]
    ) -> None:
        # The specimen of the metrics scenarios: 322 connectors over far fewer pairs, so a count of
        # connectors instead of distinct pairs fails. Re-derived off the copied YAML.
        project = functional_project(METRICS_SPECIMEN)
        model = project.root / "model"
        components = _yaml_components(model)
        connectors = _yaml_documents(model, "connectors")
        pairs: set[tuple[str, str]] = set()
        for connector in connectors:
            ends = (connector["from"], connector["to"])
            assert all("swc" in end for end in ends), connector["name"]
            directions = [
                next(port["direction"] for port in components[end["swc"]]["ports"] if port["name"] == end["port"])
                for end in ends
            ]
            if sorted(directions) == ["provide", "require"] and ends[0]["swc"] != ends[1]["swc"]:
                provider, requirer = ends if directions[0] == "provide" else ends[::-1]
                pairs.add((requirer["swc"], provider["swc"]))
        assert not any(document.get("external") for document in components.values())
        assert len(connectors) > len(pairs) > 0
        assert project.locator.model_dependencies() == len(pairs)
        assert project.locator.model_connectors() == len(connectors)
        assert project.locator.model_components() == len(components)

    @staticmethod
    def _memory_model(root: Path, memories: list[dict[str, Any]], budgets: Mapping[str, Mapping[str, int]]) -> Any:
        """:func:`_hand_model`'s components with *budgets* (component -> memory -> bytes), and *memories*."""
        components = [{**component, "memory": dict(budgets.get(component["name"], {}))} for component in _READERS]
        locator = _hand_model(root, components)
        for memory in memories:
            _write_element(
                root / "hand" / "hardware" / f"{memory['name']}.yaml",
                {"kind": "memory", "type": "RAM", "accessible": ["CORE0"], "enforcement": "strict", **memory},
            )
        return locator

    def test_the_fit_questions_name_the_memory_with_a_whole_amount_in_the_gap(self, tmp_path: Path) -> None:
        # RAM1: size 1000 x 0.8 = 800, used 700: the smallest whole amount over 800 is 101 (801 in
        # all), within the size; 301 (1001 in all) is the smallest over the size.
        budgets = {"C1": {"RAM1": 400}, "C2": {"RAM1": 300}, "C3": {"RAM1": 0}, "P": {"RAM1": 0}}
        locator = self._memory_model(tmp_path, [{"name": "RAM1", "size": 1000, "threshold": 0.8}], budgets)
        assert (locator.threshold_gap_memory(), locator.threshold_gap_bytes(), locator.over_size_bytes()) == (
            "RAM1",
            101,
            301,
        )

    def test_an_external_stub_is_no_consumer_of_a_memory(self, tmp_path: Path) -> None:
        # The analysis ignores an external stub (`resource_analysis`: a stub is not a consumer), so
        # one declaring no budget neither makes RAM1 undeclared nor moves its use: the same answers
        # as the model without it.
        budgets = {"C1": {"RAM1": 400}, "C2": {"RAM1": 300}, "C3": {"RAM1": 0}, "P": {"RAM1": 0}}
        locator = self._memory_model(tmp_path, [{"name": "RAM1", "size": 1000, "threshold": 0.8}], budgets)
        _write_element(
            tmp_path / "hand" / "swc" / "g" / "Stub_ext.yaml",
            {"name": "Stub_ext", "type": "application", "external": True},
        )
        assert (locator.threshold_gap_memory(), locator.threshold_gap_bytes(), locator.over_size_bytes()) == (
            "RAM1",
            101,
            301,
        )

    def test_a_non_whole_threshold_limit_is_floored(self, tmp_path: Path) -> None:
        # 28 672 x 0.8 = 22 937.6, the small scenario's RAM: 22 937 - 15 872 + 1 = 7 066 is over the
        # limit by 0.4 bytes, and one byte less (7 065, 22 937 in all) is within it. A float product
        # (28672 * 0.8 = 22937.600000000002) would floor to the same, but 0.29 x 100 would not.
        budgets = {"C1": {"RAM0": 15872}, "C2": {"RAM0": 0}, "C3": {"RAM0": 0}, "P": {"RAM0": 0}}
        locator = self._memory_model(tmp_path, [{"name": "RAM0", "size": 28672, "threshold": 0.8}], budgets)
        assert locator.threshold_gap_bytes() == 7066
        # 100 x 0.29 is 29 exactly (28.999999999999996 in floats, which would floor to 28): 29 - 28 +
        # 1 = 2, where the float product would answer 1, an amount within the limit.
        whole = self._memory_model(
            tmp_path / "whole",
            [{"name": "RAMX", "size": 100, "threshold": 0.29}],
            {name: {"RAMX": 28 if name == "C1" else 0} for name in ("C1", "C2", "C3", "P")},
        )
        assert whole.threshold_gap_bytes() == 2

    def test_a_memory_with_an_undeclared_budget_is_skipped(self, tmp_path: Path) -> None:
        # RAMA comes first by name, and C3 declares nothing for it; RAMB's consumers all declare.
        budgets = {name: {"RAMB": 100} for name in ("C1", "C2", "C3", "P")}
        for name in ("C1", "C2", "P"):
            budgets[name]["RAMA"] = 10
        memories = [{"name": "RAMA", "size": 1000, "threshold": 0.5}, {"name": "RAMB", "size": 1000, "threshold": 0.5}]
        locator = self._memory_model(tmp_path, memories, budgets)
        assert (locator.threshold_gap_memory(), locator.threshold_gap_bytes(), locator.over_size_bytes()) == (
            "RAMB",
            101,
            601,
        )

    @pytest.mark.parametrize(
        ("memory", "used"),
        [({"size": 10, "threshold": 1.0}, 0), ({"size": 1000, "threshold": 0.5}, 501)],
        ids=["threshold-one", "already-over"],
    )
    def test_a_model_without_the_gap_has_no_answer(self, tmp_path: Path, memory: dict[str, Any], used: int) -> None:
        # A whole size leaves a whole amount between the limit and the size unless the threshold is 1
        # (10 x 0.95 = 9.5 still has 10); a use over the limit leaves no amount below it to start from.
        budgets = {name: {"RAM": used if name == "C1" else 0} for name in ("C1", "C2", "C3", "P")}
        locator = self._memory_model(tmp_path, [{"name": "RAM", **memory}], budgets)
        for question in (locator.threshold_gap_memory, locator.threshold_gap_bytes, locator.over_size_bytes):
            with pytest.raises(LookupError, match="no memory whose every consumer declares a budget"):
                question()

    def test_the_declared_resources_are_every_core_and_memory_by_name(self, tmp_path: Path) -> None:
        # A partition and a processor are hardware too, and no resource of the build report's table.
        locator = _hand_model(tmp_path, _READERS)
        hardware = tmp_path / "hand" / "hardware"
        for element in (
            {"name": "CORE1", "kind": "core", "threshold": 0.7},
            {"name": "CORE0", "kind": "core", "threshold": 0.7},
            {"name": "RAM", "kind": "memory", "type": "RAM", "size": 1000, "accessible": ["CORE0"], "threshold": 0.8},
            {
                "name": "Flash",
                "kind": "memory",
                "type": "Flash",
                "size": 4096,
                "accessible": ["CORE1"],
                "threshold": 1.0,
            },
            {"name": "P1", "kind": "partition", "core": "CORE0"},
        ):
            _write_element(hardware / f"{element['name']}.yaml", element)
        assert locator.declared_resources() == ("CORE0", "CORE1", "Flash", "RAM")

    def test_a_model_without_hardware_declares_no_resource(self, tmp_path: Path) -> None:
        assert _hand_model(tmp_path, _READERS).declared_resources() == ()
