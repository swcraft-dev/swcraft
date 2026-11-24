"""Stated names: every site derives from the emitted names, and the name rules judge them.

Contract ``8.0.0`` lets a component state its internal behaviour's name and an
event, a read, a write or a call state its own. The name **written** is the stated
one where it is set, else the derived one -- and the event counter runs over every
event, so a stated event keeps its slot
and moves no other event's derived name. One definition,
``validator.emitted_behavior_name`` / ``emitted_event_names`` /
``emitted_access_point_names``, read by every site that judges a name:

* ``rule:name.name-space-collision`` -- a stated behaviour name against the
  component's ports, a stated event name against another event's name ignoring
  case, a stated access point name inside its runnable;
* ``rule:name.runnable-event-collision`` -- a runnable against the events as
  written;
* ``rule:name.duplicate-event`` -- two events written with one name,
  exactly; reachable only through a stated name;
* ``rule:swc.access.name-collision`` -- two entries of one runnable written with
  one name;
* ``rule:identifier.declared`` judges a stated name and ``rule:identifier.generated``
  judges a derived form **only where no name is stated** -- in ``check-model``,
  pass D, the writer's guard, the write path's guard and the component import's.

Every stated name here is invented. What this module does **not** reach: nothing
here reads a generated file for a stated name (``test_stated_names_writer.py``), and
what the importers state is ``test_stated_names_import.py``'s.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.editing.registry as reg
import validator_package
import yaml
from conftest import REPO_ROOT
from external_stubs_support import change, component_stub_ports_model_root
from model_contract_fixtures import load_binding, load_contract
from swcraft_tools.component_exchange import ReservedNameError
from swcraft_tools.component_exchange.import_ import _refuse_runnable_event_collisions
from swcraft_tools.component_exchange.name_policies import access_point_collisions, illegal_identifiers
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.address import FieldStep, MemberStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES, emitted_identifier_violations
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_identifier_conformance,
    check_name_space_collisions,
    check_name_uniqueness,
    derived_event_names,
    duplicate_event_names,
    emitted_access_point_names,
    emitted_behavior_name,
    emitted_event_names,
    names,
    runnable_event_collisions,
)
from swcraft_tools.writer.arxml_generation import (
    _check_component_names,
    _event_base_name,
    _unique_event_names,
    generate_arxml,
)
from swcraft_tools.writer.elements import write_element
from swcraft_tools.writer.mutations import ReservedNameError as WriterReservedNameError
from swcraft_tools.writer.mutations import check_runnable_access_names

BASIC = REPO_ROOT / "examples" / "basic"
PROVIDER = "swc/example/ExampleSWC.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
OWNER = Subject("swc", "ExampleSWC")

#: A legal Identifier one character over the limit: what a stated name may not be.
TOO_LONG = "N" * (MAX_IDENTIFIER_LENGTH + 1)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, *mutations: tuple[str, Callable[[dict[str, Any]], None]]) -> Path:
    """``examples/basic``'s model, each ``(file, mutation)`` applied to the file's YAML."""
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    for relative, mutation in mutations:
        path = project / "model" / relative
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutation(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return project / "model"


def _rules(records: Any, rule: str) -> list[Any]:
    return [record for record in records if record.rule == rule]


def _component(**fields: Any) -> SWComponent:
    """A component ``S`` with one provide port ``p`` and the given fields, for the predicates alone."""
    document: dict[str, Any] = {
        "name": "S",
        "type": "application",
        "ports": [{"name": "p", "direction": "provide", "interface": "I"}],
    }
    document.update(fields)
    return SWComponent.model_validate(document)


def _two_timing_events(stated: str | None = None, *, index: int = 1) -> SWComponent:
    """Runnable ``R`` with two timing events, the one at *index* stating *stated*."""
    events: list[dict[str, Any]] = [{"type": "timing", "period": 0.01}, {"type": "timing", "period": 0.02}]
    if stated is not None:
        events[index]["name"] = stated
    return _component(runnables=[{"name": "R", "events": events}])


def _event_subject(runnable: str, identity: dict[str, Any]) -> Subject:
    return OWNER.at(field_step("runnables"), member_step(runnable), field_step("events"), member_step(identity))


# ---------------------------------------------------------------------------
# The emitted names
# ---------------------------------------------------------------------------


class TestTheEmittedNames:
    def test_a_stated_event_keeps_its_slot_in_the_counter(self) -> None:
        """Stating the second event's name moves the first event's derived name nowhere."""
        unstated, stated = _two_timing_events(), _two_timing_events("Tick")

        assert derived_event_names(unstated) == ["TMT_R_0", "TMT_R_1"]
        assert derived_event_names(stated) == ["TMT_R_0", "TMT_R_1"]
        assert emitted_event_names(stated) == ["TMT_R_0", "Tick"]

    def test_a_stated_name_is_never_suffixed(self) -> None:
        """A stated name equal to the counter's base is written as it is, not given a ``_0``."""
        component = _two_timing_events("TMT_R", index=0)

        assert emitted_event_names(component) == ["TMT_R", "TMT_R_1"]

    def test_the_derived_names_are_the_writers_counter(self) -> None:
        """The counter ``check-model`` reads is the emitter's, stated names or not."""
        component = _two_timing_events("Tick")
        pairs = [(runnable, event) for runnable in component.runnables for event in runnable.events]

        assert derived_event_names(component) == _unique_event_names(
            [_event_base_name(runnable, event) for runnable, event in pairs]
        )

    def test_the_behaviour_name(self) -> None:
        assert emitted_behavior_name(_component()) == "S_InternalBehavior"
        assert emitted_behavior_name(_component(behavior_name="Behaviour")) == "Behaviour"

    def test_the_access_point_names(self) -> None:
        component = _component(
            runnables=[
                {
                    "name": "R",
                    "reads": [{"port": "p", "element": "a"}, {"port": "p", "element": "b", "name": "ReadB"}],
                    "calls": [{"port": "p", "operation": "op"}],
                }
            ]
        )

        assert emitted_access_point_names(component.runnables[0]) == ["R_p_a", "ReadB", "Call_p_op"]


