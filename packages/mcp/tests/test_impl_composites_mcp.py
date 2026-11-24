"""``read`` and ``impact_of`` over the implementation composites and the pointer, asked over the wire.

* ``read`` publishes each kind as its file holds it (``full``), keeps the references (``element_type``,
  ``target``) in the summaries, lists and filters the ``elements`` collection, walks a path into a member, and
  refuses a member no element carries and a filter field no member kind declares;
* ``impact_of`` follows each new edge both ways, bounded by ``depth`` (0 refused), and answers a pointer cycle
  without listing the start;
* no reply names where the project is stored: a refused file is named by its storage name, ``read`` names the
  element.

What this module does not reach: the ``index`` form of ``read`` over the kinds, and ``change_set``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import impl_composites_support as composites
import pytest
import swcraft_mcp.server as _server
from protocol_client import call


def _project_at(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    composites.impl_composites_model_root(root)
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(root / "swcraft.yaml"))
    _server._PROJECT = None
    return root


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at the implementation-composite model; return the project root."""
    return _project_at(tmp_path / "impl-composites-project", monkeypatch)


@pytest.fixture()
def cyclic(project: Path) -> Path:
    """The same project with :data:`impl_composites_support.POINTER_CYCLES` added."""
    composites.add_pointer_cycles(project / "model")
    return project


def _read(**arguments: Any) -> dict[str, Any]:
    structured, _texts = call("read", **arguments)
    assert "error" not in structured, structured
    return structured


def _refused(**arguments: Any) -> dict[str, Any]:
    structured, _texts = call("read", **arguments)
    assert structured.get("response") == "error", structured
    return structured


def _impact(**arguments: Any) -> str:
    _structured, texts = call("impact_of", **arguments)
    return "\n".join(texts)


def _dependents(text: str) -> set[str]:
    """The ``layer name`` lines of a ``full`` report, as names."""
    return {
        line.strip().split(" ")[1]
        for line in text.splitlines()
        if line.startswith("  ") and " " in line.strip() and not line.strip().startswith("(")
    }


class TestRead:
    @pytest.mark.parametrize(
        "document",
        [d for d in composites.DATA_TYPES if d["kind"] in {"implementation_record", "implementation_array", "pointer"}],
        ids=lambda d: d["name"],
    )
    def test_each_kind_is_published_as_its_file_holds_it(self, project: Path, document: dict[str, Any]) -> None:
        answer = _read(address={"layer": "implementation_types", "element": document["name"]})

        assert answer["form"] == "full"
        # The full form publishes every field at its default, members' `description` included.
        expected = {**document, "description": "", "package": None}
        if "elements" in document:
            expected["elements"] = [{**element, "description": ""} for element in document["elements"]]
        assert answer["value"] == expected

    def test_the_element_summary_keeps_the_array_s_reference(self, project: Path) -> None:
        answer = _read(address={"layer": "implementation_types", "element": "WheelSpeedHistory_T"}, form="summary")

        assert answer["value"] == {
            "name": "WheelSpeedHistory_T",
            "kind": "implementation_array",
            "element_type": "WheelSpeeds_T",
            "length": 4,
            "element_name": "Sample",
        }

    def test_the_layer_summary_filtered_on_kind_keeps_each_pointer_s_target(self, project: Path) -> None:
        answer = _read(address={"layer": "implementation_types"}, where=[{"field": "kind", "equals": "pointer"}])

        assert answer["form"] == "summary"
        assert answer["value"] == [
            {"name": "WheelSpeeds_Ptr", "kind": "pointer", "target": "WheelSpeeds_T"},
            {"name": "WheelSpeeds_PtrPtr", "kind": "pointer", "target": "WheelSpeeds_Ptr"},
        ]

    def test_the_elements_collection_is_read_as_members_and_filtered_on_type(self, project: Path) -> None:
        answer = _read(
            address={"layer": "implementation_types"},
            members="elements",
            where=[{"field": "type", "equals": "WheelSpeeds_Ptr"}],
        )

        assert answer["value"] == [{"element": "Handle_T", "member": {"name": "target", "type": "WheelSpeeds_Ptr"}}]

    def test_a_path_walks_into_one_member_s_type(self, project: Path) -> None:
        path = [{"field": "elements"}, {"member": "front"}, {"field": "type"}]
        answer = _read(address={"layer": "implementation_types", "element": "WheelSpeeds_T", "path": path})

        assert answer["value"] == "uint16"

    def test_a_member_no_element_carries_is_a_broken_reference(self, project: Path) -> None:
        path = [{"field": "elements"}, {"member": "middle"}]
        answer = _refused(address={"layer": "implementation_types", "element": "WheelSpeeds_T", "path": path})

        assert answer["error"] == "broken-reference"

    def test_a_filter_field_no_member_kind_declares_is_an_unknown_field(self, project: Path) -> None:
        answer = _refused(
            address={"layer": "implementation_types"}, members="elements", where=[{"field": "length", "equals": 4}]
        )

        assert answer["error"] == "unknown-field"


