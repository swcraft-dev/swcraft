"""`check_safety_coexistence` — the six coexistence rules.

One check, one read of four layers, six rules. Three answer the questions of ISO 26262-9's coexistence clause
from the model's own declarations: may this component **produce** a rated signal
(`safety.provider-below-element`), **consume** a lower-rated one (`safety.consumer-above-element`), **share a
task** (`safety.task-mixed-asil`). Two report a declared argument nothing can read
(`safety.measure-unknown-element`, `safety.measure-on-provide-port`), and `safety.measure-blank` one that names
nothing.

Two asymmetries are decisions: the provider rule has no exemption and the consumer rule does (a coexistence
argument in `measures`), and a `measures` map on a provide port is a finding while a `coexistence` text on a
homogeneous task is not (it is read the moment a level changes). The `measure-*` rules are findings, never load
failures, so the project stays readable by `list`, `query`, `diff` and `impact`.

What this module does **not** reach:

* **the wire:** no rule follows a connector or a delegation;
* **the separation:** `task-mixed-asil` reads no partition, so it never says interference is prevented;
* **the measure's content:** only blankness is checked; a blank measure still closes the consumer rule's finding,
  and a zero-width space or a text that says nothing is content;
* **an unrated element:** no `asil` is not `QM`, and neither port rule reads it;
* **an answered finding:** `check-model` mints nothing, not even a warning, for a finding a declaration closes;
* **whether any of it is right:** a model that passes these rules is not thereby safe.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, SAFETY_MODEL_ROOT, _safety_reading
from swcraft_tools import cli
from swcraft_tools.diagnostics import Subject, field_step, key_step, member_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.reader import read_swc_model
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_safety_coexistence,
)
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

PROVIDER = "safety.provider-below-element"
CONSUMER = "safety.consumer-above-element"
UNKNOWN = "safety.measure-unknown-element"
ON_PROVIDE = "safety.measure-on-provide-port"
MIXED = "safety.task-mixed-asil"
BLANK = "safety.measure-blank"
RULES = (PROVIDER, CONSUMER, UNKNOWN, ON_PROVIDE, MIXED, BLANK)


# ---------------------------------------------------------------------------
# Model fixtures — the four layers the check reads, and nothing else
# ---------------------------------------------------------------------------


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _swc(model_root: Path, name: str, ports: list[dict[str, Any]] | None = None, **extra: Any) -> None:
    linked = _PENDING_LINKS.get((model_root, name), [])
    _write(
        model_root / "swc" / "group" / f"{name}.yaml",
        {
            "name": name,
            "type": "application",
            "ports": ports or [],
            **({"satisfies": linked} if linked else {}),
            **extra,
        },
    )


def _sr(model_root: Path, name: str, elements: list[dict[str, Any]]) -> None:
    _write(
        model_root / "interfaces" / f"{name}.yaml",
        {"name": name, "kind": "sender_receiver", "data_elements": elements},
    )


def _cs(model_root: Path, name: str) -> None:
    _write(
        model_root / "interfaces" / f"{name}.yaml",
        {"name": name, "kind": "client_server", "operations": [{"name": "Do", "arguments": []}]},
    )


def _requirement(model_root: Path, req_id: str, links: list[str], **extra: Any) -> None:
    """Write a requirement, and its link into each component *links* names.

    The link is the component's own ``satisfies`` member ``<req_id>@1``, appended
    to every component file declaring the name, written before or after (``_swc``
    keeps it). A name no file declares allocates to nothing.
    """
    _write(
        model_root / "requirements" / f"{req_id}.yaml",
        {
            "id": req_id,
            "title": f"{req_id} title",
            "description": f"The system shall satisfy {req_id}.",
            "version": 1,
            "source": "architecture",
            **extra,
        },
    )
    for name in links:
        _PENDING_LINKS.setdefault((model_root, name), []).append(f"{req_id}@1")
        for path in sorted((model_root / "swc").rglob("*.yaml"), key=lambda candidate: candidate.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if document.get("name") == name:
                document["satisfies"] = [*document.get("satisfies", []), f"{req_id}@1"]
                _write(path, document)


#: ``(model root, component name) -> links`` written by :func:`_requirement`, so a
#: component file written after its requirement carries them too.
_PENDING_LINKS: dict[tuple[Path, str], list[str]] = {}


def _task(model_root: Path, name: str, runnables: list[dict[str, str]], **extra: Any) -> None:
    _write(
        model_root / "tasks" / f"{name}.yaml",
        {"name": name, "period": 0.01, "priority": 1, "partition": "PARTITION0", "runnables": runnables, **extra},
    )


def _port(name: str, direction: str, interface: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "direction": direction, "interface": interface, **extra}


def _runnable(name: str) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "timing", "period": 0.01}]}


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """One rated interface, one unrated element beside it, and nothing declared yet."""
    root = tmp_path / "model"
    _sr(
        root,
        "ISig",
        [
            {"name": "rated", "implementation_type": "uint8", "asil": "B"},
            {"name": "bare", "implementation_type": "uint8"},
        ],
    )
    return root


def _records(model_root: Path, rule: str | None = None) -> tuple[Any, ...]:
    records = check_safety_coexistence(str(model_root)).records
    return tuple(record for record in records if rule is None or record.rule == rule)


def _one(model_root: Path, rule: str) -> Any:
    records = _records(model_root, rule)
    assert len(records) == 1, f"expected one {rule!r}, got {[r.message for r in records]}"
    return records[0]


# ---------------------------------------------------------------------------
# 1. rule:safety.provider-below-element — no exemption
# ---------------------------------------------------------------------------


class TestTheProviderRule:
    def test_a_declared_level_below_the_element_is_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="A")

        record = _one(model_root, PROVIDER)

        assert "Src" in record.message
        assert "ASIL A" in record.message
        assert "rated" in record.message
        assert "ASIL B" in record.message
        assert "'p'" in record.message
        assert "ISig" in record.message

    def test_a_derived_level_below_the_element_is_a_finding_naming_the_requirements(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig")])
        _requirement(model_root, "REQ_1", ["Src"], asil="A")

        record = _one(model_root, PROVIDER)

        assert "ASIL A" in record.message
        assert Subject("requirements", "REQ_1", (field_step("asil"),)) in record.subjects

    def test_a_derived_qm_producing_a_rated_element_is_a_finding(self, model_root: Path) -> None:
        """A component nothing allocates a level to is exactly the mis-declaration to name."""
        _swc(model_root, "Src", [_port("p", "provide", "ISig")])
        _requirement(model_root, "REQ_1", ["Src"])

        assert "ASIL QM" in _one(model_root, PROVIDER).message

    def test_an_equal_level_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="B")

        assert _records(model_root, PROVIDER) == ()

    def test_a_level_above_the_element_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="D")

        assert _records(model_root, PROVIDER) == ()

    def test_an_unrated_element_is_never_compared(self, model_root: Path) -> None:
        """An unrated element: no level, no comparison, no finding."""
        _sr(model_root, "IBare", [{"name": "bare", "implementation_type": "uint8"}])
        _swc(model_root, "Src", [_port("p", "provide", "IBare")], asil="QM")

        assert _records(model_root, PROVIDER) == ()

    def test_a_require_port_is_not_this_rule(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("r", "require", "ISig")], asil="A")

        assert _records(model_root, PROVIDER) == ()

    def test_a_client_server_interface_has_no_element_to_compare(self, model_root: Path) -> None:
        _cs(model_root, "ICs")
        _swc(model_root, "Src", [_port("p", "provide", "ICs")], asil="QM")

        assert _records(model_root, PROVIDER) == ()

    def test_an_undeclared_interface_makes_the_rule_abstain(self, model_root: Path) -> None:
        """`port.undeclared-interface`'s finding, and this check reports nothing beside it."""
        _swc(model_root, "Src", [_port("p", "provide", "IMissing")], asil="QM")

        assert _records(model_root) == ()

    def test_two_rated_elements_on_one_port_are_two_findings(self, model_root: Path) -> None:
        _sr(
            model_root,
            "ISig",
            [
                {"name": "one", "implementation_type": "uint8", "asil": "B"},
                {"name": "two", "implementation_type": "uint8", "asil": "D"},
            ],
        )
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="A")

        assert len(_records(model_root, PROVIDER)) == 2

    def test_two_components_producing_one_element_are_one_finding_each(self, model_root: Path) -> None:
        _swc(model_root, "A", [_port("p", "provide", "ISig")], asil="A")
        _swc(model_root, "B", [_port("p", "provide", "ISig")], asil="A")

        assert len(_records(model_root, PROVIDER)) == 2

    def test_the_finding_names_its_three_legal_repairs(self, model_root: Path) -> None:
        """The finding names its three legal repairs as subjects rather than imperative prose.

        Raise the component, lower the element, or change the requirements the level derives from. The rule does not
        choose."""
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="A")

        subjects = _one(model_root, PROVIDER).subjects

        assert Subject("swc", "Src", (field_step("asil"),)) in subjects
        assert Subject("swc", "Src", (field_step("ports"), member_step("p"), field_step("interface"))) in subjects
        assert (
            Subject(
                "interfaces",
                "ISig",
                (field_step("data_elements"), member_step("rated"), field_step("asil")),
            )
            in subjects
        )


