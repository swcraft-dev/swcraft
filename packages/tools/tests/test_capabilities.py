"""The component exchange's capability registry, ``component_exchange/capabilities.py``.

Table-driven over the seven entries: :func:`read_signal`, :func:`decide`, each family's
projection, the carry groups and the report lines. The expected lines, regimes and projections
are written out by hand, not derived from the entries, so a moved word in an entry fails by name.

What this does not reach: whether ``plan_component_import`` calls the mechanism at the
right points and in the right order, and what an import writes or reports from a
decision -- the exchange suites and ``scripts/exchange_output_oracle.py`` hold that.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from swcraft_tools import capability_markers
from swcraft_tools.component_exchange import capabilities
from swcraft_tools.component_exchange.capabilities import (
    ACCESS_LISTS,
    ASYNCHRONOUS_CALLS,
    CAPABILITIES,
    CARRIED,
    CARRY_GROUPS,
    DATA_EVENTS,
    EXCLUSIVE_AREAS,
    IMPLICIT_ACCESS,
    MARKED,
    OPERATION_EVENTS,
    REPLACE_FLAG,
    STATED,
    VARIABLES,
    Capability,
    CarryContext,
    Decision,
    Signal,
    carry,
    decide,
    project_runnable,
    project_swc,
    read_signal,
)
from swcraft_tools.component_exchange.divergence import projected_swc
from swcraft_tools.component_exchange.report import ComponentImportReport
from swcraft_tools.schema import Runnable, SWComponent

_SOURCE = "the file"

#: Each refusing family's refusal line for the value ``'bogus'``, word for word.
_REFUSALS: dict[str, str] = {
    "operation_events": (
        "the component's internal behaviour carries the SWCraft OPERATION-EVENTS marker with the value "
        "'bogus', not 'operation events and concurrency declared', so the file cannot say whether a runnable "
        "without an operation event declares none; export the component again with this release"
    ),
    "variables": (
        "the component's internal behaviour carries the SWCraft INTER-RUNNABLE-VARIABLES marker with the "
        "value 'bogus', not 'inter-runnable variables and local variable accesses declared', so the file "
        "cannot say whether a component without a variable declares none; export the component again with "
        "this release"
    ),
    "exclusive_areas": (
        "the component's internal behaviour carries the SWCraft EXCLUSIVE-AREAS marker with the value "
        "'bogus', not 'exclusive areas and exclusive area references declared', so the file cannot say "
        "whether a component without an exclusive area declares none; export the component again with this "
        "release"
    ),
    "data_events": (
        "the component's internal behaviour carries the SWCraft DATA-EVENTS marker with the value 'bogus', "
        "not 'data-received events declared', so the file cannot say whether a runnable without a "
        "data-received event declares none; export the component again with this release"
    ),
    "implicit_access": (
        "the component's internal behaviour carries the SWCraft IMPLICIT-ACCESS marker with the value 'bogus', "
        "not 'implicit reads and writes declared', so the file cannot say whether a read or a write in an "
        "explicit form is explicit; export the component again with this release"
    ),
    "asynchronous_calls": (
        "the component's internal behaviour carries the SWCraft ASYNCHRONOUS-CALLS marker with the value "
        "'bogus', not 'asynchronous calls, result points and call-returned events declared', so the file "
        "cannot say whether a synchronous call is synchronous and a runnable without a call_returned event "
        "declares none; export the component again with this release"
    ),
}


def _model(markers: dict[str, str]) -> Any:
    """The one attribute :func:`read_signal` reads: ``model.swcs[0].swcraft_markers``."""
    return SimpleNamespace(swcs=[SimpleNamespace(swcraft_markers=markers)])


def _component(**fields: Any) -> SWComponent:
    return SWComponent.model_validate({"name": "Probe", **fields})


#: A component that states nothing any family asks about.
_SILENT = _component(runnables=[{"name": "Quiet"}])

#: Per component-scope family with a ``states`` predicate: components that state its content.
_STATING: dict[str, list[SWComponent]] = {
    "variables": [
        _component(variables=[{"name": "Level", "access": "explicit", "implementation_type": "uint8"}]),
        _component(runnables=[{"name": "Reader", "reads_variables": [{"variable": "Level"}]}]),
        _component(runnables=[{"name": "Writer", "writes_variables": [{"variable": "Level"}]}]),
    ],
    "exclusive_areas": [
        _component(exclusive_areas=[{"name": "Guard"}]),
        _component(runnables=[{"name": "Enter", "can_enter": [{"area": "Guard"}]}]),
        _component(runnables=[{"name": "Inside", "runs_inside": [{"area": "Guard"}]}]),
    ],
    "data_events": [
        _component(runnables=[{"name": "OnData", "events": [{"type": "data", "port": "In", "element": "value"}]}]),
    ],
    "implicit_access": [
        _component(runnables=[{"name": "Reader", "reads": [{"port": "In", "element": "value", "by": "implicit"}]}]),
        _component(runnables=[{"name": "Writer", "writes": [{"port": "Out", "element": "value", "by": "implicit"}]}]),
    ],
    "asynchronous_calls": [
        _component(
            runnables=[{"name": "Caller", "calls": [{"port": "Cli", "operation": "Get", "asynchronous": True}]}]
        ),
        _component(
            runnables=[{"name": "OnReturn", "events": [{"type": "call_returned", "port": "Cli", "operation": "Get"}]}]
        ),
    ],
}

#: What the three-state families' components hold that a family before contract 12.7.0
#: never asks about, and so never makes it ``stated``: a by-value read and a
#: synchronous call.
_THREE_STATE = (VARIABLES, EXCLUSIVE_AREAS, DATA_EVENTS, IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS)


class TestTheRegistry:
    def test_the_seven_entries_in_report_order(self) -> None:
        """The implicit access and the asynchronous calls come last, in that order."""
        assert [c.key for c in CAPABILITIES] == [
            "access_lists",
            "operation_events",
            "variables",
            "exclusive_areas",
            "data_events",
            "implicit_access",
            "asynchronous_calls",
        ]
        assert CAPABILITIES == (
            ACCESS_LISTS,
            OPERATION_EVENTS,
            VARIABLES,
            EXCLUSIVE_AREAS,
            DATA_EVENTS,
            IMPLICIT_ACCESS,
            ASYNCHRONOUS_CALLS,
        )

    def test_the_registry_and_the_wire_end_with_the_two_in_the_same_order(self) -> None:
        assert CAPABILITIES[-2:] == (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS)
        assert capability_markers.COMPONENT_EXPORT_MARKERS[-2:] == (
            capability_markers.IMPLICIT_ACCESS,
            capability_markers.ASYNCHRONOUS_CALLS,
        )
        assert [c.marker for c in CAPABILITIES[-2:]] == list(capability_markers.COMPONENT_EXPORT_MARKERS[-2:])

    def test_no_regime_constant_or_helper_of_the_two_families_lives_outside_the_registry(self) -> None:
        """No ``IMPLICIT_ACCESS_*`` / ``ASYNCHRONOUS_CALL_*`` name lives outside the registry.

        ``import_.py`` names neither family's marker.
        """
        package = Path(capabilities.__file__).parent
        offenders: list[str] = []
        for module in sorted(package.glob("*.py"), key=lambda path: path.as_posix()):
            if module.name == "capabilities.py":
                continue
            tree = ast.parse(module.read_text(encoding="utf-8"))
            # The report's own fields (``implicit_access_regime`` and the preserved counts) are
            # declared by hand in ``report.py`` -- what the registry does not reach -- so a class
            # body's annotated fields are not names of a regime; a module-level name, a function
            # or a class is.
            for node in [*tree.body, *ast.walk(tree)]:
                names: list[str] = []
                if isinstance(node, ast.FunctionDef | ast.ClassDef):
                    names.append(node.name)
                elif node in tree.body and isinstance(node, ast.Assign):
                    names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
                elif node in tree.body and isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.append(node.target.id)
                elif isinstance(node, ast.Attribute) and node.attr in {"IMPLICIT_ACCESS", "ASYNCHRONOUS_CALLS"}:
                    offenders.append(f"{module.name}: reads {node.attr}")
                offenders.extend(
                    f"{module.name}: defines {name}"
                    for name in names
                    if name.lstrip("_").upper().startswith(("IMPLICIT_ACCESS", "ASYNCHRONOUS_CALL"))
                    or "implicit_access" in name.lower()
                    or "asynchronous_call" in name.lower()
                )
        assert not offenders, offenders

    def test_each_entry_names_its_wire_marker_and_heading(self) -> None:
        assert [(c.marker, c.heading) for c in CAPABILITIES] == [
            (capability_markers.ACCESS_POINTS, "ACCESS LISTS"),
            (capability_markers.OPERATION_EVENTS, "OPERATION EVENTS"),
            (capability_markers.INTER_RUNNABLE_VARIABLES, "VARIABLES"),
            (capability_markers.EXCLUSIVE_AREAS, "EXCLUSIVE AREAS"),
            (capability_markers.DATA_EVENTS, "DATA-RECEIVED EVENTS"),
            (capability_markers.IMPLICIT_ACCESS, "IMPLICIT ACCESS"),
            (capability_markers.ASYNCHRONOUS_CALLS, "ASYNCHRONOUS CALLS"),
        ]

    def test_the_data_types_marker_is_no_capability(self) -> None:
        """``DATA-TYPES`` selects no regime: it only proves which release wrote a file."""
        assert capability_markers.DATA_TYPES not in {c.marker for c in CAPABILITIES}

    def test_the_keys_are_unique(self) -> None:
        assert len({c.key for c in CAPABILITIES}) == len(CAPABILITIES)

    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_the_regime_field_exists_on_the_report(self, capability: Capability) -> None:
        assert capability.regime_field in ComponentImportReport.__dataclass_fields__
        assert ComponentImportReport.__dataclass_fields__[capability.regime_field].default == "none"

    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_the_report_fields_exist_on_the_report(self, capability: Capability) -> None:
        assert capability.report_fields
        assert capability.regime_field not in capability.report_fields
        missing = [name for name in capability.report_fields if name not in ComponentImportReport.__dataclass_fields__]
        assert not missing, missing

    def test_the_report_fields_are_the_hooks_own(self) -> None:
        """Written out by hand: what each family's lines around its regime line read."""
        assert {c.key: c.report_fields for c in CAPABILITIES} == {
            "access_lists": ("access_marker_value", "preserved_access_lists"),
            "operation_events": ("preserved_operation_events",),
            "variables": ("preserved_variables", "preserved_variable_accesses", "preserved_variable_mappings"),
            "exclusive_areas": (
                "preserved_exclusive_areas",
                "preserved_exclusive_area_references",
                "written_exclusive_areas",
            ),
            "data_events": ("preserved_data_events",),
            "implicit_access": ("preserved_implicit_accesses",),
            "asynchronous_calls": ("preserved_asynchronous_calls", "preserved_call_returned_events"),
        }

    def test_the_sentences_name_every_regime_the_family_can_select(self) -> None:
        assert {c.key: set(c.sentences) for c in CAPABILITIES} == {
            "access_lists": {MARKED, REPLACE_FLAG, CARRIED},
            "operation_events": {MARKED, CARRIED},
            "variables": {MARKED, STATED, CARRIED},
            "exclusive_areas": {MARKED, STATED, CARRIED},
            "data_events": {MARKED, STATED, CARRIED},
            "implicit_access": {MARKED, STATED, CARRIED},
            "asynchronous_calls": {MARKED, STATED, CARRIED},
        }


