"""The acceptance suite for a runnable's access points.

Every access-point edge case sits in one project -- `examples/basic` plus the declarations that make each
case exist -- run through the shipped `check_cross_layer_consistency` (what `swcraft check-model` calls)
**and** `generate_arxml`: a model the gate accepts is one the writer writes, an offender the writer
refuses is one the gate reports, and the two flip together one character or one rename either side of a
boundary. The unit evidence of each case stays in its own module (`test_arxml_access_writer.py`,
`test_validator_identifier.py`, `test_validator_name_uniqueness.py`, `test_arxml_xsd_conformance.py`).
Every acceptance carries the perturbation that measures it.

Does not reach:

* the schemas: XSD validation is `test_arxml_xsd_conformance.py`'s stage (`python scripts/dev.py test-xsd`);
* the exact bytes of a point: the hand-derived frozen fragments are `test_arxml_access_writer.py`'s; this
  module compares nothing byte-wise;
* real tool exports (behind `SWCRAFT_RHAPSODY_EXPORT` and the `slow` marker);
* the exchange: `import component`'s regimes are `test_component_import.py`'s;
* a digest: this project is `examples/basic` *plus* declarations, so the digest witness is
  `test_arxml_components_writer.py::TestExamplesBasicDeclaresNoAccessAndEmitsNone`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, golden_slice
from swcraft_tools.capability_markers import ACCESS_POINTS, SDG_GID
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    READ_ACCESS_PREFIX,
    WRITE_ACCESS_PREFIX,
    access_point_added_length,
    access_point_name,
    generate_arxml,
)
from test_arxml_golden import TestSemanticRoundTrip as SemanticRoundTripOracle
from test_arxml_golden import _access_sets

#: The project every test here starts from: a model that already passes `check-model`, so any finding a
#: test sees is the one it wrote.
BASIC_PROJECT = REPO_ROOT / "examples" / "basic"

#: `examples/basic`'s own interfaces. The SR one carries the read and the write,
#: the CS one the call — neither is invented here.
SR_INTERFACE = "IExample"
CS_INTERFACE = "IDiagnosticService"

#: `examples/basic`'s own data element, and a second so that one port can carry a by-argument read *and* a
#: by-value read -- the split is the container's, never the port's.
SR_ELEMENT = "value"
BY_VALUE_ELEMENT = "level"

#: The operation the call points at, `IDiagnosticService`'s own.
CS_OPERATION = "ReadDtc"

#: What each form leaves the two declared names **between them** — derived from
#: the writer's own arithmetic, never restated as literals.
ACCESS_BUDGETS = {
    prefix: MAX_IDENTIFIER_LENGTH - access_point_added_length(prefix)
    for prefix in (READ_ACCESS_PREFIX, WRITE_ACCESS_PREFIX, CALL_POINT_PREFIX)
}


def _port_on_the_budget(prefix: str, member: str, seed: str) -> str:
    """A port name putting `len(port) + len(member)` exactly on *prefix*'s budget.

    The port is lengthened because a data element name also reaches every com-spec on its interface and the
    interface's `{Interface}_{Element}_Init` joint budget.
    """
    return seed + "A" * (ACCESS_BUDGETS[prefix] - len(member) - len(seed))


#: The three pairs sitting exactly on their budget, in the base project: the
#: `{prefix: (port, member, list)}` the boundary runnable declares.
BOUNDARY_ENTRIES: dict[str, tuple[str, str, str]] = {
    READ_ACCESS_PREFIX: (_port_on_the_budget(READ_ACCESS_PREFIX, SR_ELEMENT, "rBudget"), SR_ELEMENT, "reads"),
    WRITE_ACCESS_PREFIX: (_port_on_the_budget(WRITE_ACCESS_PREFIX, SR_ELEMENT, "pBudget"), SR_ELEMENT, "writes"),
    CALL_POINT_PREFIX: (_port_on_the_budget(CALL_POINT_PREFIX, CS_OPERATION, "rCallBudget"), CS_OPERATION, "calls"),
}

#: The interface the collision cases add, and the two ports on it whose names
#: differ only in where the underscore falls.
COLLIDING_INTERFACE = "ICollide"
COLLIDING_PORTS = ("a_b", "a")


def _write(model_root: Path, layer: str, stem: str, document: Any) -> None:
    """Write one element file in the canonical writer form."""
    directory = model_root / layer
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.yaml").write_text(
        yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


def _read(model_root: Path, layer: str, stem: str) -> Any:
    return yaml.safe_load((model_root / layer / f"{stem}.yaml").read_text(encoding="utf-8"))


def _copy_basic(tmp_path: Path) -> Path:
    """Copy `examples/basic`'s manifest and model into *tmp_path*; return the project."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASIC_PROJECT / "swcraft.yaml", project / "swcraft.yaml")
    shutil.copytree(BASIC_PROJECT / "model", project / "model")
    return project