# ---------------------------------------------------------------------------
# 2. rule:safety.consumer-above-element — closed by a `measures` entry
# ---------------------------------------------------------------------------


class TestTheConsumerRule:
    def test_a_consumer_above_the_element_with_no_measure_is_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="D")

        record = _one(model_root, CONSUMER)

        assert "Dst" in record.message
        assert "ASIL D" in record.message
        assert "rated" in record.message
        assert "no measure" in record.message

    def test_a_measure_for_that_element_closes_it(self, model_root: Path) -> None:
        """The exemption, and the whole reason `measures` exists."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "E2E profile 4"})], asil="D")

        assert _records(model_root, CONSUMER) == ()

    def test_a_blank_measure_closes_it_too(self, model_root: Path) -> None:
        """Rule 2 reads the KEY: a blank text closes it.

        ``safety.measure-blank`` refuses the model at the same port; its own tests
        are below.
        """
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": ""})], asil="D")

        assert _records(model_root, CONSUMER) == ()
        assert len(_records(model_root, BLANK)) == 1

    def test_a_measure_for_another_element_leaves_the_unmeasured_one_reported(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"bare": "watchdog"})], asil="D")

        assert "rated" in _one(model_root, CONSUMER).message

    def test_an_equal_level_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="B")

        assert _records(model_root, CONSUMER) == ()

    def test_a_level_below_the_element_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="A")

        assert _records(model_root, CONSUMER) == ()

    def test_an_unrated_element_is_never_compared(self, model_root: Path) -> None:
        _sr(model_root, "IBare", [{"name": "bare", "implementation_type": "uint8"}])
        _swc(model_root, "Dst", [_port("r", "require", "IBare")], asil="D")

        assert _records(model_root, CONSUMER) == ()

    def test_a_provide_port_is_not_this_rule(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("p", "provide", "ISig")], asil="D")

        assert _records(model_root, CONSUMER) == ()

    def test_a_derived_consumer_level_is_read_the_same_way(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")])
        _requirement(model_root, "REQ_1", ["Dst"], asil="D")

        assert "ASIL D" in _one(model_root, CONSUMER).message

    def test_a_derived_qm_consumer_is_above_nothing(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")])
        _requirement(model_root, "REQ_1", ["Dst"])

        assert _records(model_root, CONSUMER) == ()

    def test_the_finding_names_the_measures_field_as_the_fourth_repair(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="D")

        subjects = _one(model_root, CONSUMER).subjects

        assert Subject("swc", "Dst", (field_step("ports"), member_step("r"), field_step("measures"))) in subjects


# ---------------------------------------------------------------------------
# 3. rule:safety.measure-unknown-element
# ---------------------------------------------------------------------------


class TestTheUnknownElementRule:
    def test_a_key_naming_no_element_of_a_sender_receiver_interface_is_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"ratd": "typo"})], asil="D")

        record = _one(model_root, UNKNOWN)

        assert "ratd" in record.message
        assert "ISig" in record.message

    def test_the_finding_carries_no_target(self, model_root: Path) -> None:
        """The finding is position-keyed and carries no target, never the misspelt key.

        `editing/diagnosis.py` buckets a finding on `(group, target)`, so a key as
        target would merge two components misspelling one key into one finding. The
        key stays in the message and in the subject's own `KeyStep`.
        """
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"ratd": "typo"})], asil="D")

        assert _one(model_root, UNKNOWN).target is None

    def test_a_provide_port_is_not_this_rule(self, model_root: Path) -> None:
        """Rule 4 owns a map on a provide port, and its one repair decides the key question too.

        Reporting both would be two findings for one root cause, with a repair (fix
        the spelling) that repairs nothing, and a finding count that depends on the
        spelling.
        """
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={"ratd": "typo"})], asil="D")

        assert _records(model_root, UNKNOWN) == ()
        assert len(_records(model_root, ON_PROVIDE)) == 1

    def test_the_subject_is_the_keys_own_four_step_address_ending_in_a_key_step(self, model_root: Path) -> None:
        """A mapping entry is addressed by its KEY and never as a member — the differ's own rule."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"ratd": "typo"})], asil="D")

        (subject,) = _one(model_root, UNKNOWN).subjects

        assert subject == Subject(
            "swc",
            "Dst",
            (field_step("ports"), member_step("r"), field_step("measures"), key_step("ratd")),
        )

    def test_every_key_on_a_client_server_port_is_a_finding(self, model_root: Path) -> None:
        """A client-server interface declares no data element at all, so no key can name one."""
        _cs(model_root, "ICs")
        _swc(model_root, "Dst", [_port("r", "require", "ICs", measures={"Do": "x", "other": "y"})], asil="D")

        assert len(_records(model_root, UNKNOWN)) == 2

    def test_a_key_naming_an_element_that_is_not_below_the_consumer_is_legal(self, model_root: Path) -> None:
        """A measure ahead of need is not an error — the same argument the homogeneous task gets."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "x"})], asil="A")

        assert _records(model_root, UNKNOWN) == ()

    def test_an_undeclared_interface_makes_the_rule_abstain(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "IMissing", measures={"x": "y"})], asil="D")

        assert _records(model_root, UNKNOWN) == ()


# ---------------------------------------------------------------------------
# 4. rule:safety.measure-on-provide-port
# ---------------------------------------------------------------------------


class TestTheProvidePortRule:
    def test_a_non_empty_map_on_a_provide_port_is_one_finding_naming_every_key(self, model_root: Path) -> None:
        _swc(
            model_root,
            "Src",
            [_port("p", "provide", "ISig", measures={"rated": "x", "bare": "y"})],
            asil="D",
        )

        record = _one(model_root, ON_PROVIDE)

        assert "'rated'" in record.message
        assert "'bare'" in record.message

    def test_the_file_loads_and_the_project_stays_readable(self, model_root: Path) -> None:
        """A measure on a provide port is a FINDING and never a load failure.

        A load failure would make the whole project unreadable by `list`, `query`,
        `diff` and `impact` for a field whose point is that nothing verifies it.
        """
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={"rated": "x"})], asil="D")

        (swc,) = read_swc_model(str(model_root))

        assert swc.ports[0].measures == {"rated": "x"}
        assert _records(model_root, ON_PROVIDE) != ()

    def test_a_require_port_with_a_measure_is_not_this_rule(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "x"})], asil="D")

        assert _records(model_root, ON_PROVIDE) == ()

    def test_an_empty_map_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={})], asil="D")

        assert _records(model_root, ON_PROVIDE) == ()

    def test_a_blank_text_is_still_a_finding_because_content_is_not_what_is_checked(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={"rated": ""})], asil="D")

        assert _records(model_root, ON_PROVIDE) != ()

    def test_the_one_legal_repair_is_the_field_and_not_a_key(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={"rated": "x"})], asil="D")

        (subject,) = _one(model_root, ON_PROVIDE).subjects

        assert subject == Subject("swc", "Src", (field_step("ports"), member_step("p"), field_step("measures")))


# ---------------------------------------------------------------------------
# 5. rule:safety.task-mixed-asil — closed by a `coexistence` text
# ---------------------------------------------------------------------------


class TestTheTaskRule:
    @pytest.fixture
    def mixed(self, model_root: Path) -> Path:
        _swc(model_root, "Hi", runnables=[_runnable("HiRun")], asil="D")
        _swc(model_root, "Lo", runnables=[_runnable("LoRun")], asil="A")
        _task(model_root, "T", [{"swc": "Hi", "runnable": "HiRun"}, {"swc": "Lo", "runnable": "LoRun"}])
        return model_root

    def test_two_distinct_levels_with_no_argument_is_a_finding(self, mixed: Path) -> None:
        record = _one(mixed, MIXED)

        assert "'T'" in record.message
        assert "'Hi' (ASIL D)" in record.message
        assert "'Lo' (ASIL A)" in record.message

    def test_a_coexistence_text_closes_it(self, mixed: Path) -> None:
        _task(
            mixed,
            "T",
            [{"swc": "Hi", "runnable": "HiRun"}, {"swc": "Lo", "runnable": "LoRun"}],
            coexistence="Separate partitions with timing protection.",
        )

        assert _records(mixed, MIXED) == ()

    def test_a_blank_coexistence_closes_NOTHING_and_the_task_is_still_reported(self, mixed: Path) -> None:
        """A blank `coexistence` closes nothing, unlike a measure's blank value.

        `coexistence` defaults to `""`, so `""` is the field's undeclared state and
        the rule reads it as falsy. A measure's KEY is what closes rule 2, so its
        `''` value closes it as a profile name does.
        """
        _task(
            mixed,
            "T",
            [{"swc": "Hi", "runnable": "HiRun"}, {"swc": "Lo", "runnable": "LoRun"}],
            coexistence="",
        )

        assert len(_records(mixed, MIXED)) == 1

    def test_a_homogeneous_task_is_not_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "A", runnables=[_runnable("Run")], asil="D")
        _swc(model_root, "B", runnables=[_runnable("Run")], asil="D")
        _task(model_root, "T", [{"swc": "A", "runnable": "Run"}, {"swc": "B", "runnable": "Run"}])

        assert _records(model_root, MIXED) == ()

    def test_a_homogeneous_task_carrying_an_argument_is_still_not_a_finding(self, model_root: Path) -> None:
        """The deliberate asymmetry: an argument on a homogeneous task is no finding.

        A measure on a provide port is *structurally* unreadable and is reported;
        an argument on a task whose components happen to be rated alike is
        *currently* unneeded and becomes read the moment a level changes.
        """
        _swc(model_root, "A", runnables=[_runnable("Run")], asil="D")
        _task(model_root, "T", [{"swc": "A", "runnable": "Run"}], coexistence="Written ahead of need.")

        assert _records(model_root) == ()

    def test_three_levels_are_one_finding(self, model_root: Path) -> None:
        for name, level in (("A", "D"), ("B", "B"), ("C", "A")):
            _swc(model_root, name, runnables=[_runnable("Run")], asil=level)
        _task(model_root, "T", [{"swc": name, "runnable": "Run"} for name in ("A", "B", "C")])

        record = _one(model_root, MIXED)

        assert "3 different ASILs" in record.message

    def test_a_derived_level_counts(self, model_root: Path) -> None:
        _swc(model_root, "Hi", runnables=[_runnable("Run")], asil="D")
        _swc(model_root, "Lo", runnables=[_runnable("Run")])
        _requirement(model_root, "REQ_1", ["Lo"], asil="A")
        _task(model_root, "T", [{"swc": "Hi", "runnable": "Run"}, {"swc": "Lo", "runnable": "Run"}])

        assert "'Lo' (ASIL A)" in _one(model_root, MIXED).message

    def test_qm_beside_a_rated_component_is_mixed(self, model_root: Path) -> None:
        _swc(model_root, "Hi", runnables=[_runnable("Run")], asil="D")
        _swc(model_root, "Qm", runnables=[_runnable("Run")])
        _task(model_root, "T", [{"swc": "Hi", "runnable": "Run"}, {"swc": "Qm", "runnable": "Run"}])

        assert "'Qm' (ASIL QM)" in _one(model_root, MIXED).message

    def test_two_runnables_of_one_component_are_one_component(self, model_root: Path) -> None:
        _swc(model_root, "A", runnables=[_runnable("One"), _runnable("Two")], asil="D")
        _task(model_root, "T", [{"swc": "A", "runnable": "One"}, {"swc": "A", "runnable": "Two"}])

        assert _records(model_root, MIXED) == ()

    def test_an_unknown_swc_reference_contributes_nothing(self, model_root: Path) -> None:
        """`task.mapping-unknown-swc`'s finding, and this rule abstains rather than reporting twice."""
        _swc(model_root, "Hi", runnables=[_runnable("Run")], asil="D")
        _task(model_root, "T", [{"swc": "Hi", "runnable": "Run"}, {"swc": "Ghost", "runnable": "Run"}])

        assert _records(model_root, MIXED) == ()

    def test_a_task_with_no_runnables_is_not_a_finding(self, model_root: Path) -> None:
        _task(model_root, "T", [])

        assert _records(model_root, MIXED) == ()

    def test_the_finding_names_the_coexistence_field_and_every_component(self, mixed: Path) -> None:
        subjects = _one(mixed, MIXED).subjects

        assert Subject("tasks", "T", (field_step("coexistence"),)) in subjects
        assert Subject("swc", "Hi", (field_step("asil"),)) in subjects
        assert Subject("swc", "Lo", (field_step("asil"),)) in subjects