class TestTheReportLines:
    """The one renderer: per family its lines before, its regime line, its lines after, in registry order."""

    def _lines(self, **fields: Any) -> list[str]:
        report = ComponentImportReport(source="x.arxml", dry_run=False, swc="S", swc_action="updated", **fields)
        return [line for block in report.blocks() for line in block.text_lines()]

    def test_every_family_in_order_with_the_unrecognized_line_first_and_the_written_line_last(self) -> None:
        lines = self._lines(
            access_marker_value="bogus",
            access_list_regime=CARRIED,
            preserved_access_lists={"R": {"reads": 1}},
            operation_event_regime=CARRIED,
            preserved_operation_events={"R": {"operation_events": 1, "concurrent": True}},
            variable_regime=CARRIED,
            preserved_variables=["v"],
            preserved_variable_accesses={"R": {"reads_variables": 1}},
            preserved_variable_mappings=["M"],
            exclusive_area_regime=CARRIED,
            preserved_exclusive_areas=["A"],
            preserved_exclusive_area_references={"R": {"can_enter": 1}},
            written_exclusive_areas=(1, 1),
            data_event_regime=CARRIED,
            preserved_data_events={"R": 2},
            implicit_access_regime=CARRIED,
            preserved_implicit_accesses={"R": {"reads": 1}},
            asynchronous_call_regime=CARRIED,
            preserved_asynchronous_calls={"R": 1},
            preserved_call_returned_events={"Q": 1},
        )
        family = [line for line in lines if not line.startswith(("SWC S", "CREATED", "REUSED"))]
        assert [line.split(" -- ")[0].split(":")[0] for line in family] == [
            "ACCESS-POINTS marker present with value 'bogus'",
            "ACCESS LISTS regime",
            "ACCESS LISTS preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "OPERATION EVENTS regime",
            "OPERATION EVENTS preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "VARIABLES regime",
            "VARIABLES preserved from the target project (not carried by the source ARXML)",
            "VARIABLE ACCESSES preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "VARIABLES' MAPPING SETS preserved from the target project (not carried by the source ARXML)",
            "EXCLUSIVE AREAS regime",
            "EXCLUSIVE AREAS preserved from the target project (not carried by the source ARXML)",
            "EXCLUSIVE AREA REFERENCES preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "WRITTEN 1 exclusive areas and 1 exclusive area references",
            "DATA-RECEIVED EVENTS regime",
            "DATA-RECEIVED EVENTS preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "IMPLICIT ACCESS regime",
            "IMPLICIT ACCESS preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "ASYNCHRONOUS CALLS regime",
            "ASYNCHRONOUS CALLS preserved from the target project (not carried by the source ARXML)",
            "  - R",
            "CALL-RETURNED EVENTS preserved from the target project (not carried by the source ARXML)",
            "  - Q",
        ]

    def test_a_regime_of_none_renders_no_regime_line_but_the_unrecognized_line_stays(self) -> None:
        lines = self._lines(access_marker_value="bogus")
        assert not [line for line in lines if " regime: " in line]
        assert any(line.startswith("ACCESS-POINTS marker present") for line in lines)

    def test_the_regime_line_is_heading_regime_and_sentence(self) -> None:
        lines = self._lines(variable_regime=STATED)
        assert [line for line in lines if " regime: " in line] == [
            "VARIABLES regime: stated -- the export carries no SWCraft inter-runnable variables marker and states "
            "variables or variable accesses, so what it states replaced the target's through the diff preview above"
        ]

    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_the_entry_is_consistent_with_its_scope(self, capability: Capability) -> None:
        """A refusing family names what the file cannot say; a runnable-scope one, who speaks."""
        assert (capability.cannot_say is not None) == (capability.on_unrecognized == "refuse")
        assert (capability.speaks is not None) == (capability.scope == "runnable")
        assert not (capability.states is not None and capability.scope == "runnable")

    def test_the_four_fields_that_serve_one_family_serve_exactly_it(self) -> None:
        assert [c.key for c in CAPABILITIES if c.replace_flag] == ["access_lists"]
        assert [c.key for c in CAPABILITIES if c.on_unrecognized == "unmarked"] == ["access_lists"]
        assert [c.key for c in CAPABILITIES if c.scope == "runnable"] == ["operation_events"]
        assert [c.key for c in CAPABILITIES if c.states is not None] == [
            "variables",
            "exclusive_areas",
            "data_events",
            "implicit_access",
            "asynchronous_calls",
        ]

    def test_the_module_never_imports_the_import_module(self) -> None:
        """``import_.py`` raises the refusal and builds the carry context; the registry imports neither."""
        tree = ast.parse(Path(capabilities.__file__).read_text(encoding="utf-8"))
        imported = {
            ("." * node.level) + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}

        assert not any(name.endswith("import_") for name in imported), sorted(imported)