def _consumer_document() -> dict[str, Any]:
    """`ExampleConsumerSWC`, the component that declares -- every case on it.

    `ExampleSWC` stays as `examples/basic` ships it, so the marker's absence is a fact about a component.
    """
    return {
        "name": "ExampleConsumerSWC",
        "type": "application",
        "description": "Example software component consuming data from ExampleSWC",
        "ports": [
            {"name": "rIn", "direction": "require", "interface": SR_INTERFACE},
            {"name": "pEcho", "direction": "provide", "interface": SR_INTERFACE},
            {"name": "rDiag", "direction": "require", "interface": CS_INTERFACE},
            *(
                {
                    "name": port,
                    "direction": "provide" if prefix == WRITE_ACCESS_PREFIX else "require",
                    "interface": CS_INTERFACE if prefix == CALL_POINT_PREFIX else SR_INTERFACE,
                }
                for prefix, (port, _member, _list_name) in BOUNDARY_ENTRIES.items()
            ),
        ],
        "runnables": [
            # Every shape at once: a by-argument and a by-value read on one port, a write, and a call.
            {
                "name": "ExampleConsumerRun",
                "min_start_interval": 0.01,
                "events": [{"type": "timing", "period": 0.01}],
                "reads": [
                    {"port": "rIn", "element": SR_ELEMENT},
                    {"port": "rIn", "element": BY_VALUE_ELEMENT, "by": "value"},
                ],
                "writes": [{"port": "pEcho", "element": SR_ELEMENT}],
                "calls": [{"port": "rDiag", "operation": CS_OPERATION}],
            },
            # A runnable declaring nothing inside the declaring component: "this component emits nothing"
            # cannot satisfy it.
            {"name": "ConsumerIdle", "events": [{"type": "timing", "period": 0.02}]},
            {
                "name": "ConsumerSingle",
                "events": [{"type": "timing", "period": 0.05}],
                "reads": [{"port": "rIn", "element": SR_ELEMENT}],
            },
            {
                "name": "ConsumerBoundary",
                "events": [{"type": "timing", "period": 0.1}],
                "reads": [{"port": BOUNDARY_ENTRIES[READ_ACCESS_PREFIX][0], "element": SR_ELEMENT}],
                "writes": [{"port": BOUNDARY_ENTRIES[WRITE_ACCESS_PREFIX][0], "element": SR_ELEMENT}],
                "calls": [{"port": BOUNDARY_ENTRIES[CALL_POINT_PREFIX][0], "operation": CS_OPERATION}],
            },
        ],
        "memory": {"RAM0": 1536},
        # As `examples/basic` ships it: a component without the value is refused.
        "handle_termination_and_restart": "no_support",
    }


@pytest.fixture
def access_project(tmp_path: Path) -> Path:
    """`examples/basic` plus every access-point case this module tests."""
    project = _copy_basic(tmp_path)
    model_root = project / "model"
    interface = _read(model_root, "interfaces", SR_INTERFACE)
    interface["data_elements"].append({"name": BY_VALUE_ELEMENT, "implementation_type": "uint8"})
    _write(model_root, "interfaces", SR_INTERFACE, interface)
    _write(model_root, "swc/example", "ExampleConsumerSWC", _consumer_document())
    return project


