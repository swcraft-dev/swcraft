"""A port's own ``init`` (a ``com_specs`` entry's) judged by the eight init rules.

A ``com_specs`` entry may state ``init``, the port's own value for the data element it names
(AUTOSAR's com-spec ``initValue``, which supersedes the data prototype's: ``[TPS_SWCT_01182]``). It is
``field:interfaces.data_element.init``'s twin, judged against the type of the data element the entry
names with the same predicates:

* the eight ``rule:interface.init-*`` rules ``validator.check_init_conformance`` mints, each reported
  at the port's entry with the refusing declaration as the second subject, the message naming the
  component, the port and the element;
* ``rule:swc.port.com-spec-inapplicable``: an ``init`` on a queued element has no place, because a
  queued com-spec carries no ``INIT-VALUE``;
* an entry naming no data element is ``rule:swc.port.com-spec-unknown-element``'s alone, and its
  ``init`` is not judged: there is no type to judge it against.

Each is asserted in ``check-model``, in the ARXML writer's up-front guard (no file, the rule's own
message), in the component export (no file) and in the editing protocol's pass D.

What this does not reach: the ARXML the writer emits for a legal port ``init``
(``test_arxml_symbol_and_port_init_writer.py``), and the importer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, model_element_path, write_model_element
from model_contract_fixtures import load_contract
from project_support import files_of
from swcraft_tools.component_exchange import export_component
from swcraft_tools.component_exchange.closure import ComponentExchangeError
from swcraft_tools.component_exchange.export import ClosureValidationError
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency, check_init_conformance
from swcraft_tools.writer.arxml_generation import generate_arxml

BASIC = REPO_ROOT / "examples" / "basic"
INTERFACE = "interfaces/IExample.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
PROVIDER = "swc/example/ExampleSWC.yaml"

#: The eight rules ``check_init_conformance`` mints, at a port's entry too.
INIT_RULES: tuple[str, ...] = (
    "interface.init-shape-mismatch",
    "interface.init-unknown-enum-literal",
    "interface.init-record-field-mismatch",
    "interface.init-array-length-mismatch",
    "interface.init-out-of-range",
    "interface.init-string-scalar",
    "interface.init-unwritable-integer",
    "interface.init-on-external-type",
)
INAPPLICABLE = "swc.port.com-spec-inapplicable"
UNKNOWN_ELEMENT = "swc.port.com-spec-unknown-element"

#: An integer no interpreter limit lets `str()` convert, written as TEXT: `yaml.safe_dump` would raise on it.
WIDE = "0x" + "f" * 4000
_WIDE_PLACEHOLDER = "__WIDE__"

#: Every type a case needs, beside the example's own; each is clean on the example (measured).
TYPES: dict[str, dict[str, Any]] = {
    "Level": {"name": "Level", "kind": "application_type", "range": {"min": 0.0, "max": 100.0}},
    "Mode": {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}]},
    "Pair": {
        "name": "Pair",
        "kind": "record",
        "fields": [{"name": "a", "type": "Level"}, {"name": "b", "type": "Mode"}],
    },
    "Trio": {"name": "Trio", "kind": "array", "element_type": "Level", "length": 3},
    "Bounded": {"name": "Bounded", "kind": "value_type", "base": "float32_base", "value_min": 0.0, "value_max": 100.0},
    "ExtT": {
        "name": "ExtT",
        "kind": "external_type",
        "external_ref": "/Platform/ImplementationDataTypes/ExtT",
        "external_dest": "IMPLEMENTATION-DATA-TYPE",
    },
}


#: The application-side kinds: a layer of their own, and a data element naming one sets
#: `application_type` rather than `type`.
APPLICATION_KINDS = frozenset({"application_type", "enum", "record", "array"})


def _is_application(type_name: str) -> bool:
    return type_name in TYPES and TYPES[type_name]["kind"] in APPLICATION_KINDS


def _unwritable_detail() -> str:
    return (
        "is <an integer of 16000 bits, too wide to render>: writing it into the ARXML means converting it to its "
        f"decimal string, which is above this interpreter's {sys.get_int_max_str_digits()}-digit limit."
    )


#: `rule -> (the element's type, the port's init, the walk's detail, the second subject's type)`.
CASES: dict[str, tuple[str, Any, str, str]] = {
    "interface.init-shape-mismatch": (
        "float32",
        [1, 2],
        "is a list, but type 'float32' is a value_type, which takes a scalar.",
        "float32",
    ),
    "interface.init-unknown-enum-literal": (
        "Mode",
        "REVERSE",
        "is 'REVERSE', which names no literal of enum 'Mode'; it declares ['OFF', 'ON']. A literal name YAML "
        "resolves to a non-string (ON, NO, 0x10) must be quoted.",
        "Mode",
    ),
    "interface.init-record-field-mismatch": (
        "Pair",
        {"a": 1.0},
        "does not name the fields record 'Pair' declares: missing ['b'], undeclared [].",
        "Pair",
    ),
    "interface.init-array-length-mismatch": (
        "Trio",
        [1.0, 2.0],
        "has 2 member(s), but array 'Trio' declares length 3.",
        "Trio",
    ),
    "interface.init-out-of-range": (
        "Bounded",
        150.0,
        "is 150.0, which is above type 'Bounded's declared value_max 100.0.",
        "Bounded",
    ),
    "interface.init-string-scalar": (
        "float32",
        "fast",
        "is 'fast', a string, but type 'float32' is a value_type that references no table, so its values are "
        "numbers; a text names an enumeration's literal or a table's entry, and nothing else.",
        "float32",
    ),
    "interface.init-unwritable-integer": ("float32", _WIDE_PLACEHOLDER, "", "float32"),
    "interface.init-on-external-type": ("ExtT", 1, "", "ExtT"),
}


def _detail(rule: str) -> str:
    return _unwritable_detail() if rule == "interface.init-unwritable-integer" else CASES[rule][2]


def _port_message(rule: str, swc: str = "ExampleConsumerSWC", port: str = "rIn") -> str:
    if rule == "interface.init-on-external-type":
        return (
            f"SWC '{swc}' port '{port}' states an init value of its own for data element 'value', but the "
            "element's type 'ExtT' is an external data type stub whose content is outside the project: no value "
            "can be shaped against it. Remove the port's init, or type the element by a declared type."
        )
    return f"SWC '{swc}' port '{port}' com_specs init value for data element 'value' at 'init' {_detail(rule)}"


# ---------------------------------------------------------------------------
# Builders over a copy of examples/basic
# ---------------------------------------------------------------------------


def _put(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, sort_keys=False).replace(_WIDE_PLACEHOLDER, WIDE)
    path.write_text(text, encoding="utf-8", newline="\n")


def _edit(model: Path, relative: str, mutate: Any) -> None:
    path = model / relative
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    _put(path, document)


def _project(tmp_path: Path, element_type: str = "float32", *, queued: bool = False) -> Path:
    """The example, its data element typed *element_type*, with every type and one mapping set the cases need."""
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    model = project / "model"
    for name, document in TYPES.items():
        _put(model_element_path(model, name, kind=document["kind"]), document)
    write_model_element(
        model,
        {
            "name": "ExampleTypes",
            "maps": [
                {"application": "Level", "implementation": "float32"},
                {"application": "Mode", "implementation": "uint8"},
            ],
        },
        layer="mapping_sets",
    )
    for component in (PROVIDER, CONSUMER):
        _edit(model, component, lambda d: d.update(data_type_mappings=["ExampleTypes"]))

    def retype(document: dict[str, Any]) -> None:
        if _is_application(element_type):
            # The one type field set, in the position `implementation_type` holds.
            element = document["data_elements"][0]
            document["data_elements"][0] = {
                ("application_type" if key == "implementation_type" else key): (
                    element_type if key == "implementation_type" else value
                )
                for key, value in element.items()
            }
        else:
            document["data_elements"][0]["implementation_type"] = element_type
        if queued:
            document["data_elements"][0]["sw_impl_policy"] = "queued"

    _edit(model, INTERFACE, retype)
    return project


def _com_specs(model: Path, relative: str, *entries: dict[str, Any]) -> None:
    _edit(model, relative, lambda d: d["ports"][0].update(com_specs=list(entries)))


def _broken(tmp_path: Path, rule: str) -> Path:
    element_type, init, _, _ = CASES[rule]
    project = _project(tmp_path, element_type)
    _com_specs(project / "model", CONSUMER, {"data_element": "value", "init": init})
    return project


def _records(model: Path) -> list[Any]:
    return list(check_cross_layer_consistency(str(model)).records)


def _of(model: Path, rules: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(record.rule, record.message) for record in _records(model) if record.rule in rules]


# ---------------------------------------------------------------------------
# The contract: the nine widened rules
# ---------------------------------------------------------------------------


class TestTheWidenedRules:
    @pytest.mark.parametrize("rule", [*INIT_RULES, INAPPLICABLE])
    def test_each_names_the_port_s_init_and_says_so(self, rule: str) -> None:
        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == f"rule:{rule}"]
        assert "field:swc.port_com_spec.init" in declared["appliesTo"]
        assert declared["summary"] == CONSISTENCY_RULES[rule]

    @pytest.mark.parametrize("rule", INIT_RULES)
    def test_each_init_rule_names_the_walk_that_reaches_the_port_s_init(self, rule: str) -> None:
        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == f"rule:{rule}"]
        for field in (
            "field:swc.swc.external",
            "field:swc.swc.name",
            "field:swc.swc.ports",
            "field:swc.port.name",
            "field:swc.port.interface",
            "field:swc.port.com_specs",
            "field:swc.port_com_spec.data_element",
            "field:interfaces.data_element.name",
            "field:interfaces.data_element.init",
            "field:interfaces.data_element.implementation_type",
            "field:interfaces.data_element.sw_impl_policy",
        ):
            assert field in declared["appliesTo"], field
        assert "com_specs" in declared["summary"]


# ---------------------------------------------------------------------------
# The predicate: one case per rule, at the port's entry
# ---------------------------------------------------------------------------


def test_the_example_with_every_type_is_clean(tmp_path: Path) -> None:
    """Non-vacuity: without a port init every type the cases use passes `check-model`."""
    for element_type in {case[0] for case in CASES.values()}:
        project = _project(tmp_path / element_type, element_type)
        assert check_cross_layer_consistency(str(project / "model")).consistent, element_type


@pytest.mark.parametrize("rule", INIT_RULES)
def test_each_rule_fires_at_the_port_s_entry_with_its_message(rule: str, tmp_path: Path) -> None:
    model = _broken(tmp_path, rule) / "model"

    assert _of(model, (*INIT_RULES, INAPPLICABLE, UNKNOWN_ELEMENT)) == [(rule, _port_message(rule))]


@pytest.mark.parametrize("rule", INIT_RULES)
def test_the_subjects_are_the_port_s_init_and_the_refusing_declaration(rule: str, tmp_path: Path) -> None:
    model = _broken(tmp_path, rule) / "model"

    (record,) = [record for record in check_init_conformance(str(model)).records if record.rule == rule]
    entry, declaration = record.subjects
    assert (entry.layer, entry.element) == ("swc", "ExampleConsumerSWC")
    assert [step.value for step in entry.path] == ["ports", "rIn", "com_specs", "value", "init"]
    layer = "application_types" if _is_application(CASES[rule][3]) else "implementation_types"
    assert (declaration.layer, declaration.element) == (layer, CASES[rule][3])


@pytest.mark.parametrize("rule", [rule for rule in INIT_RULES if rule != "interface.init-on-external-type"])
def test_the_port_and_the_element_are_judged_by_one_predicate(rule: str, tmp_path: Path) -> None:
    """The same value on the element gives the same verdict and the same detail: one walk, two positions."""
    element_type, init, _, _ = CASES[rule]
    project = _project(tmp_path, element_type)
    _edit(project / "model", INTERFACE, lambda d: d["data_elements"][0].update(init=init))

    (record,) = [record for record in check_init_conformance(str(project / "model")).records]
    assert record.rule == rule
    assert record.message == f"Interface 'IExample' element 'value' init value at 'init' {_detail(rule)}"


def test_a_provide_port_s_init_is_judged_too(tmp_path: Path) -> None:
    project = _project(tmp_path, "float32")
    _com_specs(project / "model", PROVIDER, {"data_element": "value", "init": [1, 2]})

    assert _of(project / "model", INIT_RULES) == [
        ("interface.init-shape-mismatch", _port_message("interface.init-shape-mismatch", "ExampleSWC", "pOut"))
    ]


@pytest.mark.parametrize(
    "init",
    [1.5, 0, True],
    ids=["float", "zero", "boolean"],
)
def test_a_conforming_port_init_is_silent(init: Any, tmp_path: Path) -> None:
    project = _project(tmp_path, "float32")
    _com_specs(project / "model", CONSUMER, {"data_element": "value", "init": init})

    assert _of(project / "model", (*INIT_RULES, INAPPLICABLE, UNKNOWN_ELEMENT)) == []


def test_an_entry_naming_no_element_is_the_unknown_element_rule_s_alone(tmp_path: Path) -> None:
    project = _project(tmp_path, "float32")
    _com_specs(project / "model", CONSUMER, {"data_element": "absent", "init": [1, 2]})

    assert [rule for rule, _ in _of(project / "model", (*INIT_RULES, INAPPLICABLE, UNKNOWN_ELEMENT))] == [
        UNKNOWN_ELEMENT
    ]


# ---------------------------------------------------------------------------
# The inapplicable clause: an init on a queued element
# ---------------------------------------------------------------------------

QUEUED_RECEIVER_MESSAGE = (
    "SWC 'ExampleConsumerSWC' require port 'rIn' states 'init' for data element 'value', but the element is queued, "
    "and a queued receiver com-spec has no place for a non-queued one."
)
QUEUED_SENDER_MESSAGE = (
    "SWC 'ExampleSWC' provide port 'pOut' states 'init' for data element 'value', but the element is queued, and a "
    "queued sender com-spec has no place for an init value."
)


def _queued_receiver(tmp_path: Path, init: Any = 1.0) -> Path:
    project = _project(tmp_path, "float32", queued=True)
    _com_specs(project / "model", CONSUMER, {"data_element": "value", "queue_length": 1, "init": init})
    return project


class TestInitOnAQueuedElement:
    def test_on_a_receiver(self, tmp_path: Path) -> None:
        model = _queued_receiver(tmp_path) / "model"
        assert _of(model, (*INIT_RULES, INAPPLICABLE)) == [(INAPPLICABLE, QUEUED_RECEIVER_MESSAGE)]

    def test_on_a_sender(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "float32", queued=True)
        model = project / "model"
        _com_specs(model, CONSUMER, {"data_element": "value", "queue_length": 1})
        _com_specs(model, PROVIDER, {"data_element": "value", "init": 1.0})
        assert _of(model, (*INIT_RULES, INAPPLICABLE)) == [(INAPPLICABLE, QUEUED_SENDER_MESSAGE)]

    def test_its_value_is_not_judged_as_well(self, tmp_path: Path) -> None:
        """One finding: the value has no place, so whether it fits the type is not asked."""
        model = _queued_receiver(tmp_path, init=[1, 2]) / "model"
        assert [rule for rule, _ in _of(model, (*INIT_RULES, INAPPLICABLE))] == [INAPPLICABLE]

    def test_a_queued_element_without_a_port_init_is_silent(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "float32", queued=True)
        _com_specs(project / "model", CONSUMER, {"data_element": "value", "queue_length": 1})
        assert _of(project / "model", (*INIT_RULES, INAPPLICABLE)) == []


#: Every refusal this module asserts outside the predicate: the rule and how to break the example for it.
REFUSALS: dict[str, tuple[Any, str]] = {
    **{rule: ((lambda tmp, rule=rule: _broken(tmp, rule)), _port_message(rule)) for rule in INIT_RULES},
    INAPPLICABLE: (_queued_receiver, QUEUED_RECEIVER_MESSAGE),
}


# ---------------------------------------------------------------------------
# check-model
# ---------------------------------------------------------------------------


def _check_model(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


@pytest.mark.parametrize("rule", sorted(REFUSALS))
def test_check_model_lists_each(rule: str, tmp_path: Path) -> None:
    build, message = REFUSALS[rule]
    project = build(tmp_path)

    result = _check_model(project)

    assert result.returncode == 1, result.stdout
    assert message in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# The ARXML writer and the component export: refused before any write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
@pytest.mark.parametrize("rule", sorted(REFUSALS))
def test_generate_arxml_refuses_with_the_rule_s_message_and_writes_nothing(
    rule: str, version: str, tmp_path: Path
) -> None:
    build, message = REFUSALS[rule]
    model = build(tmp_path) / "model"
    out = tmp_path / "out" / "model.arxml"

    with pytest.raises(ValueError) as caught:
        generate_arxml(str(model), str(out), version=version)

    assert f"rule:{rule}: {message}" in str(caught.value)
    assert not out.exists()


#: The two init rules the export never reaches the writer's guard for, each refused earlier for a reason that
#: holds for an element's own `init` as well: the export refuses any component reaching an external type stub
#: (`ComponentExchangeError`), and it materializes the closure through `yaml.safe_dump`, which cannot write an
#: integer above the interpreter's limit at all.
_EXPORT_REFUSED_EARLIER = ("interface.init-on-external-type", "interface.init-unwritable-integer")


@pytest.mark.parametrize("rule", [rule for rule in INIT_RULES if rule not in _EXPORT_REFUSED_EARLIER])
def test_the_component_export_refuses_an_init_rule_at_the_writer_s_guard(rule: str, tmp_path: Path) -> None:
    """A value the writer cannot write is its guard's to refuse during the export, before any write."""
    model = _broken(tmp_path, rule) / "model"
    out = tmp_path / "ExampleConsumerSWC.arxml"

    with pytest.raises(ValueError) as caught:
        export_component(str(model), "ExampleConsumerSWC", str(out))

    assert f"rule:{rule}: {_port_message(rule)}" in str(caught.value)
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