class TestReadSignal:
    @pytest.mark.parametrize("flag", [False, True])
    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_the_writers_value_is_marked_whatever_the_flag(self, capability: Capability, flag: bool) -> None:
        model = _model({capability.marker.sd_gid: capability.marker.text})

        assert read_signal(capability, model, replace_flag_given=flag) == Signal(MARKED, None)

    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_no_marker_without_the_flag_is_carried(self, capability: Capability) -> None:
        assert read_signal(capability, _model({}), replace_flag_given=False) == Signal(CARRIED, None)

    @pytest.mark.parametrize(
        ("capability", "expected"),
        [
            (ACCESS_LISTS, REPLACE_FLAG),
            (OPERATION_EVENTS, CARRIED),
            (VARIABLES, CARRIED),
            (EXCLUSIVE_AREAS, CARRIED),
            (DATA_EVENTS, CARRIED),
            (IMPLICIT_ACCESS, CARRIED),
            (ASYNCHRONOUS_CALLS, CARRIED),
        ],
        ids=lambda v: v.key if isinstance(v, Capability) else v,
    )
    def test_no_marker_with_the_flag_selects_replace_flag_for_the_access_lists_alone(
        self, capability: Capability, expected: str
    ) -> None:
        assert read_signal(capability, _model({}), replace_flag_given=True) == Signal(expected, None)

    @pytest.mark.parametrize("flag", [False, True])
    @pytest.mark.parametrize("capability", [c for c in CAPABILITIES if c is not ACCESS_LISTS], ids=lambda c: c.key)
    def test_another_value_is_the_refusal_line_word_for_word(self, capability: Capability, flag: bool) -> None:
        model = _model({capability.marker.sd_gid: "bogus"})

        assert read_signal(capability, model, replace_flag_given=flag) == _REFUSALS[capability.key]

    @pytest.mark.parametrize(("flag", "expected"), [(False, CARRIED), (True, REPLACE_FLAG)])
    def test_another_access_value_is_read_as_unmarked_and_kept(self, flag: bool, expected: str) -> None:
        model = _model({"ACCESS-POINTS": "implicit"})

        assert read_signal(ACCESS_LISTS, model, replace_flag_given=flag) == Signal(expected, "implicit")

    def test_each_family_reads_its_own_sd_alone(self) -> None:
        """A marker of another family is no marker of this one."""
        markers = {c.marker.sd_gid: c.marker.text for c in CAPABILITIES if c is not VARIABLES}

        assert read_signal(VARIABLES, _model(markers), replace_flag_given=False) == Signal(CARRIED, None)