class TestTheTaskRuleCountsLevelsByX:
    """Distinct levels are distinct `X`, so `B` beside `B(D)` is one level.

    Both mixed-level counts are asked — rule 5's own and `safety_reading`'s
    `mixed_tasks`, which the editing guard reads.
    """

    @staticmethod
    def _mixed_tasks(model_root: Path) -> frozenset[str]:
        return frozenset(_safety_reading(model_root).mixed_tasks)

    def _two(self, model_root: Path, first: str, second: str) -> Path:
        _swc(model_root, "One", runnables=[_runnable("Run")], asil=first)
        _swc(model_root, "Two", runnables=[_runnable("Run")], asil=second)
        _task(model_root, "T", [{"swc": "One", "runnable": "Run"}, {"swc": "Two", "runnable": "Run"}])
        return model_root

    def test_a_plain_and_a_decomposed_value_of_one_x_are_not_mixed(self, model_root: Path) -> None:
        self._two(model_root, "B", "B(D)")

        assert _records(model_root, MIXED) == ()
        assert self._mixed_tasks(model_root) == frozenset()

    def test_two_decomposed_values_of_one_x_are_not_mixed(self, model_root: Path) -> None:
        self._two(model_root, "B(C)", "B(D)")

        assert _records(model_root, MIXED) == ()
        assert self._mixed_tasks(model_root) == frozenset()

    def test_a_qm_and_a_decomposed_qm_are_not_mixed(self, model_root: Path) -> None:
        self._two(model_root, "QM", "QM(D)")

        assert _records(model_root, MIXED) == ()
        assert self._mixed_tasks(model_root) == frozenset()

    def test_two_values_of_different_x_are_still_mixed_and_named_as_written(self, model_root: Path) -> None:
        self._two(model_root, "B", "A(D)")

        record = _one(model_root, MIXED)

        assert "2 different ASILs" in record.message
        assert "'One' (ASIL B)" in record.message
        assert "'Two' (ASIL A(D))" in record.message
        assert self._mixed_tasks(model_root) == frozenset({"T"})