#: Each earlier refusal's own type and a fragment of its own message, so a crash of another kind -- a
#: `TypeError`, an `AttributeError` -- cannot pass for it.
_EARLIER_REFUSALS: dict[str, tuple[type[Exception], str]] = {
    "interface.init-on-external-type": (ComponentExchangeError, "reaches external type"),
    "interface.init-unwritable-integer": (ValueError, "for integer string conversion"),
}


@pytest.mark.parametrize("rule", _EXPORT_REFUSED_EARLIER)
def test_the_two_the_export_refuses_earlier_still_write_nothing(rule: str, tmp_path: Path) -> None:
    model = _broken(tmp_path, rule) / "model"
    out = tmp_path / "ExampleConsumerSWC.arxml"
    kind, fragment = _EARLIER_REFUSALS[rule]

    with pytest.raises(kind) as caught:
        export_component(str(model), "ExampleConsumerSWC", str(out))

    assert type(caught.value) is kind
    assert fragment in str(caught.value)

    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


def test_the_component_export_refuses_the_inapplicable_init_at_the_closure(tmp_path: Path) -> None:
    model = _queued_receiver(tmp_path) / "model"
    out = tmp_path / "ExampleConsumerSWC.arxml"

    with pytest.raises(ClosureValidationError) as caught:
        export_component(str(model), "ExampleConsumerSWC", str(out))

    assert caught.value.issues == [QUEUED_RECEIVER_MESSAGE]
    assert not out.exists()