class TestDecide:
    @pytest.mark.parametrize("stating", [False, True])
    @pytest.mark.parametrize("capability", _THREE_STATE, ids=lambda c: c.key)
    def test_a_three_state_family_under_marked_is_compared_and_marked(
        self, capability: Capability, stating: bool
    ) -> None:
        incoming = _STATING[capability.key][0] if stating else _SILENT
        signal = Signal(MARKED, None)

        assert decide(capability, signal, incoming) == Decision(signal, True, frozenset(), MARKED)

    @pytest.mark.parametrize(
        ("capability", "index"),
        [(c, i) for c in _THREE_STATE for i in range(len(_STATING[c.key]))],
        ids=lambda v: v.key if isinstance(v, Capability) else str(v),
    )
    def test_a_three_state_family_unmarked_that_states_is_compared_and_stated(
        self, capability: Capability, index: int
    ) -> None:
        signal = Signal(CARRIED, None)

        assert decide(capability, signal, _STATING[capability.key][index]) == Decision(
            signal, True, frozenset(), STATED
        )

    @pytest.mark.parametrize("capability", _THREE_STATE, ids=lambda c: c.key)
    def test_a_three_state_family_unmarked_that_states_none_is_carried(self, capability: Capability) -> None:
        signal = Signal(CARRIED, None)

        assert decide(capability, signal, _SILENT) == Decision(signal, False, frozenset(), CARRIED)

    @pytest.mark.parametrize("capability", _THREE_STATE, ids=lambda c: c.key)
    def test_another_familys_content_is_no_statement(self, capability: Capability) -> None:
        """What one family states never makes another one ``stated``."""
        others = [c for key, cs in _STATING.items() if key != capability.key for c in cs]
        signal = Signal(CARRIED, None)

        assert {decide(capability, signal, other).regime for other in others} == {CARRIED}

    @pytest.mark.parametrize("capability", [IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS], ids=lambda c: c.key)
    def test_a_by_value_read_and_a_synchronous_call_state_neither_new_family(self, capability: Capability) -> None:
        """What an export before contract 12.7.0 could write is no statement of the forms or the synchrony."""
        older = _component(
            runnables=[
                {
                    "name": "Old",
                    "reads": [{"port": "In", "element": "value", "by": "value"}],
                    "writes": [{"port": "Out", "element": "value"}],
                    "calls": [{"port": "Cli", "operation": "Get"}],
                }
            ]
        )
        signal = Signal(CARRIED, None)

        assert decide(capability, signal, older).regime == CARRIED

    @pytest.mark.parametrize(
        ("signal", "compared"),
        [(Signal(MARKED, None), True), (Signal(REPLACE_FLAG, None), True), (Signal(CARRIED, None), False)],
        ids=lambda v: v.regime if isinstance(v, Signal) else str(v),
    )
    def test_the_access_lists_are_two_state(self, signal: Signal, compared: bool) -> None:
        """No ``states``: a component's own access lists never make them ``stated``."""
        incoming = _component(runnables=[{"name": "Reader", "reads": [{"port": "In", "element": "value"}]}])

        assert decide(ACCESS_LISTS, signal, incoming) == Decision(signal, compared, frozenset(), signal.regime)

    def test_an_unrecognized_access_value_rides_on_the_decision(self) -> None:
        signal = Signal(CARRIED, "implicit")

        assert decide(ACCESS_LISTS, signal, _SILENT).signal.unrecognized == "implicit"

    _SPEAKING = _component(
        runnables=[
            {"name": "Server", "events": [{"type": "operation", "port": "Srv", "operation": "Get"}]},
            {"name": "Parallel", "concurrent": True},
            {"name": "OnData", "events": [{"type": "data", "port": "In", "element": "value"}]},
            {"name": "Quiet"},
        ]
    )

    @pytest.mark.parametrize(("value", "compared"), [(MARKED, True), (CARRIED, False)])
    def test_the_operation_events_are_decided_per_runnable(self, value: str, compared: bool) -> None:
        """Under either signal the runnables that speak are named; under ``marked`` every one is compared."""
        signal = Signal(value, None)

        assert decide(OPERATION_EVENTS, signal, self._SPEAKING) == Decision(
            signal, compared, frozenset({"Server", "Parallel"}), value
        )

    def test_a_silent_component_has_no_speaking_runnable(self) -> None:
        signal = Signal(CARRIED, None)

        assert decide(OPERATION_EVENTS, signal, _SILENT) == Decision(signal, False, frozenset(), CARRIED)