def _generate(project: Path, version: str = "4.2.2") -> str:
    """Generate the project's ARXML at *version* and return its text."""
    output = project / "dist" / "model.arxml"
    generate_arxml(str(project / "model"), str(output), version=version)
    return output.read_text(encoding="utf-8")


def _rules(project: Path, prefix: str = "") -> list[str]:
    """Every rule the shipped gate mints on the project, filtered by *prefix*."""
    result = check_cross_layer_consistency(str(project / "model"))
    return [record.rule for record in result.records if record.rule.startswith(prefix)]


def _records(project: Path, rule: str) -> list:
    result = check_cross_layer_consistency(str(project / "model"))
    return [record for record in result.records if record.rule == rule]


def _writer_refusal(project: Path) -> str:
    """Generate *project* expecting a refusal; return the message (one `ValueError` batches every offender)."""
    with pytest.raises(ValueError) as excinfo:
        generate_arxml(str(project / "model"), str(project / "dist" / "model.arxml"))
    return str(excinfo.value)


def _runnable(document: str, name: str) -> str:
    """The `RUNNABLE-ENTITY` fragment of *name*, dedented."""
    return golden_slice(document, name, tag="RUNNABLE-ENTITY")


def _points(document: str, runnable: str, container: str) -> list[str]:
    """Every `SHORT-NAME` inside *container* under *runnable*, in emitted order."""
    fragment = _runnable(document, runnable)
    if f"<{container}>" not in fragment:
        return []
    inner = fragment.split(f"<{container}>", 1)[1].split(f"</{container}>", 1)[0]
    return [
        line.split("<SHORT-NAME>")[1].split("</SHORT-NAME>")[0] for line in inner.splitlines() if "<SHORT-NAME>" in line
    ]


def _rewrite_consumer(project: Path, mutate) -> None:
    """Apply *mutate* to the consumer document and write it back."""
    document = _read(project / "model", "swc/example", "ExampleConsumerSWC")
    mutate(document)
    _write(project / "model", "swc/example", "ExampleConsumerSWC", document)


def _runnable_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(runnable for runnable in document["runnables"] if runnable["name"] == name)


# 1. The project the gate accepts


class TestTheAccessProjectPassesCheckModel:
    """Section 1: the shipped gate accepts the whole project.

    Every assertion is about `check_cross_layer_consistency`, never one of its checks alone; each check has
    a module of its own.
    """

    def test_the_whole_project_is_accepted(self, access_project: Path) -> None:
        result = check_cross_layer_consistency(str(access_project / "model"))

        assert result.consistent, result.issues

    def test_the_project_really_declares_every_shape(self, access_project: Path) -> None:
        """Non-vacuity: an acceptance over a model declaring nothing would pass."""
        document = _read(access_project / "model", "swc/example", "ExampleConsumerSWC")
        declared = {
            runnable["name"]: {key for key in ("reads", "writes", "calls") if runnable.get(key)}
            for runnable in document["runnables"]
        }

        assert declared == {
            "ExampleConsumerRun": {"reads", "writes", "calls"},
            "ConsumerIdle": set(),
            "ConsumerSingle": {"reads"},
            "ConsumerBoundary": {"reads", "writes", "calls"},
        }
        assert any(entry.get("by") == "value" for entry in _runnable_of(document, "ExampleConsumerRun")["reads"]), (
            "the by-value read is what makes the second receive container reachable"
        )

    @pytest.mark.parametrize(
        ("case", "rule"),
        [
            ("unknown-port", "swc.access.unknown-port"),
            ("unknown-element", "swc.access.unknown-element"),
            ("unknown-operation", "swc.access.unknown-operation"),
            ("write-through-require", "swc.access.direction-mismatch"),
            ("read-through-client-server", "swc.access.interface-kind-mismatch"),
        ],
    )
    def test_each_declaration_is_load_bearing(self, access_project: Path, case: str, rule: str) -> None:
        """Five one-word edits each turn the gate red with its own rule, so the entries are known to resolve."""

        def mutate(document: dict[str, Any]) -> None:
            main = _runnable_of(document, "ExampleConsumerRun")
            if case == "unknown-port":
                main["reads"][0]["port"] = "rNotAPort"
            elif case == "unknown-element":
                main["reads"][0]["element"] = "notAnElement"
            elif case == "unknown-operation":
                main["calls"][0]["operation"] = "NotAnOperation"
            elif case == "write-through-require":
                main["writes"][0]["port"] = "rIn"
            else:
                main["reads"][0]["port"] = "rDiag"

        _rewrite_consumer(access_project, mutate)

        assert _rules(access_project, "swc.access.") == [rule]