# ---------------------------------------------------------------------------
# Pass D
# ---------------------------------------------------------------------------


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _create_entry(project: Path, entry: dict[str, Any]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    operation = {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": "ExampleConsumerSWC",
            "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "com_specs"}, {"member": entry["data_element"]}],
        },
        "value": entry,
    }
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "a port's own init", "operations": [operation]},
        project=str(project),
        review=_approve,
    )


def _assert_refused(project: Path, response: dict[str, Any], message: str, before: dict[str, bytes]) -> None:
    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    messages = [response["message"], *(entry["message"] for entry in response.get("additional", []))]
    assert any(text.startswith(message) for text in messages), response
    assert files_of(project / "model") == before


@pytest.mark.parametrize("rule", [rule for rule in INIT_RULES if rule != "interface.init-unwritable-integer"])
def test_pass_d_refuses_a_change_set_that_introduces_each_init_rule(rule: str, tmp_path: Path) -> None:
    element_type, init, _, _ = CASES[rule]
    project = _project(tmp_path, element_type)
    before = files_of(project / "model")

    response = _create_entry(project, {"data_element": "value", "init": init})

    _assert_refused(project, response, _port_message(rule), before)


def test_pass_d_refuses_an_unwritable_integer(tmp_path: Path) -> None:
    """Refused by the component file's write guard, before the dump that could not write it, as an element's is."""
    project = _project(tmp_path, "float32")
    before = files_of(project / "model")

    response = _create_entry(project, {"data_element": "value", "init": int(WIDE, 16)})

    _assert_refused(
        project,
        response,
        "SWC 'ExampleConsumerSWC' port 'rIn' com_specs init value for data element 'value' at 'init' is <an "
        "integer of 16000 bits, too wide to render>: writing it into the model file means converting it to its "
        f"decimal string, which is above this interpreter's {sys.get_int_max_str_digits()}-digit limit "
        "(interface.init-unwritable-integer). Choose a value it can write.",
        before,
    )