class TestImpactOf:
    def test_the_first_ring_of_a_record_s_dependents_is_one_per_new_edge(self, project: Path) -> None:
        text = _impact(name="WheelSpeeds_T", form="full", depth=1)

        assert _dependents(text) == {
            "WheelSpeeds_Ref",
            "WheelSpeedHistory_T",
            "WheelSpeeds_Ptr",
            "WheelSpeedMaps",
            "IExample",
        }
        assert "(depth: 1)" in text

    def test_the_transitive_walk_crosses_the_pointer_and_the_alias(self, project: Path) -> None:
        names = _dependents(_impact(name="WheelSpeeds_T", form="full"))

        assert {"Handle_T", "WheelSpeeds_PtrPtr", "Frame_T", "Frame_Ref", "IDiagnosticService"} <= names

    def test_the_dependencies_of_a_pointer_to_a_pointer_are_the_whole_chain(self, project: Path) -> None:
        text = _impact(name="WheelSpeeds_PtrPtr", direction="dependencies")

        # The base type at the end of the chain is a `base_type`.
        assert "implementation_type (3): WheelSpeeds_Ptr, WheelSpeeds_T, uint16" in text, text
        assert "base_type (1): uint16_base" in text, text

    def test_depth_zero_is_refused(self, project: Path) -> None:
        assert _impact(name="WheelSpeeds_T", depth=0).startswith("ERROR: depth must be at least 1")

    def test_a_two_node_cycle_never_answers_the_start(self, cyclic: Path) -> None:
        """``Node_T``'s other element is a ``uint8``: the walk reaches it and still never lists ``Node_T``."""
        assert _dependents(_impact(name="Node_T", form="full")) == {"Node_Ptr"}
        assert _dependents(_impact(name="Node_T", form="full", direction="dependencies")) == {
            "Node_Ptr",
            "uint8",
            "uint8_base",
        }

    @pytest.mark.parametrize("start", ["Ring_Rec", "Ring_T", "Ring_Ptr"])
    def test_a_three_node_ring_answers_the_other_two(self, cyclic: Path, start: str) -> None:
        others = {"Ring_Rec", "Ring_T", "Ring_Ptr"} - {start}

        assert _dependents(_impact(name=start, form="full")) == others
        assert _dependents(_impact(name=start, form="full", direction="dependencies")) == others


class TestNoReplyNamesWhereTheProjectIsStored:
    @pytest.fixture()
    def broken(self, project: Path) -> Path:
        composites.data_type(
            project / "model",
            {
                "name": "Spare_T",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 0,
                "element_name": "B",
            },
        )
        return project

    def test_impact_of_names_the_refused_file_by_its_storage_name(self, broken: Path) -> None:
        text = _impact(name="WheelSpeeds_T")

        assert text.startswith("ERROR: Invalid implementation type YAML in data_types/implementation/Spare_T.yaml: "), (
            text
        )
        assert str(broken) not in text and broken.as_posix() not in text and broken.name not in text

    def test_a_refusal_of_two_fields_is_one_error_line(self, project: Path) -> None:
        """The reply's ``ERROR:`` keeps one line, the details joined by ``; ``."""
        composites.data_type(
            project / "model",
            {
                "name": "Spare_T",
                "kind": "implementation_array",
                "element_type": 5,
                "length": 0,
                "element_name": "B",
            },
        )
        text = _impact(name="WheelSpeeds_T")

        assert text == (
            "ERROR: Invalid implementation type YAML in data_types/implementation/Spare_T.yaml: "
            "element_type: Input should be a valid string; length: Value error, length must be > 0"
        )

    def test_read_names_the_element_and_no_file(self, broken: Path) -> None:
        answer = _refused(address={"layer": "implementation_types", "element": "WheelSpeeds_T"})

        assert answer["error"] == "consistency-violation"
        assert answer["addresses"] == [{"layer": "implementation_types", "element": "Spare_T"}]
        assert ".yaml" not in answer["message"] and broken.name not in answer["message"]