class TestTheElementRulesCompareByX:
    """The provider and consumer rules compare a decomposed level by ``X``.

    The fixture's ``ISig.rated`` is ``B``. A tie by ``X`` fires nothing, a
    lower or higher ``X`` fires as a plain one would, and the message names the
    component's value as written.
    """

    @pytest.mark.parametrize(("level", "fires"), [("B(D)", False), ("A(D)", True)])
    def test_the_provider_rule(self, model_root: Path, level: str, fires: bool) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil=level)

        records = _records(model_root, PROVIDER)

        assert len(records) == fires
        if fires:
            assert f"ASIL {level}" in records[0].message

    def test_a_decomposed_element_ties_with_its_plain_x(self, model_root: Path) -> None:
        _sr(model_root, "ISig", [{"name": "rated", "implementation_type": "uint8", "asil": "C(D)"}])
        _swc(model_root, "Src", [_port("p", "provide", "ISig")], asil="C")
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="C")

        assert _records(model_root, PROVIDER) == ()
        assert _records(model_root, CONSUMER) == ()

    @pytest.mark.parametrize(("level", "fires"), [("B(D)", False), ("C(D)", True)])
    def test_the_consumer_rule(self, model_root: Path, level: str, fires: bool) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil=level)

        records = _records(model_root, CONSUMER)

        assert len(records) == fires
        if fires:
            assert f"ASIL {level}" in records[0].message

    def test_the_census_cache_keeps_a_plain_and_a_decomposed_key_apart(self, model_root: Path) -> None:
        """``_elements_below_level`` caches by ``(interface, level)``: ``C`` and ``C(D)`` are two keys, one answer."""
        _swc(model_root, "Plain", [_port("r", "require", "ISig")], asil="C")
        _swc(model_root, "Decomposed", [_port("r", "require", "ISig")], asil="C(D)")
        _swc(model_root, "Tied", [_port("r", "require", "ISig")], asil="B(D)")

        messages = sorted(record.message for record in _records(model_root, CONSUMER))

        assert len(messages) == 2
        assert any("'Decomposed'" in message and "ASIL C(D)" in message for message in messages)
        assert any("'Plain'" in message and "ASIL C" in message for message in messages)