# ---------------------------------------------------------------------------
# rule:name.name-space-collision
# ---------------------------------------------------------------------------


def _set_behaviour(name: str) -> Callable[[dict[str, Any]], None]:
    def mutate(swc: dict[str, Any]) -> None:
        swc["behavior_name"] = name

    return mutate


def _second_timing_event(stated: str) -> Callable[[dict[str, Any]], None]:
    """ExampleRun gains a second timing event stating *stated*; the first derives ``TMT_ExampleRun_0``."""

    def mutate(swc: dict[str, Any]) -> None:
        swc["runnables"][0]["events"].append({"type": "timing", "period": 0.02, "name": stated})

    return mutate


class TestTheNameSpaceRule:
    @pytest.mark.parametrize("stated", ["POUT", "pOut"], ids=["case-only", "exact"])
    def test_a_stated_behaviour_name_against_a_port(self, stated: str, tmp_path: Path) -> None:
        """The behaviour shares the component's name space with its ports: case-only and exact alike."""
        model = _project(tmp_path, (PROVIDER, _set_behaviour(stated)))

        (finding,) = _rules(check_name_space_collisions(str(model)).records, "name.name-space-collision")

        assert f"'{stated}' (behavior_name)" in finding.message
        assert "'pOut' (ports)" in finding.message
        assert OWNER.at(field_step("behavior_name")) in finding.subjects

    def test_a_stated_event_name_against_another_event_ignoring_case(self, tmp_path: Path) -> None:
        model = _project(tmp_path, (PROVIDER, _second_timing_event("tmt_examplerun_0")))

        (finding,) = _rules(check_name_space_collisions(str(model)).records, "name.name-space-collision")

        assert "'TMT_ExampleRun_0' (events (generated))" in finding.message
        assert "'tmt_examplerun_0' (events)" in finding.message
        assert _event_subject("ExampleRun", {"type": "timing", "period": 0.02, "name": "tmt_examplerun_0"}) in (
            finding.subjects
        )
        # Case-only is this rule's; the exact rule is silent.
        assert _rules(check_name_uniqueness(str(model)).records, "name.duplicate-event") == []

    def test_a_stated_access_point_name_against_another_ignoring_case(self, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["ports"].append({"name": "rIn2", "direction": "require", "interface": "IExample"})
            swc["runnables"][0]["reads"] = [
                {"port": "rIn", "element": "value"},
                {"port": "rIn2", "element": "value", "name": "r_rin_value"},
            ]

        model = _project(tmp_path, (CONSUMER, mutate))

        (finding,) = _rules(check_name_space_collisions(str(model)).records, "name.name-space-collision")

        assert "'R_rIn_value' (reads)" in finding.message
        assert "'r_rin_value' (reads)" in finding.message
        assert _rules(check_name_uniqueness(str(model)).records, "swc.access.name-collision") == []

    def test_the_unmodified_example_has_no_finding(self, tmp_path: Path) -> None:
        assert check_name_space_collisions(str(_project(tmp_path))).consistent


# ---------------------------------------------------------------------------
# rule:name.runnable-event-collision
# ---------------------------------------------------------------------------


class TestTheRunnableEventRule:
    def test_the_writers_guard_says_written_for_a_stated_event_name(self) -> None:
        """The writer's header names the events as written, a stated one included."""
        component = _component(runnables=[{"name": "R", "events": [{"type": "timing", "period": 0.01, "name": "R"}]}])

        with pytest.raises(ValueError, match="collide with event SHORT-NAMEs as they are written") as caught:
            _check_component_names([component])
        assert "generated event" not in str(caught.value)

    @pytest.mark.parametrize("runnable", ["Other", "other"], ids=["exact", "case-only"])
    def test_a_stated_event_name_against_a_runnable(self, runnable: str, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["runnables"][0]["events"][0]["name"] = "Other"
            swc["runnables"].append({"name": runnable})

        model = _project(tmp_path, (PROVIDER, mutate))

        (finding,) = _rules(check_name_uniqueness(str(model)).records, "name.runnable-event-collision")

        assert "TIMING-EVENT 'Other' stated on runnable 'ExampleRun'" in finding.message
        assert f"rename runnable '{runnable}' or change the event's stated name" in finding.message
        assert finding.subjects == (OWNER.at(field_step("runnables"), member_step(runnable)),)
        # Every runnable-against-event pair is this rule's alone.
        assert check_name_space_collisions(str(model)).consistent

    def test_a_stated_name_frees_the_derived_one(self) -> None:
        """A runnable ``TMT_R`` beside R's one timing event collides until the event states another name."""
        runnables = [{"name": "R", "events": [{"type": "timing", "period": 0.01}]}, {"name": "TMT_R"}]
        stated = [{"name": "R", "events": [{"type": "timing", "period": 0.01, "name": "Tick"}]}, {"name": "TMT_R"}]

        assert len(runnable_event_collisions(_component(runnables=runnables))) == 1
        assert runnable_event_collisions(_component(runnables=stated)) == []


# ---------------------------------------------------------------------------
# rule:name.duplicate-event
# ---------------------------------------------------------------------------


class TestTheDuplicateEventRule:
    def test_the_rule_is_declared_bound_and_diagnosed(self) -> None:
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:name.duplicate-event"]

        assert rule["family"] == "cross-element"
        assert rule["summary"] == CONSISTENCY_RULES["name.duplicate-event"]
        # The phase of `check_name_uniqueness` that mints it, which the check calls.
        assert load_binding()["rules"]["rule:name.duplicate-event"] == {
            "module": "swcraft_tools.validator.names",
            "callable": "_component_member_name_records",
        }
        assert "_component_member_name_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert RULE_CODES["name.duplicate-event"].code == CONSISTENCY_VIOLATION

    def test_two_stated_names_equal_exactly(self, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["runnables"][0]["events"][0]["name"] = "Tick"
            swc["runnables"].append({"name": "Other", "events": [{"type": "init", "name": "Tick"}]})

        model = _project(tmp_path, (PROVIDER, mutate))

        (finding,) = _rules(check_name_uniqueness(str(model)).records, "name.duplicate-event")

        assert "writes the event SHORT-NAME 'Tick' for more than one event" in finding.message
        assert "TIMING-EVENT 'Tick' stated on runnable 'ExampleRun'" in finding.message
        assert "INIT-EVENT 'Tick' stated on runnable 'Other'" in finding.message
        assert finding.subjects == (
            _event_subject("ExampleRun", {"type": "timing", "period": 0.01, "name": "Tick"}),
            _event_subject("Other", {"type": "init", "name": "Tick"}),
        )
        # An exact repeat inside EVENTS is not the name-space rule's.
        assert check_name_space_collisions(str(model)).consistent

    def test_a_stated_name_equal_to_another_events_derived_name(self, tmp_path: Path) -> None:
        model = _project(tmp_path, (PROVIDER, _second_timing_event("TMT_ExampleRun_0")))

        (finding,) = _rules(check_name_uniqueness(str(model)).records, "name.duplicate-event")

        assert "TIMING-EVENT 'TMT_ExampleRun_0' the writer generates for runnable 'ExampleRun'" in finding.message
        assert "TIMING-EVENT 'TMT_ExampleRun_0' stated on runnable 'ExampleRun'" in finding.message

    def test_silent_on_a_case_only_pair(self) -> None:
        assert duplicate_event_names(_two_timing_events("tmt_r_0")) == []

    def test_silent_where_nothing_is_stated(self, tmp_path: Path) -> None:
        assert duplicate_event_names(_two_timing_events()) == []
        assert _rules(check_name_uniqueness(str(_project(tmp_path))).records, "name.duplicate-event") == []

    def test_an_external_stub_is_not_judged(self) -> None:
        stub = SWComponent.model_validate(
            {
                "name": "Stub",
                "type": "application",
                "external": True,
                "external_ref": "/Foreign/Stub",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "runnables": [
                    {
                        "name": "R",
                        "events": [{"type": "init", "name": "X"}, {"type": "timing", "period": 1.0, "name": "X"}],
                    }
                ],
            }
        )

        assert duplicate_event_names(stub) == []

    def test_the_writer_refuses_it_before_writing(self, tmp_path: Path) -> None:
        model = _project(tmp_path, (PROVIDER, _second_timing_event("TMT_ExampleRun_0")))
        output = tmp_path / "out" / "model.arxml"
        (finding,) = _rules(check_name_uniqueness(str(model)).records, "name.duplicate-event")

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output))

        assert finding.message in str(caught.value)
        assert not output.exists()

    def test_the_component_import_refuses_it(self) -> None:
        with pytest.raises(ReservedNameError, match="two events of one component would be written with one SHORT-NAME"):
            _refuse_runnable_event_collisions(_two_timing_events("TMT_R_0"))


# ---------------------------------------------------------------------------
# rule:swc.access.name-collision
# ---------------------------------------------------------------------------


def _reads(*entries: dict[str, Any]) -> SWComponent:
    return _component(runnables=[{"name": "R", "reads": list(entries)}])


class TestTheAccessCollisionRule:
    def test_a_stated_name_equal_to_another_entrys_derived_name(self, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["ports"].append({"name": "rIn2", "direction": "require", "interface": "IExample"})
            swc["runnables"][0]["reads"] = [
                {"port": "rIn", "element": "value"},
                {"port": "rIn2", "element": "value", "name": "R_rIn_value"},
            ]

        model = _project(tmp_path, (CONSUMER, mutate))

        (finding,) = _rules(check_name_uniqueness(str(model)).records, "swc.access.name-collision")

        assert "emits access point SHORT-NAME 'R_rIn_value' for more than one access entry" in finding.message
        assert "reads port 'rIn' element 'value' (derived)" in finding.message
        assert "reads port 'rIn2' element 'value' (stated)" in finding.message

    def test_a_stated_name_frees_the_derived_one(self) -> None:
        """``(a_b, c)`` and ``(a, b_c)`` derive one name until one of them states another."""
        colliding = _reads({"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"})
        stated = _reads({"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c", "name": "Other"})

        with pytest.raises(WriterReservedNameError):
            check_runnable_access_names("S", colliding.runnables[0])
        check_runnable_access_names("S", stated.runnables[0])
        assert access_point_collisions(stated) == []
        assert len(access_point_collisions(colliding)) == 1

    def test_the_write_path_and_the_component_import_refuse_a_stated_repeat(self) -> None:
        component = _reads({"port": "p", "element": "a"}, {"port": "p", "element": "b", "name": "R_p_a"})

        with pytest.raises(WriterReservedNameError, match="'R_p_a' is written for") as caught:
            check_runnable_access_names("S", component.runnables[0])
        assert "declares access entries that are written with one access point SHORT-NAME" in str(caught.value)
        (line,) = access_point_collisions(component)
        assert "'R_p_a' is written for" in line

    def test_the_writers_guard_says_written_in_its_header_too(self) -> None:
        """The header and the clause agree: a stated name is *written*, never *derived*."""
        component = _reads({"port": "p", "element": "a"}, {"port": "p", "element": "b", "name": "R_p_a"})

        with pytest.raises(ValueError, match="'R_p_a' is written for") as caught:
            _check_component_names([component])
        assert "access entries of one runnable in swc/ are written with one access point SHORT-NAME" in str(
            caught.value
        )
        assert "derive one" not in str(caught.value)

    def test_the_writers_two_guards_print_the_rules_repair_by_the_stated_flag(self) -> None:
        """A stated name in the pair drops the injectivity sentence, as ``check-model``'s does."""
        stated = _reads({"port": "p", "element": "a"}, {"port": "p", "element": "b", "name": "R_p_a"})
        derived = _reads({"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"})

        for component, present, absent in (
            (stated, "Change a stated name, or rename one of the conflicting names.", "not injective"),
            (
                derived,
                "so the derivation is not injective. Rename one of the conflicting names, or state an entry's name.",
                "stated name",
            ),
        ):
            with pytest.raises(WriterReservedNameError) as write_path:
                check_runnable_access_names("S", component.runnables[0])
            with pytest.raises(ValueError) as writer:
                _check_component_names([component])
            for message in (str(write_path.value), str(writer.value)):
                assert present in message
                assert absent not in message


# ---------------------------------------------------------------------------
# rule:identifier.declared and rule:identifier.generated
# ---------------------------------------------------------------------------


def _long_runnable_with_timing_event(stated: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "timing", "period": 0.01}
    if stated is not None:
        event["name"] = stated
    # `TMT_` + 125 characters = 129: the derived form is one over the limit.
    return {"name": "R" * 125, "events": [event]}


class TestTheIdentifierRules:
    def test_a_stated_behaviour_name_over_the_limit(self, tmp_path: Path) -> None:
        model = _project(tmp_path, (PROVIDER, _set_behaviour(TOO_LONG)))

        records = check_identifier_conformance(str(model)).records

        (finding,) = _rules(records, "identifier.declared")
        assert finding.subjects == (OWNER.at(field_step("behavior_name")),)
        assert _rules(records, "identifier.generated") == []

    def test_a_stated_behaviour_name_replaces_an_illegal_derived_one(self, tmp_path: Path) -> None:
        """A 112-character component derives a 129-character behaviour; stating one leaves nothing to charge."""
        long_name = "C" * 112

        def rename(swc: dict[str, Any]) -> None:
            swc["name"] = long_name

        unstated = _project(tmp_path / "unstated", (PROVIDER, rename))
        stated = _project(tmp_path / "stated", (PROVIDER, rename), (PROVIDER, _set_behaviour("Behaviour")))

        assert len(_rules(check_identifier_conformance(str(unstated)).records, "identifier.generated")) == 1
        assert check_identifier_conformance(str(stated)).consistent

    @pytest.mark.parametrize("stated", [None, "Tick", TOO_LONG], ids=["derived", "stated", "stated-too-long"])
    def test_an_event(self, stated: str | None, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["runnables"].append(_long_runnable_with_timing_event(stated))

        records = check_identifier_conformance(str(_project(tmp_path, (PROVIDER, mutate)))).records

        generated, declared = _rules(records, "identifier.generated"), _rules(records, "identifier.declared")
        assert (len(generated), len(declared)) == {None: (1, 0), "Tick": (0, 0), TOO_LONG: (0, 1)}[stated]

    @pytest.mark.parametrize("stated", [None, "ReadIt", TOO_LONG], ids=["derived", "stated", "stated-too-long"])
    def test_an_access_point(self, stated: str | None, tmp_path: Path) -> None:
        # `R_` + 121 + `_` + `value` = 129: the pair's joint budget of 125 is spent by one.
        port = "P" * 121

        def mutate(swc: dict[str, Any]) -> None:
            swc["ports"].append({"name": port, "direction": "require", "interface": "IExample"})
            entry: dict[str, Any] = {"port": port, "element": "value"}
            if stated is not None:
                entry["name"] = stated
            swc["runnables"][0]["reads"] = [entry]

        records = check_identifier_conformance(str(_project(tmp_path, (CONSUMER, mutate)))).records

        generated, declared = _rules(records, "identifier.generated"), _rules(records, "identifier.declared")
        assert (len(generated), len(declared)) == {None: (1, 0), "ReadIt": (0, 0), TOO_LONG: (0, 1)}[stated]


def _twin_shapes() -> dict[str, SWComponent]:
    """The shapes every identifier twin is asked, each against its verdict in ``check-model``."""
    return {
        "derived-too-long": _component(runnables=[_long_runnable_with_timing_event(None)]),
        "stated-replaces-it": _component(runnables=[_long_runnable_with_timing_event("Tick")]),
        "stated-too-long": _component(runnables=[_long_runnable_with_timing_event(TOO_LONG)]),
        "behaviour-stated-too-long": _component(behavior_name=TOO_LONG),
        "access-stated-too-long": _reads({"port": "p", "element": "a", "name": TOO_LONG}),
    }


TWIN_SHAPES = sorted(_twin_shapes())

#: The one shape every twin accepts: the stated name replaces an illegal derived one.
LEGAL = {"stated-replaces-it"}

#: What each twin's refusal of a refused shape names: the name it judged, so a shape
#: refused for another reason does not pass. The derived form is ``TMT_`` and the
#: 125-character runnable; every stated shape states :data:`TOO_LONG`.
NAMED = {
    "derived-too-long": "TMT_" + "R" * 125,
    "stated-too-long": TOO_LONG,
    "behaviour-stated-too-long": TOO_LONG,
    "access-stated-too-long": TOO_LONG,
}

#: The words every twin's identifier refusal carries: the length rule's.
LIMIT = f"at most {MAX_IDENTIFIER_LENGTH} characters"


class TestTheIdentifierTwins:
    """Pass D, the writer's guard, the write path's guard and the component import's, on the same shapes."""

    @pytest.mark.parametrize("shape", TWIN_SHAPES)
    def test_pass_d(self, shape: str) -> None:
        component = _twin_shapes()[shape]
        staged = StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})

        violations = emitted_identifier_violations(staged)

        if shape in LEGAL:
            assert violations == ()
        else:
            (violation,) = violations
            assert f"'{NAMED[shape]}'" in violation.message and LIMIT in violation.message, violation.message

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "timing", "period": 0.01, "name": TOO_LONG},
            {
                "type": "timing",
                "period": 0.01,
                "disabled_modes": [{"port": "p", "mode_group_ref": "/Modes/IMode/Group", "mode_ref": "/Modes/M/On"}],
                "name": TOO_LONG,
            },
            {"type": "init", "name": TOO_LONG},
            {"type": "operation", "port": "p", "operation": "Op", "name": TOO_LONG},
        ],
        ids=["timing", "timing-with-disabled-modes", "init", "operation"],
    )
    def test_pass_d_addresses_a_stated_name_where_it_is_stated(self, event: dict[str, Any]) -> None:
        """Every event kind's stated name is addressed as the whole value it is, its identity carrying the name."""
        component = _component(runnables=[{"name": "R", "events": [event]}])
        staged = StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})

        (violation,) = emitted_identifier_violations(staged)

        assert violation.addresses[0].path[-2:] == (FieldStep("events"), MemberStep(event))

    @pytest.mark.parametrize("shape", TWIN_SHAPES)
    def test_the_writers_guard(self, shape: str) -> None:
        component = _twin_shapes()[shape]
        if shape in LEGAL:
            _check_component_names([component])
        else:
            with pytest.raises(ValueError, match=re.escape(NAMED[shape])) as caught:
                _check_component_names([component])
            assert "not a legal AUTOSAR" in str(caught.value) or LIMIT in str(caught.value)

    @pytest.mark.parametrize("shape", TWIN_SHAPES)
    def test_the_write_path(self, shape: str, tmp_path: Path) -> None:
        component = _twin_shapes()[shape]
        target = tmp_path / "swc" / "core" / "S.yaml"
        if shape in LEGAL:
            write_element(str(tmp_path), "swc/core/S.yaml", component, create=True)
            (written,) = read_swc_model(str(tmp_path))
            assert written == component
        else:
            with pytest.raises(ValueError, match=re.escape(NAMED[shape])):
                write_element(str(tmp_path), "swc/core/S.yaml", component, create=True)
            assert not target.exists()

    @pytest.mark.parametrize("shape", TWIN_SHAPES)
    def test_the_component_import(self, shape: str) -> None:
        violations = illegal_identifiers(_twin_shapes()[shape], [], [])

        if shape in LEGAL:
            assert violations == []
        else:
            (violation,) = violations
            assert f"'{NAMED[shape]}'" in violation, violation