# The projection

_TIMING = {"type": "timing", "period": 0.1}
_OPERATION = {"type": "operation", "port": "Srv", "operation": "Reset"}
_DATA = {"type": "data", "port": "In", "element": "value"}
_RETURNED = {"type": "call_returned", "port": "Cli", "operation": "Reset"}

#: A runnable carrying every kind a family projects, each list out of its sorted order.
#: The implicit form is on the one write, so the access lists' sort and the form's
#: projection never meet on one list in ``test_the_mechanism_applies_every_family_in_one_projection``;
#: an implicit read's projection is its own case below.
_BUSY: dict[str, Any] = {
    "name": "Busy",
    "concurrent": True,
    "events": [_TIMING, _OPERATION, _DATA, _RETURNED],
    "reads": [{"port": "In", "element": "value"}, {"port": "In", "element": "other"}],
    "writes": [{"port": "Out", "element": "value", "by": "implicit"}],
    "calls": [{"port": "Cli", "operation": "Reset", "asynchronous": True, "poll": True, "timeout": 0.5}],
    "reads_variables": [{"variable": "Zeta"}, {"variable": "Alpha"}],
    "writes_variables": [{"variable": "Yota"}, {"variable": "Beta"}],
    "can_enter": [{"area": "Guard"}],
    "runs_inside": [{"area": "Inner"}],
}