# 2. The same project, through the writer


class TestTheAccessProjectIsWrittenAndComesBack:
    """Section 2: the access project is written and its entries come back.

    `generate_arxml` writes one point per entry in the container its list and form select, and the reader brings the
    entries back.
    """

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_every_entry_becomes_exactly_one_point(self, access_project: Path, version: str) -> None:
        document = _generate(access_project, version)

        assert _points(document, "ExampleConsumerRun", "DATA-RECEIVE-POINT-BY-ARGUMENTS") == [
            access_point_name(READ_ACCESS_PREFIX, "rIn", SR_ELEMENT)
        ]
        assert _points(document, "ExampleConsumerRun", "DATA-RECEIVE-POINT-BY-VALUES") == [
            access_point_name(READ_ACCESS_PREFIX, "rIn", BY_VALUE_ELEMENT)
        ]
        assert _points(document, "ExampleConsumerRun", "DATA-SEND-POINTS") == [
            access_point_name(WRITE_ACCESS_PREFIX, "pEcho", SR_ELEMENT)
        ]
        assert _points(document, "ExampleConsumerRun", "SERVER-CALL-POINTS") == [
            access_point_name(CALL_POINT_PREFIX, "rDiag", CS_OPERATION)
        ]

    def test_the_runnable_declaring_nothing_carries_no_container_at_all(self, access_project: Path) -> None:
        """A runnable with no list, inside a component that declares, carries none of the four access containers."""
        fragment = _runnable(_generate(access_project), "ConsumerIdle")

        for container in (
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "DATA-RECEIVE-POINT-BY-VALUES",
            "DATA-SEND-POINTS",
            "SERVER-CALL-POINTS",
        ):
            assert container not in fragment

    def test_the_runnable_with_one_read_carries_that_one_container(self, access_project: Path) -> None:
        fragment = _runnable(_generate(access_project), "ConsumerSingle")

        assert "DATA-RECEIVE-POINT-BY-ARGUMENTS" in fragment
        assert "DATA-RECEIVE-POINT-BY-VALUES" not in fragment
        assert "DATA-SEND-POINTS" not in fragment
        assert "SERVER-CALL-POINTS" not in fragment

    def test_dropping_by_value_moves_the_point_to_the_other_container(self, access_project: Path) -> None:
        """Dropping `by: value` moves the same pair to the other container: guards a writer flattening `by: value`."""

        def mutate(document: dict[str, Any]) -> None:
            for entry in _runnable_of(document, "ExampleConsumerRun")["reads"]:
                entry.pop("by", None)

        _rewrite_consumer(access_project, mutate)
        document = _generate(access_project)

        assert _points(document, "ExampleConsumerRun", "DATA-RECEIVE-POINT-BY-VALUES") == []
        assert _points(document, "ExampleConsumerRun", "DATA-RECEIVE-POINT-BY-ARGUMENTS") == [
            access_point_name(READ_ACCESS_PREFIX, "rIn", SR_ELEMENT),
            access_point_name(READ_ACCESS_PREFIX, "rIn", BY_VALUE_ELEMENT),
        ]

    def test_the_call_point_carries_the_zero_timeout(self, access_project: Path) -> None:
        fragment = golden_slice(_generate(access_project), access_point_name(CALL_POINT_PREFIX, "rDiag", CS_OPERATION))

        assert "<TIMEOUT>0</TIMEOUT>" in fragment
        assert "SYNCHRONOUS-SERVER-CALL-POINT" in fragment

    def test_the_declaring_component_is_marked_and_the_quiet_one_is_not(self, access_project: Path) -> None:
        """The marker is per component: `ExampleConsumerSWC` carries it, the untouched `ExampleSWC` does not."""
        document = _generate(access_project)
        marker = f'<SDG GID="{SDG_GID}">'

        assert marker in golden_slice(document, "ExampleConsumerSWC_InternalBehavior")
        assert ACCESS_POINTS.sd_gid in golden_slice(document, "ExampleConsumerSWC_InternalBehavior")
        assert "ADMIN-DATA" not in golden_slice(document, "ExampleSWC_InternalBehavior")

    def test_giving_the_quiet_component_one_read_marks_it_too(self, access_project: Path) -> None:
        """The marker follows the points, not the component: one write on `ExampleSWC`'s `pOut` marks it."""
        document = _read(access_project / "model", "swc/example", "ExampleSWC")
        _runnable_of(document, "ExampleRun")["writes"] = [{"port": "pOut", "element": SR_ELEMENT}]
        _write(access_project / "model", "swc/example", "ExampleSWC", document)

        emitted = _generate(access_project)

        assert f'<SDG GID="{SDG_GID}">' in golden_slice(emitted, "ExampleSWC_InternalBehavior")

    def test_every_runnables_entry_set_comes_back_equal(self, access_project: Path, tmp_path: Path) -> None:
        """The round-trip oracle on this project: the entry sets per runnable, read off the Pydantic objects.

        The oracle is imported from `test_arxml_golden.py` (under a non-`Test` alias, so pytest does not
        collect it twice), so this project and the fixture root are read back by one pipeline.
        """
        output = tmp_path / "round-trip.arxml"
        generate_arxml(str(access_project / "model"), str(output))
        swcs, _interfaces, _data_types, _connectors, skips, _mapped = SemanticRoundTripOracle._round_trip(str(output))
        source = _read(access_project / "model", "swc/example", "ExampleConsumerSWC")

        assert [f"{entry.tag} at {entry.path}: {entry.reason}" for entry in skips] == []
        returned = _access_sets(swcs)
        assert returned[("ExampleConsumerSWC", "ConsumerIdle")] == set()
        assert returned[("ExampleConsumerSWC", "ExampleConsumerRun")] == {
            ("reads", "rIn", SR_ELEMENT, None),
            ("reads", "rIn", BY_VALUE_ELEMENT, "value"),
            ("writes", "pEcho", SR_ELEMENT, None),
            ("calls", "rDiag", CS_OPERATION, None),
        }
        assert returned[("ExampleConsumerSWC", "ConsumerBoundary")] == {
            (list_name, port, member, None) for port, member, list_name in BOUNDARY_ENTRIES.values()
        }
        assert len(_runnable_of(source, "ExampleConsumerRun")["reads"]) == 2


