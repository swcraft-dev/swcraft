"""The read-only commands over the implementation composites and the pointer.

``list``, ``query`` and ``impact`` read each layer's declared identity, fields and
references, never a fixed set of kinds, so the three new reference fields need no code of
their own; this module is the evidence, end to end through ``swcraft``'s ``main()`` (the
exit code is the command's own) on ``impl_composites_support``'s model. A pointer graph that
closes on itself is walked and answered, exit 0: ``check-model`` refuses it, the read-only
commands are not the gate. ``query`` refuses, exit 1, a collection the model does not
declare, a field no member kind declares and ``--depth 0``; ``impact --depth 0`` is
argparse's usage error, exit 2, raised before the command runs. The MCP answers over the same model are
``packages/mcp/tests/test_impl_composites_mcp.py``'s.

What this module does not reach: a walk's per-edge ``detail`` — each new row renders none,
so a reader of ``impact`` is not told which member of a record names the type; ``diff`` over
the kinds (``test_differ.py``'s); and any model shape the helper does not build.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import impl_composites_support as composites
import pytest
from conftest import model_element_path
from swcraft_tools.cli import main

NEW_KINDS: dict[str, set[str]] = {
    "implementation_record": {"WheelSpeeds_T", "Frame_T", "Handle_T"},
    "implementation_array": {"WheelSpeedHistory_T", "Spare_T"},
    "pointer": {"WheelSpeeds_Ptr", "WheelSpeeds_PtrPtr"},
}


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    """The implementation-composite project; its manifest."""
    return composites.impl_composites_model_root(tmp_path / "impl-composites-project").parent / "swcraft.yaml"


@pytest.fixture()
def cyclic(manifest: Path) -> Path:
    """The same project with :data:`impl_composites_support.POINTER_CYCLES` added; its manifest."""
    composites.add_pointer_cycles(manifest.parent / "model")
    return manifest


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Run ``swcraft`` with *argv*; return ``(exit code, stdout, stderr)``."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _json(argv: list[str], manifest: Path, monkeypatch, capsys) -> dict[str, Any]:
    code, out, err = _run([*argv, "--project", str(manifest), "--format", "json"], monkeypatch, capsys)
    assert code == 0, (out, err)
    return json.loads(out)


def _names(document: dict[str, Any]) -> list[str]:
    """The identity of every element a slice document selected, in its order."""
    return list(document["elements"])


def _members(document: dict[str, Any]) -> list[tuple[str, str]]:
    return [(row["element"], row["member"]) for row in document["elements"]]


def _walked(document: dict[str, Any]) -> set[str]:
    """Every name a walk document matched, over every layer."""
    return {row["name"] for rows in document["matched"].values() for row in rows}


def _walk(argv: list[str], manifest: Path, monkeypatch, capsys) -> set[str]:
    return _walked(_json(argv, manifest, monkeypatch, capsys))


class TestList:
    def test_every_element_of_the_three_kinds_is_listed(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out, _err = _run(["list", "implementation_types", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        listed = {
            line.removeprefix("implementation_types: ")
            for line in out.splitlines()
            if line.startswith("implementation_types: ")
        }
        assert set().union(*NEW_KINDS.values()) <= listed
        assert {"WheelSpeeds_Ref", "Frame_Ref"} <= listed

        # Contract 8.0.0: the application record and array the composites map
        # with are elements of `application_types`, listed under that layer.
        code, out, _err = _run(["list", "application_types", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        listed = {
            line.removeprefix("application_types: ")
            for line in out.splitlines()
            if line.startswith("application_types: ")
        }
        assert {"WheelSpeeds", "WheelSpeedHistory"} <= listed


class TestTheQuerySlice:
    @pytest.mark.parametrize("kind", sorted(NEW_KINDS))
    def test_where_selects_each_kind(self, manifest: Path, monkeypatch, capsys, kind: str) -> None:
        document = _json(["query", "implementation_types", "--where", f"kind={kind}"], manifest, monkeypatch, capsys)

        assert set(_names(document)) == NEW_KINDS[kind]

    def test_the_elements_collection_is_listed_as_members_in_declared_order(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        document = _json(["query", "implementation_types", "--members", "elements"], manifest, monkeypatch, capsys)

        assert _members(document) == [
            ("Frame_T", "speeds"),
            ("Frame_T", "count"),
            ("Handle_T", "target"),
            ("WheelSpeeds_T", "front"),
            ("WheelSpeeds_T", "rear"),
        ]

    def test_a_member_filter_on_type_selects_the_member_that_names_the_pointer(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        document = _json(
            ["query", "implementation_types", "--members", "elements", "--where", "type=WheelSpeeds_Ptr"],
            manifest,
            monkeypatch,
            capsys,
        )

        assert _members(document) == [("Handle_T", "target")]

    def test_the_text_form_names_the_member_beside_its_owner(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out, _err = _run(
            [
                "query",
                "implementation_types",
                "--members",
                "elements",
                "--where",
                "type=uint8",
                "--project",
                str(manifest),
            ],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert "implementation_types: Frame_T.count" in out.splitlines()

    def test_exists_on_target_selects_the_pointers_alone(self, manifest: Path, monkeypatch, capsys) -> None:
        document = _json(["query", "implementation_types", "--exists", "target"], manifest, monkeypatch, capsys)

        assert set(_names(document)) == NEW_KINDS["pointer"]

    def test_where_on_the_array_s_element_type_selects_the_array(self, manifest: Path, monkeypatch, capsys) -> None:
        document = _json(
            ["query", "implementation_types", "--where", "element_type=WheelSpeeds_T"], manifest, monkeypatch, capsys
        )

        assert _names(document) == ["WheelSpeedHistory_T"]

    def test_a_collection_the_model_does_not_declare_is_refused(self, manifest: Path, monkeypatch, capsys) -> None:
        """``sub_elements`` is the ARXML tag, not the model's collection: exit 1, never an empty answer."""
        code, out, err = _run(
            ["query", "implementation_types", "--members", "sub_elements", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 1, (out, err)
        assert "sub_elements" in out + err

    def test_a_field_no_member_kind_declares_is_refused(self, manifest: Path, monkeypatch, capsys) -> None:
        """``length`` is the array's field, never an element's: exit 1."""
        code, out, err = _run(
            [
                "query",
                "implementation_types",
                "--members",
                "elements",
                "--where",
                "length=4",
                "--project",
                str(manifest),
            ],
            monkeypatch,
            capsys,
        )

        assert code == 1, (out, err)
        assert "length" in out + err


class TestTheWalkFollowsEachNewEdge:
    def test_an_alias_of_a_record_depends_through_both_records_and_the_inner_alias(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        """``Frame_Ref`` → ``Frame_T`` → (``WheelSpeeds_Ref`` → ``WheelSpeeds_T`` → ``uint16``, ``uint8``)."""
        names = _walk(["query", "--deps", "Frame_Ref"], manifest, monkeypatch, capsys)

        assert names == {"Frame_T", "WheelSpeeds_Ref", "WheelSpeeds_T", "uint16", "uint16_base", "uint8", "uint8_base"}

    def test_a_pointer_to_a_pointer_depends_on_the_whole_chain(self, manifest: Path, monkeypatch, capsys) -> None:
        names = _walk(["query", "--deps", "WheelSpeeds_PtrPtr"], manifest, monkeypatch, capsys)

        assert names == {"WheelSpeeds_Ptr", "WheelSpeeds_T", "uint16", "uint16_base"}

    def test_an_array_depends_on_its_element_type(self, manifest: Path, monkeypatch, capsys) -> None:
        names = _walk(["query", "--deps", "WheelSpeedHistory_T", "--depth", "1"], manifest, monkeypatch, capsys)

        assert names == {"WheelSpeeds_T"}

    def test_an_unreferenced_array_has_its_element_type_and_no_dependents(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        assert _walk(["query", "--deps", "Spare_T"], manifest, monkeypatch, capsys) == {"uint8", "uint8_base"}
        assert _walk(["query", "--dependents", "Spare_T"], manifest, monkeypatch, capsys) == set()

    def test_the_first_ring_of_a_record_s_dependents_is_one_per_new_edge(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        """The alias, the array, the pointer and the map name the record; the interface types an element by it."""
        names = _walk(["query", "--dependents", "WheelSpeeds_T", "--depth", "1"], manifest, monkeypatch, capsys)

        assert names == {"WheelSpeeds_Ref", "WheelSpeedHistory_T", "WheelSpeeds_Ptr", "WheelSpeedMaps", "IExample"}

    def test_the_transitive_dependents_cross_every_new_edge(self, manifest: Path, monkeypatch, capsys) -> None:
        names = _walk(["query", "--dependents", "WheelSpeeds_T"], manifest, monkeypatch, capsys)

        # Through the pointer to the record holding it and to the pointer to the pointer; through the
        # alias to the record whose element names it and on to that record's alias; and on to the
        # interfaces and components those types reach.
        assert {"Handle_T", "WheelSpeeds_PtrPtr", "Frame_T", "Frame_Ref", "IDiagnosticService"} <= names
        assert {"ExampleSWC", "ExampleConsumerSWC"} <= names
        assert "Spare_T" not in names and "WheelSpeeds_T" not in names

    def test_a_scalar_s_first_ring_reaches_the_record_through_its_element(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        names = _walk(["query", "--dependents", "uint16", "--depth", "1"], manifest, monkeypatch, capsys)

        assert "WheelSpeeds_T" in names
        assert "WheelSpeedHistory_T" not in names  # two edges away: the bound holds


class TestImpact:
    def test_the_report_names_each_dependent_by_layer(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out, _err = _run(["impact", "WheelSpeeds_T", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        lines = {line.strip() for line in out.splitlines()}
        for expected in (
            "implementation_type WheelSpeeds_Ref",
            "implementation_type WheelSpeedHistory_T",
            "implementation_type WheelSpeeds_Ptr",
            "implementation_type WheelSpeeds_PtrPtr",
            "implementation_type Handle_T",
            "implementation_type Frame_T",
            "implementation_type Frame_Ref",
            "mapping_set WheelSpeedMaps",
            "interface IExample",
            "interface IDiagnosticService",
        ):
            assert expected in lines, out

    def test_depth_one_stops_at_the_first_ring(self, manifest: Path, monkeypatch, capsys) -> None:
        names = _walk(["impact", "WheelSpeeds_T", "--depth", "1"], manifest, monkeypatch, capsys)

        assert names == {"WheelSpeeds_Ref", "WheelSpeedHistory_T", "WheelSpeeds_Ptr", "WheelSpeedMaps", "IExample"}

    def test_impact_depth_zero_is_a_usage_error(self, manifest: Path, monkeypatch, capsys) -> None:
        code, _out, err = _run(
            ["impact", "WheelSpeeds_T", "--depth", "0", "--project", str(manifest)], monkeypatch, capsys
        )

        assert code == 2, err

    def test_query_depth_zero_is_the_command_s_refusal(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out, err = _run(
            ["query", "--dependents", "WheelSpeeds_T", "--depth", "0", "--project", str(manifest)], monkeypatch, capsys
        )

        assert code == 1, (out, err)

    def test_a_name_no_layer_declares_is_answered_exit_zero(self, manifest: Path, monkeypatch, capsys) -> None:
        code, _out, _err = _run(["impact", "WheelSpeeds_Impl", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0


class TestAPointerCycleIsWalkedToItsEnd:
    def test_check_model_refuses_the_cycles_the_walk_answers(self, cyclic: Path, monkeypatch, capsys) -> None:
        code, out, err = _run(["check-model", "--project", str(cyclic)], monkeypatch, capsys)

        assert code == 1
        assert "Cycle detected" in out + err

    def test_the_record_s_one_dependent_is_its_pointer_and_never_itself(
        self, cyclic: Path, monkeypatch, capsys
    ) -> None:
        assert _walk(["impact", "Node_T"], cyclic, monkeypatch, capsys) == {"Node_Ptr"}

    def test_the_pointer_depends_on_the_record_and_what_its_other_element_names(
        self, cyclic: Path, monkeypatch, capsys
    ) -> None:
        assert _walk(["query", "--deps", "Node_Ptr"], cyclic, monkeypatch, capsys) == {"Node_T", "uint8", "uint8_base"}

    @pytest.mark.parametrize("start", ["Ring_Rec", "Ring_T", "Ring_Ptr"])
    def test_a_three_node_ring_answers_the_other_two_in_both_directions(
        self, cyclic: Path, monkeypatch, capsys, start: str
    ) -> None:
        others = {"Ring_Rec", "Ring_T", "Ring_Ptr"} - {start}

        assert _walk(["query", "--deps", start], cyclic, monkeypatch, capsys) == others
        assert _walk(["query", "--dependents", start], cyclic, monkeypatch, capsys) == others

    def test_depth_one_on_the_ring_is_one_edge_each_way(self, cyclic: Path, monkeypatch, capsys) -> None:
        """``Ring_T`` names ``Ring_Ptr`` (its element type) and is named by ``Ring_Rec`` (its element)."""
        assert _walk(["query", "--deps", "Ring_T", "--depth", "1"], cyclic, monkeypatch, capsys) == {"Ring_Ptr"}
        assert _walk(["impact", "Ring_T", "--depth", "1"], cyclic, monkeypatch, capsys) == {"Ring_Rec"}

    def test_the_text_report_over_the_cycle_is_answered(self, cyclic: Path, monkeypatch, capsys) -> None:
        code, out, _err = _run(["impact", "Ring_Ptr", "--project", str(cyclic)], monkeypatch, capsys)

        assert code == 0
        lines = {line.strip() for line in out.splitlines()}
        assert {"implementation_type Ring_Rec", "implementation_type Ring_T"} <= lines
        assert "implementation_type Ring_Ptr" not in lines


class TestTheTerminalNamesTheFullPath:
    """``cli.messages.error_text`` renders ``ElementParseError`` with ``full_path=True``: the user's own terminal."""

    @pytest.mark.parametrize(
        "argv", [["impact", "WheelSpeeds_T"], ["query", "implementation_types"], ["check-model"], ["list"]]
    )
    def test_a_new_kind_the_schema_refuses_is_named_by_its_full_path(
        self, manifest: Path, monkeypatch, capsys, argv: list[str]
    ) -> None:
        broken = model_element_path(manifest.parent / "model", "Spare_T", kind="implementation_array")
        composites.data_type(
            manifest.parent / "model",
            {
                "name": "Spare_T",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 0,
                "element_name": "B",
            },
        )

        code, _out, err = _run([*argv, "--project", str(manifest)], monkeypatch, capsys)

        assert code != 0
        assert f"Error: Invalid implementation type YAML in {broken.resolve()}: " in err, err