def _runnable(**fields: Any) -> Runnable:
    return Runnable.model_validate({**_BUSY, **fields})


#: Per family and per ``compared``: the hand-written projection of :data:`_BUSY`.
_PROJECTED: dict[tuple[str, bool], dict[str, Any]] = {
    ("access_lists", True): {
        "reads": [{"port": "In", "element": "other"}, {"port": "In", "element": "value"}],
    },
    ("access_lists", False): {"reads": [], "writes": [], "calls": []},
    ("operation_events", True): {},
    ("operation_events", False): {"events": [_TIMING, _DATA, _RETURNED], "concurrent": False},
    ("variables", True): {
        "reads_variables": [{"variable": "Alpha"}, {"variable": "Zeta"}],
        "writes_variables": [{"variable": "Beta"}, {"variable": "Yota"}],
    },
    ("variables", False): {"reads_variables": [], "writes_variables": []},
    ("exclusive_areas", True): {},
    ("exclusive_areas", False): {"can_enter": [], "runs_inside": []},
    ("data_events", True): {},
    ("data_events", False): {"events": [_TIMING, _OPERATION, _RETURNED]},
    ("implicit_access", True): {},
    ("implicit_access", False): {"writes": [{"port": "Out", "element": "value"}]},
    ("asynchronous_calls", True): {},
    ("asynchronous_calls", False): {
        "calls": [{"port": "Cli", "operation": "Reset"}],
        "events": [_TIMING, _OPERATION, _DATA],
    },
}


def _decisions(**compared: bool) -> dict[str, Decision]:
    """One decision per family, compared unless *compared* says otherwise."""
    return {c.key: Decision(Signal(MARKED, None), compared.get(c.key, True), frozenset(), MARKED) for c in CAPABILITIES}


class TestTheProjection:
    @pytest.mark.parametrize("compared", [True, False])
    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_each_family_projects_its_own_fields_alone(self, capability: Capability, compared: bool) -> None:
        """Each hook's update, alone over :data:`_BUSY` (no family before it set ``events``)."""
        update = capability.project_runnable(_runnable(), compared, {})

        assert _runnable().model_copy(update=update) == _runnable(**_PROJECTED[(capability.key, compared)])

    def test_an_event_family_filters_the_events_the_one_before_it_set(self) -> None:
        """The two event filters compose: each reads ``events`` from the update built so far."""
        update = DATA_EVENTS.project_runnable(_runnable(), False, {"events": _runnable(events=[_TIMING, _DATA]).events})

        assert update == {"events": _runnable(events=[_TIMING]).events}

    def test_the_default_compares_every_family_but_the_access_lists(self) -> None:
        assert {c.key: c.default_compared for c in CAPABILITIES} == {
            "access_lists": False,
            "operation_events": True,
            "variables": True,
            "exclusive_areas": True,
            "data_events": True,
            "implicit_access": True,
            "asynchronous_calls": True,
        }

    def test_an_implicit_read_is_projected_explicit_and_a_by_value_read_kept(self) -> None:
        runnable = _runnable(
            reads=[
                {"port": "In", "element": "value", "by": "implicit"},
                {"port": "In", "element": "other", "by": "value"},
            ]
        )

        projected = project_runnable(runnable, _decisions(implicit_access=False))

        assert [(r.element, r.by) for r in projected.reads] == [("other", "value"), ("value", None)]
        assert [(r.element, r.by) for r in project_runnable(runnable, _decisions()).reads] == [
            ("other", "value"),
            ("value", "implicit"),
        ]

    def test_the_projection_is_one_model_copy_with_the_caller_s_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []
        real = Runnable.model_copy

        def counted(self: Runnable, *, update: dict[str, Any] | None = None, deep: bool = False) -> Runnable:
            calls.append(dict(update or {}))
            return real(self, update=update, deep=deep)

        monkeypatch.setattr(Runnable, "model_copy", counted)
        projected = project_runnable(_runnable(), _decisions(), {"satisfies": []})

        assert len(calls) == 1
        assert "satisfies" in calls[0]
        assert projected.satisfies == []

    @pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
    def test_the_mechanism_applies_every_family_in_one_projection(self, capability: Capability) -> None:
        """Every family compared but *capability*: its fields left out, every other's compared."""
        expected: dict[str, Any] = {}
        for other in CAPABILITIES:
            if other is not capability:
                expected.update(_PROJECTED[(other.key, True)])
        expected.update(_PROJECTED[(capability.key, False)])

        assert project_runnable(_runnable(), _decisions(**{capability.key: False})) == _runnable(**expected)

    def test_the_three_event_filters_together_keep_the_timing_event_alone(self) -> None:
        projected = project_runnable(
            _runnable(), _decisions(operation_events=False, data_events=False, asynchronous_calls=False)
        )

        assert projected.events == _runnable(events=[_TIMING]).events
        assert projected.concurrent is False

    def test_a_speaking_runnable_is_compared_under_carried(self) -> None:
        decisions = _decisions()
        decisions[OPERATION_EVENTS.key] = Decision(Signal(CARRIED, None), False, frozenset({"Busy"}), CARRIED)

        assert project_runnable(_runnable(), decisions).events == _runnable().events
        assert (
            project_runnable(_runnable(name="Other"), decisions).events
            == _runnable(events=[_TIMING, _DATA, _RETURNED]).events
        )

    @pytest.mark.parametrize("compared", [True, False])
    def test_the_component_level_fields(self, compared: bool) -> None:
        swc = _component(
            variables=[
                {"name": "Zeta", "access": "explicit", "implementation_type": "uint8"},
                {"name": "Alpha", "access": "explicit", "implementation_type": "uint8"},
            ],
            exclusive_areas=[{"name": "Inner"}, {"name": "Guard"}],
        )

        projected = project_swc(swc, _decisions(variables=compared, exclusive_areas=compared))

        assert set(projected) == {"variables", "exclusive_areas"}
        assert [v.name for v in projected["variables"]] == (["Alpha", "Zeta"] if compared else [])
        assert [a.name for a in projected["exclusive_areas"]] == (["Inner", "Guard"] if compared else [])

    def test_the_default_projection_compares_every_family_but_the_access_lists(self) -> None:
        """``projected_swc`` with no decisions: today's default, written out by hand."""
        swc = _component(
            runnables=[_BUSY],
            variables=[
                {"name": "Zeta", "access": "explicit", "implementation_type": "uint8"},
                {"name": "Alpha", "access": "explicit", "implementation_type": "uint8"},
            ],
            exclusive_areas=[{"name": "Inner"}, {"name": "Guard"}],
        )
        expected = swc.model_copy(
            update={
                "runnables": [
                    _runnable(
                        reads=[],
                        writes=[],
                        calls=[],
                        reads_variables=[{"variable": "Alpha"}, {"variable": "Zeta"}],
                        writes_variables=[{"variable": "Beta"}, {"variable": "Yota"}],
                    )
                ],
                "variables": [swc.variables[1], swc.variables[0]],
            }
        )

        assert projected_swc(swc) == expected
        assert projected_swc(swc) == projected_swc(swc, _decisions(access_lists=False))