# 3. The two name rules, across both surfaces, on this project


class TestTheTwoNameRulesAgreeAcrossBothSurfaces:
    """`check-model` and `generate_arxml` agree on the two name rules.

    The three forms' joint budgets and the underscore collision: `check-model` reports and `generate_arxml` refuses the
    same offender, and one character or one rename flips both.
    """

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGETS), ids=repr)
    def test_a_pair_exactly_on_the_budget_is_accepted_by_both(self, access_project: Path, prefix: str) -> None:
        port, member, _list_name = BOUNDARY_ENTRIES[prefix]
        emitted = access_point_name(prefix, port, member)

        assert len(port) + len(member) == ACCESS_BUDGETS[prefix]
        assert len(emitted) == MAX_IDENTIFIER_LENGTH
        assert _rules(access_project) == []

        document = _generate(access_project)

        assert f"<SHORT-NAME>{emitted}</SHORT-NAME>" in document

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGETS), ids=repr)
    def test_one_character_more_is_refused_by_both_naming_both_names(self, access_project: Path, prefix: str) -> None:
        """One character more on the port is refused by both; each name alone is legal, so it is the pair's."""
        port, member, _list_name = BOUNDARY_ENTRIES[prefix]
        over_budget = f"{port}A"
        swc_file = access_project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
        swc_file.write_text(
            swc_file.read_text(encoding="utf-8").replace(port, over_budget),
            encoding="utf-8",
            newline="\n",
        )
        emitted = access_point_name(prefix, over_budget, member)

        reported = _records(access_project, "identifier.generated")

        assert _rules(access_project) == ["identifier.generated"]
        assert emitted in reported[0].message
        assert f"'{over_budget}'" in reported[0].message
        assert f"'{member}'" in reported[0].message
        assert f"{ACCESS_BUDGETS[prefix]} characters between them" in reported[0].message

        message = _writer_refusal(access_project)

        assert emitted in message
        assert over_budget in message
        assert member in message
        assert not (access_project / "dist" / "model.arxml").exists(), "a refused model leaves no output"

    @staticmethod
    def _collide(project: Path, second_element: str) -> str:
        """Two ports of one runnable whose pairs put one string on both derived names (`a_b` + `c`, `a` + `b_c`).

        Returns the name the first pair derives.
        """
        _write(
            project / "model",
            "interfaces",
            COLLIDING_INTERFACE,
            {
                "name": COLLIDING_INTERFACE,
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "c", "implementation_type": "uint8"},
                    {"name": second_element, "implementation_type": "uint8"},
                ],
            },
        )

        def mutate(document: dict[str, Any]) -> None:
            document["ports"].extend(
                {"name": port, "direction": "require", "interface": COLLIDING_INTERFACE} for port in COLLIDING_PORTS
            )
            _runnable_of(document, "ExampleConsumerRun")["reads"].extend(
                [
                    {"port": COLLIDING_PORTS[0], "element": "c"},
                    {"port": COLLIDING_PORTS[1], "element": second_element},
                ]
            )

        _rewrite_consumer(project, mutate)
        return access_point_name(READ_ACCESS_PREFIX, COLLIDING_PORTS[0], "c")

    def test_two_pairs_deriving_one_name_are_refused_by_both_naming_it(self, access_project: Path) -> None:
        """Port `a_b` element `c` and port `a` element `b_c` both derive `R_a_b_c`: both surfaces refuse it."""
        shared = self._collide(access_project, "b_c")

        assert shared == access_point_name(READ_ACCESS_PREFIX, COLLIDING_PORTS[1], "b_c")

        reported = _records(access_project, "swc.access.name-collision")

        assert _rules(access_project) == ["swc.access.name-collision"]
        assert shared in reported[0].message
        assert f"'{COLLIDING_PORTS[0]}'" in reported[0].message
        assert "'b_c'" in reported[0].message

        message = _writer_refusal(access_project)

        assert shared in message
        assert not (access_project / "dist" / "model.arxml").exists(), "a refused model leaves no output"

    def test_renaming_one_of_the_two_elements_makes_both_accept_it_again(self, access_project: Path) -> None:
        """`b_d` moves the underscore and both surfaces accept the model: the refusal above is the collision's."""
        first = self._collide(access_project, "b_d")

        assert _rules(access_project) == []

        document = _generate(access_project)

        assert f"<SHORT-NAME>{first}</SHORT-NAME>" in document
        assert (
            f"<SHORT-NAME>{access_point_name(READ_ACCESS_PREFIX, COLLIDING_PORTS[1], 'b_d')}</SHORT-NAME>" in document
        )