# ---------------------------------------------------------------------------
# A stated behaviour name on an external stub: reported and refused, never dropped
# ---------------------------------------------------------------------------


class TestABehaviourNameOnAStub:
    """A ``behavior_name`` on an external stub is reported and refused.

    An external stub emits no behaviour, so a ``behavior_name`` there would be dropped without a word:
    ``rule:swc.external-with-ports`` reports it, as every other behaviour-side field, and the writer refuses it."""

    def test_check_model_reports_it_and_the_writer_refuses_it(self, tmp_path: Path) -> None:
        model_root = component_stub_ports_model_root(tmp_path / "model")
        change(model_root, "swc/external/ExternalStub.yaml", lambda document: document.update(behavior_name="B"))

        (record,) = [
            record
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule == "swc.external-with-ports"
        ]
        assert "declares 'external: true' and also behavior_name" in record.message
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("behavior_name"),)),)

        output_dir = tmp_path / "fresh-output"
        with pytest.raises(ValueError, match="external stub 'ExternalStub' declares behavior_name"):
            generate_arxml(str(model_root), str(output_dir / "model.arxml"))
        assert not output_dir.exists()

    def test_the_rule_names_the_field(self) -> None:
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:swc.external-with-ports"]
        assert "field:swc.swc.behavior_name" in rule["appliesTo"]
        assert rule["summary"].endswith("a package or a behaviour name.")