# The carry


class TestTheCarry:
    def test_the_groups_and_their_order(self) -> None:
        assert [[c.key for c in group.families] for group in CARRY_GROUPS] == [
            ["access_lists"],
            ["operation_events", "data_events", "asynchronous_calls"],
            ["variables"],
            ["exclusive_areas"],
            ["implicit_access"],
            ["asynchronous_calls"],
        ]
        assert {c.key for group in CARRY_GROUPS for c in group.families} == {c.key for c in CAPABILITIES}

    def test_every_group_has_a_hook_of_its_own(self) -> None:
        assert len({group.hook for group in CARRY_GROUPS}) == len(CARRY_GROUPS)

    @staticmethod
    def _carried(*keys: str) -> dict[str, Decision]:
        """``carried`` for every family of *keys*, ``marked`` for every other."""
        carried = Decision(Signal(CARRIED, None), False, frozenset(), CARRIED)
        marked = Decision(Signal(MARKED, None), True, frozenset(), MARKED)
        return {c.key: carried if c.key in keys else marked for c in CAPABILITIES}

    def test_the_asynchronous_calls_family_is_in_the_events_group_and_its_own(self) -> None:
        """One family in two groups (the registry's mechanism, no extension).

        Its events join the one traversal, its synchrony takes a hook of its own after the access lists' carry.
        """
        assert [i for i, group in enumerate(CARRY_GROUPS) if ASYNCHRONOUS_CALLS in group.families] == [1, 5]

    @pytest.mark.parametrize(
        "keys",
        [
            ("operation_events", "data_events", "asynchronous_calls"),
            ("operation_events", "data_events"),
            ("operation_events",),
            ("data_events",),
            ("asynchronous_calls",),
        ],
        ids="+".join,
    )
    def test_the_events_group_runs_once_and_the_collision_refusal_after_it(
        self, monkeypatch: pytest.MonkeyPatch, keys: tuple[str, ...]
    ) -> None:
        log: list[Any] = []
        real = capabilities._carry_target_events

        def traversal(*args: Any, **kwargs: Any) -> Any:
            log.append(("events", kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(capabilities, "_carry_target_events", traversal)
        existing = _component(runnables=[{"name": "Busy", "events": [_TIMING, _OPERATION, _DATA, _RETURNED]}])
        incoming = _component(runnables=[{"name": "Busy", "events": [{"type": "timing", "period": 0.2}]}])
        ctx = CarryContext(mapping_sets=dict, refuse_event_collisions=lambda swc: log.append(("refuse", swc)))

        swc, fields = carry(self._carried(*keys), existing, incoming, ctx)

        assert [entry[0] for entry in log] == ["events", "refuse"]
        assert log[0][1] == {
            "operation_events": "operation_events" in keys,
            "data_events": "data_events" in keys,
            "call_returned_events": "asynchronous_calls" in keys,
        }
        assert log[1][1] is swc, "the refusal judges the component the traversal built"
        kinds = [e.type for e in swc.runnables[0].events]
        assert kinds == [
            "timing",
            *(["operation"] if "operation_events" in keys else []),
            *(["data"] if "data_events" in keys else []),
            *(["call_returned"] if "asynchronous_calls" in keys else []),
        ]
        assert set(fields) >= {"preserved_operation_events", "preserved_data_events", "preserved_call_returned_events"}

    def test_no_carried_family_runs_no_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(swc: SWComponent) -> None:
            raise AssertionError("no events group ran")

        incoming = _component(runnables=[{"name": "Busy"}])
        ctx = CarryContext(mapping_sets=dict, refuse_event_collisions=refuse)

        assert carry(self._carried(), _component(runnables=[_BUSY]), incoming, ctx) == (incoming, {})

    def test_the_fields_of_every_group_are_merged(self) -> None:
        existing = _component(
            runnables=[_BUSY],
            variables=[{"name": "Alpha", "access": "explicit", "implementation_type": "uint8"}],
            exclusive_areas=[{"name": "Guard"}],
        )
        incoming = _component(runnables=[{"name": "Busy"}])
        ctx = CarryContext(mapping_sets=dict, refuse_event_collisions=lambda swc: None)

        _swc, fields = carry(self._carried(*(c.key for c in CAPABILITIES)), existing, incoming, ctx)

        assert fields == {
            "preserved_access_lists": {"Busy": {"reads": 2, "writes": 1, "calls": 1}},
            "preserved_operation_events": {"Busy": {"operation_events": 1, "concurrent": True}},
            "preserved_data_events": {"Busy": 1},
            "preserved_call_returned_events": {"Busy": 1},
            "preserved_variables": ["Alpha"],
            "preserved_variable_accesses": {"Busy": {"reads_variables": 2, "writes_variables": 2}},
            "preserved_variable_mappings": [],
            "preserved_exclusive_areas": ["Guard"],
            "preserved_exclusive_area_references": {"Busy": {"can_enter": 1, "runs_inside": 1}},
            # The access lists' carry brought the target's entries whole, forms and synchrony
            # included, so the implicit-access and asynchronous-call hooks found nothing to carry.
            "preserved_implicit_accesses": {},
            "preserved_asynchronous_calls": {},
        }

    def test_the_implicit_form_and_the_synchrony_are_carried_onto_the_file_s_own_entries(self) -> None:
        """Under a marked access list the file's entries stand.

        The two hooks give them the target's form and synchrony, pair by pair, and leave a stated ``by: value``
        alone.
        """
        existing = _component(
            runnables=[
                {
                    "name": "Busy",
                    "reads": [
                        {"port": "In", "element": "value", "by": "implicit"},
                        {"port": "In", "element": "other", "by": "implicit"},
                    ],
                    "writes": [{"port": "Out", "element": "value", "by": "implicit"}],
                    "calls": [
                        {"port": "Cli", "operation": "Reset", "asynchronous": True, "result": None, "poll": True}
                    ],
                }
            ]
        )
        incoming = _component(
            runnables=[
                {
                    "name": "Busy",
                    "reads": [{"port": "In", "element": "value"}, {"port": "In", "element": "other", "by": "value"}],
                    "writes": [{"port": "Out", "element": "value"}],
                    "calls": [{"port": "Cli", "operation": "Reset"}, {"port": "Cli", "operation": "Get"}],
                }
            ]
        )
        decisions = self._carried("implicit_access", "asynchronous_calls")
        ctx = CarryContext(mapping_sets=dict, refuse_event_collisions=lambda swc: None)

        swc, fields = carry(decisions, existing, incoming, ctx)

        (busy,) = swc.runnables
        assert [(r.element, r.by) for r in busy.reads] == [("value", "implicit"), ("other", "value")]
        assert [w.by for w in busy.writes] == ["implicit"]
        assert [(c.operation, c.asynchronous, c.poll) for c in busy.calls] == [
            ("Reset", True, True),
            ("Get", False, False),
        ]
        assert fields["preserved_implicit_accesses"] == {"Busy": {"reads": 1, "writes": 1}}
        assert fields["preserved_asynchronous_calls"] == {"Busy": 1}