# ---------------------------------------------------------------------------
# 5a. rule:safety.measure-blank — a declared argument that names nothing
# ---------------------------------------------------------------------------


def _speed(model_root: Path) -> None:
    """Write ``ISpeed``: one ASIL-rated element, ``vSpeed``."""
    _sr(model_root, "ISpeed", [{"name": "vSpeed", "implementation_type": "uint8", "asil": "B"}])


def _blank_subject(component: str, port: str, key: str) -> Subject:
    return Subject(
        "swc",
        component,
        (field_step("ports"), member_step(port), field_step("measures"), key_step(key)),
    )


class TestTheBlankMeasureRule:
    def test_row_9s_reproduction_is_one_finding_on_the_entry(self, model_root: Path) -> None:
        """A QM consumer of a rated element, ``measures: {vSpeed: ""}``."""
        _speed(model_root)
        _swc(model_root, "Qm", [_port("rSpeed", "require", "ISpeed", measures={"vSpeed": ""})], asil="QM")

        record = _one(model_root, BLANK)

        assert record.message == (
            "SWC 'Qm' port 'rSpeed' declares a blank measure for 'vSpeed': a measure names the mechanism "
            "that answers a coexistence finding, and an empty text names none. Write the mechanism, or "
            "delete the entry."
        )
        assert record.subjects == (_blank_subject("Qm", "rSpeed", "vSpeed"),)
        assert record.severity == "error"

    @pytest.mark.parametrize("text", ["", "   ", "\t", "\u00a0"], ids=["empty", "spaces", "tab", "nbsp"])
    def test_every_whitespace_only_text_is_the_finding(self, model_root: Path, text: str) -> None:
        _speed(model_root)
        _swc(model_root, "Qm", [_port("rSpeed", "require", "ISpeed", measures={"vSpeed": text})], asil="QM")

        assert len(_records(model_root, BLANK)) == 1

    def test_two_blank_keys_on_one_port_are_two_findings_in_map_order(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "", "bare": " "})], asil="D")

        subjects = [record.subjects for record in _records(model_root, BLANK)]

        assert subjects == [(_blank_subject("Dst", "r", "rated"),), (_blank_subject("Dst", "r", "bare"),)]

    def test_a_blank_text_under_an_unknown_key_is_both_findings(self, model_root: Path) -> None:
        """Two causes: renaming the key leaves the text blank, filling the text leaves the key wrong."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"nosuch": ""})], asil="D")

        assert _one(model_root, BLANK).subjects == (_blank_subject("Dst", "r", "nosuch"),)
        assert _one(model_root, UNKNOWN).subjects == (_blank_subject("Dst", "r", "nosuch"),)

    def test_a_blank_text_on_a_provide_port_is_rule_4s_finding_only(self, model_root: Path) -> None:
        _swc(model_root, "Src", [_port("p", "provide", "ISig", measures={"rated": ""})], asil="D")

        assert _records(model_root, BLANK) == ()
        assert len(_records(model_root, ON_PROVIDE)) == 1

    def test_a_non_blank_text_is_not_this_finding(self, model_root: Path) -> None:
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "E2E profile 4"})], asil="D")

        assert _records(model_root, BLANK) == ()

    def test_a_zero_width_space_is_content_the_stated_limit(self, model_root: Path) -> None:
        """U+200B is not whitespace to ``str.isspace``, so ``str.strip`` keeps it: not reached."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": "\u200b"})], asil="D")

        assert _records(model_root, BLANK) == ()

    def test_an_external_stubs_port_is_left_to_the_stub_rule(self, model_root: Path) -> None:
        _swc(
            model_root,
            "StubSWC",
            [{"name": "rX", "direction": "require", "measures": {"rated": ""}}],
            external=True,
            external_ref="/Foreign/ComponentTypes/StubSWC",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
        )

        assert _records(model_root, BLANK) == ()
        stub = [
            record
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule == "swc.external-with-ports"
        ]
        assert len(stub) == 1

    def test_the_model_with_a_blank_text_still_loads(self, model_root: Path) -> None:
        """The load stays open: the finding is minted, the file is read."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": ""})], asil="D")

        (swc,) = read_swc_model(str(model_root))

        assert swc.ports[0].measures == {"rated": ""}
        assert len(_records(model_root, BLANK)) == 1

    def test_deleting_the_entry_brings_rule_2s_finding_back(self, model_root: Path) -> None:
        """The blank closed rule 2 silently; the repair that deletes it reopens it."""
        _swc(model_root, "Dst", [_port("r", "require", "ISig", measures={"rated": ""})], asil="D")
        assert _records(model_root, CONSUMER) == ()

        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="D")

        assert _records(model_root, BLANK) == ()
        assert len(_records(model_root, CONSUMER)) == 1


def _blank_project(tmp_path: Path) -> Path:
    """``examples/basic`` with a QM consumer of a rated element whose measure is blank."""
    project = tmp_path / "project"
    shutil.copytree(
        REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views", "__pycache__")
    )
    interface = project / "model" / "interfaces" / "IExample.yaml"
    document = yaml.safe_load(interface.read_text(encoding="utf-8"))
    document["data_elements"][0]["asil"] = "B"
    _write(interface, document)
    consumer = project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
    document = yaml.safe_load(consumer.read_text(encoding="utf-8"))
    document["asil"] = "QM"
    document["ports"][0]["measures"] = {"value": ""}
    _write(consumer, document)
    return project


def _cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """One in-process ``swcraft`` run: its exit code, stdout and stderr."""
    monkeypatch.setattr(sys, "argv", ["swcraft", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestTheBlankMeasureThroughTheCommands:
    def test_check_model_exits_1_with_one_finding_on_the_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _blank_project(tmp_path)

        code, out, _ = _cli(
            monkeypatch, capsys, "check-model", "--project", str(project / "swcraft.yaml"), "--format", "json"
        )

        findings = [finding for finding in json.loads(out)["findings"] if finding["rule"] == BLANK]
        assert code == 1
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert "declares a blank measure for 'value'" in findings[0]["message"]

    def test_list_still_reads_the_project_and_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _blank_project(tmp_path)

        code, _, err = _cli(monkeypatch, capsys, "list", "--project", str(project / "swcraft.yaml"))

        assert code == 0, err


# ---------------------------------------------------------------------------
# 6. Duplicate names decide nothing, per rule
# ---------------------------------------------------------------------------


class TestReaderOrderDecidesNothing:
    """Two files declaring one name (`name.duplicate-swc`) let no rule decide by reader order.

    The shape that catches it is two files of one name whose `asil` DIFFERS. The
    reader yields them in **path** order, so `Src_a` is always read first; what
    is parametrized is **which of the two declares the level**: one declares `D`,
    the other declares nothing and derives `A` from `REQ_1`. A name-keyed reading
    takes the LAST file's level, reporting a port of one file with the other's
    level and repairs.
    """

    @staticmethod
    def _duplicate(model_root: Path, declaring: str, ports: dict[str, list[dict[str, Any]]], **extra: Any) -> None:
        """`Src_a` and `Src_b`, of which *declaring* declares `D` and the other derives `A`."""
        for suffix in ("a", "b"):
            _write(
                model_root / "swc" / "group" / f"Src_{suffix}.yaml",
                {
                    "name": "Src",
                    "type": "application",
                    "ports": ports.get(suffix, []),
                    **({"asil": "D"} if suffix == declaring else {}),
                    **extra,
                },
            )
        _requirement(model_root, "REQ_1", ["Src"], asil="A")

    @pytest.mark.parametrize("declaring", ["a", "b"])
    def test_the_provider_rule_reports_the_level_the_ports_own_file_declares(
        self, model_root: Path, declaring: str
    ) -> None:
        """`rated` is `B`, so only the file deriving `A` is below it."""
        deriving = "b" if declaring == "a" else "a"
        self._duplicate(
            model_root,
            declaring,
            {"a": [_port("pa", "provide", "ISig")], "b": [_port("pb", "provide", "ISig")]},
        )

        record = _one(model_root, PROVIDER)

        assert f"'p{deriving}'" in record.message
        assert "(ASIL A)" in record.message
        assert Subject("requirements", "REQ_1", (field_step("asil"),)) in record.subjects

    @pytest.mark.parametrize("declaring", ["a", "b"])
    def test_the_consumer_rule_reports_the_level_the_ports_own_file_declares(
        self, model_root: Path, declaring: str
    ) -> None:
        """The mirror: only the `D` file is rated above the `B` element."""
        self._duplicate(
            model_root,
            declaring,
            {"a": [_port("ra", "require", "ISig")], "b": [_port("rb", "require", "ISig")]},
        )

        record = _one(model_root, CONSUMER)

        assert f"'r{declaring}'" in record.message
        assert "(ASIL D)" in record.message
        # A DECLARED level makes no requirement a legal repair, and the two
        # halves of that answer must come from one reading of one file.
        assert not [subject for subject in record.subjects if subject.layer == "requirements"]

    @pytest.mark.parametrize("declaring", ["a", "b"])
    def test_the_two_measure_rules_read_no_level_and_report_every_declared_port(
        self, model_root: Path, declaring: str
    ) -> None:
        """Rules 3 and 4 compare no level at all, so both files' ports are reported."""
        self._duplicate(
            model_root,
            declaring,
            {
                "a": [_port("ra", "require", "ISig", measures={"typo": "x"})],
                "b": [_port("pb", "provide", "ISig", measures={"rated": "y"})],
            },
        )

        assert "'ra'" in _one(model_root, UNKNOWN).message
        assert "'pb'" in _one(model_root, ON_PROVIDE).message

    @pytest.mark.parametrize("declaring", ["a", "b"])
    def test_the_task_rules_level_and_its_named_requirements_come_from_one_reading(
        self, model_root: Path, declaring: str
    ) -> None:
        """A task names a component by NAME, so it resolves to the FIRST file of that name.

        The level the finding prints and the requirements it names as repairs are
        two readings of one relation, and both must follow that one file.
        """
        self._duplicate(model_root, declaring, {}, runnables=[_runnable("Run")])
        _swc(model_root, "Lo", runnables=[_runnable("Run")], asil="QM")
        _task(model_root, "T", [{"swc": "Src", "runnable": "Run"}, {"swc": "Lo", "runnable": "Run"}])

        record = _one(model_root, MIXED)
        names_the_requirement = Subject("requirements", "REQ_1", (field_step("asil"),)) in record.subjects

        if declaring == "a":
            assert "'Src' (ASIL D)" in record.message
            assert not names_the_requirement
        else:
            assert "'Src' (ASIL A)" in record.message
            assert names_the_requirement