# ---------------------------------------------------------------------------
# The repairs name a stated name: wherever a generated form is
# judged -- a collision or an overflow -- stating the name is a repair too, and
# where no name can be stated (`{swc}_Implementation`) the message is unchanged.
# ---------------------------------------------------------------------------

#: The sentence an overflow of a generated form a model can state ends with.
WRITTEN_IN_PLACE = "a stated name is written in place of the generated one."


def _set_name(name: str) -> Callable[[dict[str, Any]], None]:
    def mutate(swc: dict[str, Any]) -> None:
        swc["name"] = name

    return mutate


class TestTheRepairsNameAStatedName:
    def test_a_generated_event_in_a_name_space_collision(self, tmp_path: Path) -> None:
        model = _project(tmp_path, (PROVIDER, _second_timing_event("tmt_examplerun_0")))

        (finding,) = _rules(check_name_space_collisions(str(model)).records, "name.name-space-collision")

        assert "(generated event names; rename a port, an operation or a runnable, or state an event's name)" in (
            finding.message
        )

    def test_the_generated_behaviour_in_a_name_space_collision(self, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["ports"].append(
                {"name": "examplesWC_internalbehavior", "direction": "provide", "interface": "IExample"}
            )

        model = _project(tmp_path, (PROVIDER, mutate))

        (finding,) = _rules(check_name_space_collisions(str(model)).records, "name.name-space-collision")

        assert "(the behaviour's generated name; rename the port, or state behavior_name)" in finding.message

    def test_a_derived_runnable_event_collision(self) -> None:
        runnables = [{"name": "R", "events": [{"type": "timing", "period": 0.01}]}, {"name": "TMT_R"}]

        (collision,) = runnable_event_collisions(_component(runnables=runnables))

        assert "rename runnable 'TMT_R' or state the event's name." in collision.message

    def test_a_derived_access_point_collision(self, tmp_path: Path) -> None:
        derived = _reads({"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"})
        repair = "so the derivation is not injective. Rename one of the conflicting names, or state an entry's name."

        with pytest.raises(WriterReservedNameError) as write_path:
            check_runnable_access_names("S", derived.runnables[0])
        with pytest.raises(ValueError) as writer:
            _check_component_names([derived])
        for message in (str(write_path.value), str(writer.value)):
            assert repair in message

    def test_the_generated_behaviour_overflow(self, tmp_path: Path) -> None:
        """114 characters: ``_Implementation`` (no stated twin) and ``_InternalBehavior`` both overflow."""
        model = _project(tmp_path, (PROVIDER, _set_name("C" * 114)))

        findings = _rules(check_identifier_conformance(str(model)).records, "identifier.generated")

        by_form = {
            form: [finding.message for finding in findings if f"{'C' * 114}{form}'" in finding.message]
            for form in ("_Implementation", "_InternalBehavior")
        }
        ((implementation,), (behaviour,)) = by_form["_Implementation"], by_form["_InternalBehavior"]
        assert behaviour.endswith(f" Or state the component's behavior_name: {WRITTEN_IN_PLACE}")
        assert "Or state" not in implementation

    def test_a_generated_timing_event_overflow(self, tmp_path: Path) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["runnables"].append(_long_runnable_with_timing_event(None))

        records = check_identifier_conformance(str(_project(tmp_path, (PROVIDER, mutate)))).records

        (finding,) = _rules(records, "identifier.generated")
        assert finding.message.endswith(f" Or state the event's name: {WRITTEN_IN_PLACE}")

    def test_a_generated_operation_event_overflow(self, tmp_path: Path) -> None:
        # `OIT_` + 62 + `_` + 62 = 129.
        event = {"type": "operation", "port": "P" * 62, "operation": "O" * 62}

        def mutate(swc: dict[str, Any]) -> None:
            swc["runnables"].append({"name": "Served", "events": [event]})

        records = check_identifier_conformance(str(_project(tmp_path, (PROVIDER, mutate)))).records

        (finding,) = _rules(records, "identifier.generated")
        assert finding.message.endswith(f" Or state the event's name: {WRITTEN_IN_PLACE}")
        with pytest.raises(ValueError) as writer:
            _check_component_names([_component(runnables=[{"name": "Served", "events": [event]}])])
        assert str(writer.value).endswith("Rename the conflicting names, or state the event's name.")

    def test_a_generated_access_point_overflow(self, tmp_path: Path) -> None:
        port = "P" * 121

        def mutate(swc: dict[str, Any]) -> None:
            swc["ports"].append({"name": port, "direction": "require", "interface": "IExample"})
            swc["runnables"][0]["reads"] = [{"port": port, "element": "value"}]

        records = check_identifier_conformance(str(_project(tmp_path, (CONSUMER, mutate)))).records

        (finding,) = _rules(records, "identifier.generated")
        assert finding.message.endswith(f" Or state the entry's name: {WRITTEN_IN_PLACE}")
        component = _reads({"port": port, "element": "value"})
        with pytest.raises(ValueError) as write_path:
            check_runnable_access_names("S", component.runnables[0])
        assert str(write_path.value).endswith(f" Or state the entry's name: {WRITTEN_IN_PLACE}")
        with pytest.raises(ValueError) as writer:
            _check_component_names([component])
        assert str(writer.value).endswith("Rename the conflicting names, or state the entry's name.")