def test_pass_d_refuses_an_init_on_a_queued_element(tmp_path: Path) -> None:
    project = _project(tmp_path, "float32", queued=True)
    before = files_of(project / "model")

    response = _create_entry(project, {"data_element": "value", "queue_length": 1, "init": 1.0})

    _assert_refused(project, response, QUEUED_RECEIVER_MESSAGE, before)


def test_pass_d_refuses_an_update_of_the_init_on_an_existing_entry(tmp_path: Path) -> None:
    """The port's ``init`` updated on an entry the component already holds -- the address ends at the field."""
    element_type, init, _, _ = CASES["interface.init-shape-mismatch"]
    project = _project(tmp_path, element_type)
    _com_specs(project / "model", CONSUMER, {"data_element": "value", "init": 1.0})
    assert _of(project / "model", INIT_RULES) == []
    before = files_of(project / "model")
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    operation = {
        "operation": "update",
        "address": {
            "layer": "swc",
            "element": "ExampleConsumerSWC",
            "path": [
                {"field": "ports"},
                {"member": "rIn"},
                {"field": "com_specs"},
                {"member": "value"},
                {"field": "init"},
            ],
        },
        "value": init,
    }

    response = handle_request(
        {"operation": "change_set", "baseline": token, "intent": "a port's own init", "operations": [operation]},
        project=str(project),
        review=_approve,
    )

    _assert_refused(project, response, _port_message("interface.init-shape-mismatch"), before)