# ---------------------------------------------------------------------------
# 7. The registration: the closed inventory, the protocol's codes, the contract
# ---------------------------------------------------------------------------


class TestTheRulesAreRegisteredEverywhereOnce:
    @pytest.mark.parametrize("rule", RULES)
    def test_the_rule_is_in_the_closed_inventory(self, rule: str) -> None:
        assert rule in CONSISTENCY_RULES

    @pytest.mark.parametrize("rule", RULES)
    def test_the_protocol_maps_it_to_consistency_violation_in_a_group_of_its_own(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION
        assert RULE_CODES[rule].group == rule

    @pytest.mark.parametrize("rule", RULES)
    def test_the_contract_declares_it_cross_element(self, rule: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}
        assert declared[f"rule:{rule}"]["family"] == "cross-element"

    def test_every_emission_site_yields_the_layers_the_contract_declares(self) -> None:
        observed = emission_layers()
        expected = {
            PROVIDER: frozenset({"swc", "interfaces", "requirements"}),
            CONSUMER: frozenset({"swc", "interfaces", "requirements"}),
            UNKNOWN: frozenset({"swc"}),
            ON_PROVIDE: frozenset({"swc"}),
            MIXED: frozenset({"tasks", "swc", "requirements"}),
            BLANK: frozenset({"swc"}),
        }
        for rule, layers in expected.items():
            sites = observed[f"rule:{rule}"]
            assert sites and all(site == layers for site in sites), rule
        assert cross_element_layer_failures(load_contract(), observed) == []


# ---------------------------------------------------------------------------
# 8. The composition: order, and that the aggregate reports all six
# ---------------------------------------------------------------------------


class TestTheComposition:
    def test_the_aggregate_reports_all_six_rules(self) -> None:
        reported = {record.rule for record in check_cross_layer_consistency(str(SAFETY_MODEL_ROOT)).records}

        assert set(RULES) <= reported

    def test_this_checks_six_rules_follow_in_their_own_order(self) -> None:
        """The tuple order is the finding order the shipped vectors freeze.

        The acceptance fixture mints **no** `safety.component-below-requirements`,
        so this says nothing about that rule's place; the next test does, on a root
        where both fire.
        """
        records = check_cross_layer_consistency(str(SAFETY_MODEL_ROOT)).records
        safety = [record.rule for record in records if record.rule.startswith("safety.")]

        # The placement rules follow, from the check appended after this one:
        # three `partition-on-unqualified-core` findings (each task's `D`
        # partition on the `B`-capability `CORE0`), one `memory-below-component`
        # (`TorqueLimiter`'s budget on the `B` `RAM0`), and the per-type rules:
        # `P_Mixed`'s two components below its `D` with no argument, and each
        # partition's `RAM0` below it.
        assert safety == [
            PROVIDER,
            PROVIDER,
            CONSUMER,
            CONSUMER,
            UNKNOWN,
            ON_PROVIDE,
            MIXED,
            BLANK,
            *(["safety.partition-on-unqualified-core"] * 3),
            "safety.memory-below-component",
            *(["safety.component-below-partition"] * 2),
            *(["safety.partition-memory-below-level"] * 3),
        ]

    def test_the_component_asil_rule_comes_first_where_both_it_and_this_check_fire(self, model_root: Path) -> None:
        """`check_component_asil` is appended before `check_safety_coexistence`.

        `Dst` declares `C` under an ASIL-`D` requirement and consumes the `B`-rated
        element with no measure, so both mint something: reordering the composed
        tuple turns this red, where the fixture-order test above stays green.
        """
        _swc(model_root, "Dst", [_port("r", "require", "ISig")], asil="C")
        _requirement(model_root, "REQ_1", ["Dst"], asil="D")

        safety = [
            record.rule
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule.startswith("safety.")
        ]

        assert safety == ["safety.component-below-requirements", CONSUMER]

    def test_the_fixture_mints_no_other_cross_layer_finding_at_all(self) -> None:
        """A finding of any other kind appearing here is the fixture telling you something moved.

        One is expected: ``TorqueLimiter``, placed once in ``Root``, has a runnable
        in each of the three tasks and so in each of their three partitions — one
        instance in three partitions, which the fixture's README names.
        """
        records = check_cross_layer_consistency(str(SAFETY_MODEL_ROOT)).records

        others = [record for record in records if not record.rule.startswith("safety.")]
        assert [record.rule for record in others] == ["task.instance-in-multiple-partitions"]
        assert "SWC 'TorqueLimiter' is placed once" in others[0].message


# ---------------------------------------------------------------------------
# 9. Every coexistence rule fires in the acceptance fixture
# ---------------------------------------------------------------------------


class TestTheFixtureAndItsReadmeAgree:
    def test_every_rule_of_this_slice_fires_in_the_fixture(self) -> None:
        reported = {record.rule for record in check_safety_coexistence(str(SAFETY_MODEL_ROOT)).records}

        assert reported == set(RULES)


class TestTheSafetyReadingIsTheSameQuestionAsTheRULES:
    """`safety_reading` against the record builders, over every fixture.

    :class:`~swcraft_tools.validator.SafetyReading`, the editing guard's reading,
    answers the two closing conditions **per owner** — per `(component, port,
    element)` and per task — because a count keyed without the owner could be
    spent by an unrelated element carrying the same key. Replaying it against
    the findings keeps the guard's question and the check's one question. The
    equivalence is exact: **an argument answers a finding exactly where
    stripping it mints one**, so the owners the reading calls answering are
    counted against the findings the model gains with every argument removed.
    """

    ROOTS = ("fixtures/safety/model", "../../../examples/basic/model")

    @staticmethod
    def _stripped(swcs: Any, tasks: Any) -> tuple[list[Any], list[Any]]:
        """The same components and tasks with every declared argument removed."""
        return (
            [
                swc.model_copy(update={"ports": [port.model_copy(update={"measures": {}}) for port in swc.ports]})
                for swc in swcs
            ],
            [task.model_copy(update={"coexistence": ""}) for task in tasks],
        )

    @pytest.mark.parametrize("relative", ROOTS)
    def test_the_reading_names_exactly_the_owners_whose_arguments_close_a_finding(self, relative: str) -> None:
        from collections import Counter

        from swcraft_tools.reader import (
            read_compositions,
            read_hardware,
            read_interfaces,
            read_requirements,
            read_tasks,
        )
        from swcraft_tools.validator import safety_coexistence_records, safety_reading

        root = str(Path(__file__).resolve().parent / relative)
        swcs, interfaces = read_swc_model(root), read_interfaces(root)
        tasks, requirements = read_tasks(root), read_requirements(root)
        compositions = read_compositions(root)

        reading = safety_reading(swcs, interfaces, tasks, requirements, read_hardware(root), compositions)
        reported = Counter(
            record.rule for record in safety_coexistence_records(swcs, interfaces, tasks, requirements, compositions)
        )
        bare_swcs, bare_tasks = self._stripped(swcs, tasks)
        raw = Counter(
            record.rule
            for record in safety_coexistence_records(bare_swcs, interfaces, bare_tasks, requirements, compositions)
        )
        closed = raw - reported

        answered = sum(len(reading.answering(owner)) for owner in reading.measures)
        argued = sum(1 for task in reading.coexistence if task in reading.mixed_tasks)

        assert answered == closed["safety.consumer-above-element"]
        assert argued == closed["safety.task-mixed-asil"]

    def test_the_fixture_really_carries_an_answered_argument_of_each_kind(self) -> None:
        """Or the equivalence above would hold vacuously on both projects."""
        reading = _safety_reading(SAFETY_MODEL_ROOT)

        # TWO answering owners — `TorqueLimiter.rPlainMeasured`'s named profile
        # and `PlainWorker.rPlainBlank`'s BLANK one, which closes the finding
        # exactly as a named mechanism does because the KEY is what the rule
        # reads — and one answering coexistence text, `Task_Argued`'s.
        assert {owner for owner in reading.measures if reading.answering(owner)} == {
            ("TorqueLimiter", "rPlainMeasured"),
            ("PlainWorker", "rPlainBlank"),
        }
        assert {task for task in reading.coexistence if task in reading.mixed_tasks} == {"Task_Argued"}
        # And the readings really differ from one another, which is what makes
        # the guard's two directions two questions: `TorqueLimiter.rPlain`
        # declares a measure a rule can READ and that answers nothing today —
        # `debug` is unrated — while `DiagnosticsAgent`'s two maps are not
        # readable at all, one on a provide port and one naming no data element.
        assert {owner for owner in reading.measures if reading.readable(owner)} == {
            ("TorqueLimiter", "rPlain"),
            ("TorqueLimiter", "rPlainMeasured"),
            ("PlainWorker", "rPlainBlank"),
        }
        assert set(reading.measures) - {owner for owner in reading.measures if reading.readable(owner)} == {
            ("DiagnosticsAgent", "pDiag"),
            ("DiagnosticsAgent", "rSafety"),
        }
        # The partition's own argument, answered on `P_Argued`
        # (it runs `TorqueLimiter` at D and `SpeedSensor` at derived B under D)
        # and absent from `P_Mixed`, which is mixed and unargued.
        assert reading.partition_levels == {"P_Argued": "D", "P_Homogeneous": "D", "P_Mixed": "D"}
        assert reading.mixed_partitions == {"P_Argued", "P_Mixed"}
        assert {name for name in reading.partition_coexistence if name in reading.mixed_partitions} == {"P_Argued"}

    #: Every committed model with a ``tasks`` layer, and therefore partitions.
    PARTITION_ROOTS = (
        "fixtures/safety/model",
        "fixtures/hardware/model",
        "fixtures/diagrams/model",
        "fixtures/showcase/model",
        "fixtures/recorded_oracles/impact_model",
        "../../../examples/basic/model",
    )

    @pytest.mark.parametrize("relative", PARTITION_ROOTS)
    def test_the_partition_reading_is_the_below_partition_rule_s_own_condition(self, relative: str) -> None:
        """The partition's half of the replay, in both directions.

        ``mixed_partitions`` names exactly the partitions the rule reports with
        every ``coexistence`` stripped, and its argued ones exactly those whose
        argument closes a finding. The record builder is the rule's own,
        :func:`~swcraft_tools.validator.hardware_capability_records`, never the guard.
        """
        self._replay(str(Path(__file__).resolve().parent / relative))

    @staticmethod
    def _adversarial_model(root: Path, shape: str) -> None:
        """A rated partition whose components are mixed, placed where resolution is the question.

        ``TorqueLimiter`` at `D` and ``Monitor`` at `QM` run in one task; what
        moves per *shape* is what the task's ``partition`` name resolves to.
        """
        from conftest import _core, _swc, _task, _write

        _core(root, "CORE0", partition=False)
        _swc(root, "TorqueLimiter", asil="D", runnables=[{"name": "Run"}])
        _swc(root, "Monitor", asil="QM", runnables=[{"name": "Run"}])
        hardware = root / "hardware"
        if shape == "mixed":
            _write(hardware / "P_X.yaml", {"name": "P_X", "kind": "partition", "core": "CORE0", "asil": "D"})
        elif shape == "duplicate, rated second":
            _write(hardware / "P_X.yaml", {"name": "P_X", "kind": "partition", "core": "CORE0"})
            _write(hardware / "P_X~1.yaml", {"name": "P_X", "kind": "partition", "core": "CORE0", "asil": "D"})
        elif shape == "rated on an undeclared core":
            _write(hardware / "P_X.yaml", {"name": "P_X", "kind": "partition", "core": "CORE9", "asil": "D"})
        elif shape == "name resolves to a core":
            _write(hardware / "P_X.yaml", {"name": "P_X", "kind": "core", "threshold": 0.7})
            _write(hardware / "P_X~1.yaml", {"name": "P_X", "kind": "partition", "core": "CORE0", "asil": "D"})
        _task(
            root,
            "T",
            partition="P_X",
            runnables=[{"swc": "TorqueLimiter", "runnable": "Run"}, {"swc": "Monitor", "runnable": "Run"}],
        )

    @pytest.mark.parametrize(
        "shape", ["mixed", "duplicate, rated second", "rated on an undeclared core", "name resolves to a core"]
    )
    def test_the_partition_reading_agrees_with_the_rule_on_adversarial_resolution(
        self, tmp_path: Path, shape: str
    ) -> None:
        """The replay over models built to make the guard and the rule disagree.

        The committed roots leave most partitions unrated; these give a rated, mixed
        partition whose name resolution is the question. ``mixed`` is the control:
        the rule fires there, so the agreement is not vacuous.
        """
        self._adversarial_model(tmp_path, shape)

        raw = self._replay(str(tmp_path))

        if shape == "mixed":
            assert raw == {"P_X"}

    @staticmethod
    def _replay(root: str) -> set[str]:
        """Assert the reading against the rule in both directions; answer the rule's raw set."""
        from swcraft_tools.reader import (
            read_compositions,
            read_hardware,
            read_interfaces,
            read_requirements,
            read_tasks,
        )
        from swcraft_tools.schema import Partition
        from swcraft_tools.validator import hardware_capability_records, safety_reading

        swcs, interfaces = read_swc_model(root), read_interfaces(root)
        tasks, requirements, hardware = read_tasks(root), read_requirements(root), read_hardware(root)
        compositions = read_compositions(root)
        stripped = [
            element.model_copy(update={"coexistence": ""}) if isinstance(element, Partition) else element
            for element in hardware
        ]

        def reporting(layer: list[Any]) -> set[str]:
            return {
                record.subjects[0].element
                for record in hardware_capability_records(layer, tasks, swcs, requirements, compositions)
                if record.rule == "safety.component-below-partition"
            }

        reading = safety_reading(swcs, interfaces, tasks, requirements, hardware, compositions)
        raw, reported = reporting(stripped), reporting(list(hardware))

        assert reading.mixed_partitions == raw
        assert {name for name in reading.partition_coexistence if name in reading.mixed_partitions} == raw - reported
        return raw
