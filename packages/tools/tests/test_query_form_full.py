"""``swcraft query --form full``: the width parameter of a layer slice.

The kernel half (``query.slicing`` / ``query.formatting``) and the CLI half (``cli.query_command``)
are tested side by side here.

The one definition of a ``full`` value is the protocol's: :class:`TestTheWidthIsTheProtocols` holds the
CLI's JSON equal to ``read`` at ``form: "full"`` as JSON values and as serialized text, over the agent
bench, read in place, on the layers and member slices :data:`WIDTH_CASES` names, a filter and a page
included. What it does not reach: a layer or collection outside that list, the bench's own values
(another model may carry a shape the bench does not), ``summary`` and ``count`` (refused here), an
address below a layer, a placement field the placement table holds no value for (``null`` here, a
refusal there), and a misplaced element's placement value, which this surface prints raw and the
protocol refuses to publish (:meth:`TestTheKernelAtFull.test_a_placement_value_is_printed_as_the_reader_holds_it`).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cli_runner import run_swcraft
from conftest import AGENT_TASK_BENCH
from conftest import AGENT_TASK_BENCH_MANIFEST as BENCH_MANIFEST
from swcraft_tools.editing import engine
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.query import formatting as query_formatting
from swcraft_tools.query import request as query_request
from swcraft_tools.query import slicing as query_slicing

#: One width case: ``(layer, members, where)``, *where* a list of ``(field, value)`` equality clauses.
WidthCase = tuple[str, str | None, list[tuple[str, str]]]


@pytest.fixture(scope="module")
def bench_state():
    return load_model_state(AGENT_TASK_BENCH / "model")


def _value_fields(lines: list[str]) -> list[str]:
    """The field names of the indented value lines, in the order printed."""
    return [match.group(1) for line in lines if (match := re.match(r"^  ([a-z_]+): ", line))]


def _blocks(text: str) -> list[list[str]]:
    """The text answer as one block per row: its identity line, then its indented lines."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("  "):
            blocks[-1].append(line)
        elif line and not line.startswith("("):
            blocks.append([line])
    return blocks


class TestTheKernelAtFull:
    """``slice_layer(form=...)`` and the two renderers."""

    @pytest.mark.parametrize("members", [None, "ports", "runnables.calls", "memory", "satisfies"])
    def test_the_default_is_the_index_answer_unchanged(self, bench_state, members: str | None) -> None:
        default = query_slicing.slice_layer(bench_state, "swc", members=members)
        index = query_slicing.slice_layer(bench_state, "swc", members=members, form="index")
        assert default == index
        assert default.values is None
        assert query_formatting.format_query(default) == query_formatting.format_query(index)
        document = json.loads(query_formatting.format_query_json(default))
        assert document["schemaVersion"] == query_request.QUERY_SCHEMA_VERSION
        assert "form" not in document
        assert document["elements"] == list(default.identities)

    def test_full_keeps_the_identities_and_the_page(self, bench_state) -> None:
        index = query_slicing.slice_layer(bench_state, "swc", members="ports", limit=3)
        full = query_slicing.slice_layer(bench_state, "swc", members="ports", limit=3, form="full")
        assert full.identities == index.identities
        assert full.page == index.page
        assert full.values is not None and len(full.values) == len(full.identities)

    def test_a_layer_element_at_full_follows_the_declared_order_and_carries_its_placement(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", form="full")
        registry = current_registry()
        assert result.values is not None
        by_name = {value["name"]: value for value in result.values}
        assert sorted(by_name) == sorted(element.name for element in bench_state.layers["swc"])
        for element in bench_state.layers["swc"]:
            kind = registry.kind_of_model(type(element))
            assert kind is not None
            assert list(by_name[element.name]) == [facts.name for facts in kind.fields]
        groups = {value["name"]: value["group"] for value in result.values}
        assert groups == {name: values["group"] for name, values in bench_state.placement_of("swc").items()}

    def test_the_text_of_a_layer_element_prints_every_field_but_the_identity(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", form="full", limit=1)
        blocks = _blocks(query_formatting.format_query(result))
        assert len(blocks) == 1
        assert result.values is not None
        expected = [field for field in result.values[0] if field != "name"]
        assert _value_fields(blocks[0]) == expected
        assert "group" in expected

    def test_a_layer_keyed_by_id_does_not_repeat_its_identity(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "requirements", form="full", limit=1)
        assert result.identity_field == "id"
        assert result.values is not None
        (block,) = _blocks(query_formatting.format_query(result))
        assert _value_fields(block) == [field for field in result.values[0] if field != "id"]
        assert "id" not in _value_fields(block)

    def test_a_call_prints_its_fields_in_the_declared_order_without_the_identity(self, bench_state) -> None:
        """The identity line, then every non-identity field of the call, ``null`` included."""
        result = query_slicing.slice_layer(bench_state, "swc", members="runnables.calls", form="full")
        declared = [facts.name for facts in current_registry().kind("operation_call").fields]
        identity_fields = {"port", "operation"}
        blocks = _blocks(query_formatting.format_query(result))
        assert blocks, "the bench declares calls"
        for block in blocks:
            assert block[0].startswith("swc: ")
            assert _value_fields(block) == [field for field in declared if field not in identity_fields]
        assert any(line.endswith(": null") for block in blocks for line in block)

    def test_a_port_does_not_repeat_its_name(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", members="ports", form="full", limit=1)
        assert result.member_identity_field == "name"
        (block,) = _blocks(query_formatting.format_query(result))
        fields = _value_fields(block)
        assert "name" not in fields
        assert "direction" in fields and "interface" in fields

    def test_a_map_entry_prints_its_value(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", members="memory", form="full", limit=1)
        assert result.values is not None
        (block,) = _blocks(query_formatting.format_query(result))
        assert block[1:] == [f"  value: {json.dumps(result.values[0]['member'])}"]

    def test_a_whole_scalar_member_is_its_identity_and_prints_nothing_more(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", members="satisfies", form="full")
        blocks = _blocks(query_formatting.format_query(result))
        assert blocks and all(len(block) == 1 for block in blocks)

    def test_the_full_document_has_its_own_version_and_whole_rows(self, bench_state) -> None:
        result = query_slicing.slice_layer(bench_state, "swc", members="runnables.calls", form="full", limit=1)
        document = json.loads(query_formatting.format_query_json(result))
        assert document["schemaVersion"] == query_request.QUERY_FULL_SCHEMA_VERSION
        assert document["schemaVersion"] not in (
            query_request.QUERY_SCHEMA_VERSION,
            query_request.QUERY_ROWS_SCHEMA_VERSION,
        )
        assert document["form"] == "full"
        (row,) = document["elements"]
        identity = result.identities[0]
        assert {key: item for key, item in row.items() if key != "member"} == {
            key: item for key, item in identity.items() if key != "member"
        }
        assert {"port", "operation", "asynchronous", "name"} <= set(row["member"])
        # The cursor stays the identity row, never the whole value.
        assert document["page"]["next"] == identity

    def test_paging_at_full_continues_past_the_identity(self, bench_state) -> None:
        first = query_slicing.slice_layer(bench_state, "swc", members="ports", form="full", limit=2)
        assert first.page is not None
        second = query_slicing.slice_layer(
            bench_state, "swc", members="ports", form="full", limit=2, after=first.page.next
        )
        whole = query_slicing.slice_layer(bench_state, "swc", members="ports", form="full")
        assert first.values is not None and second.values is not None and whole.values is not None
        assert [*first.values, *second.values] == list(whole.values[:4])

    def test_a_form_outside_the_two_is_refused(self, bench_state) -> None:
        with pytest.raises(ValueError, match="summary"):
            query_slicing.slice_layer(bench_state, "swc", form="summary")

    def test_a_placement_field_with_no_table_value_prints_null(self, bench_state) -> None:
        """Every declared field prints: a placement field the table holds nothing for is ``null``."""
        unplaced = dataclasses.replace(bench_state, placement={})
        result = query_slicing.slice_layer(unplaced, "swc", form="full", limit=1)
        assert result.values is not None
        (value,) = result.values
        assert "group" in value
        assert value["group"] is None

    def test_a_placement_value_is_printed_as_the_reader_holds_it(self, tmp_path: Path) -> None:
        """The divergence the width case does not reach: a hand-nested file's ``group`` is printed raw.

        The protocol refuses to publish such a layer (``PlacementNotPublishable``); the CLI prints the
        user's own project to their own terminal, as its ``--where`` filter already reads the value.
        """
        deep = tmp_path / "model" / "swc" / "a" / "b" / "DeepSwc.yaml"
        deep.parent.mkdir(parents=True)
        deep.write_text(
            yaml.safe_dump({"name": "DeepSwc", "type": "application", "handle_termination_and_restart": "no_support"}),
            encoding="utf-8",
            newline="\n",
        )
        state = load_model_state(tmp_path / "model")
        result = query_slicing.slice_layer(state, "swc", form="full", where=[query_request.Clause("name", "DeepSwc")])
        assert result.values is not None
        assert result.values[0]["group"] == "a/b"


class TestTheRequestGrammar:
    def test_full_is_accepted_at_a_layer_address(self) -> None:
        parsed = query_request.parse_request({"address": {"layer": "swc"}, "form": "full", "members": "ports"})
        assert parsed.form == "full"
        assert parsed.as_json()["form"] == "full"

    def test_summary_is_refused_and_the_refusal_names_both_forms(self) -> None:
        with pytest.raises(query_request.QueryError) as refused:
            query_request.parse_request({"address": {"layer": "swc"}, "form": "summary"})
        assert "`index`" in str(refused.value) and "`full`" in str(refused.value)

    @pytest.mark.parametrize("form", ["count", "FULL"])
    def test_a_form_no_read_has_is_refused(self, form: str) -> None:
        with pytest.raises(query_request.QueryError, match="count|FULL"):
            query_request.parse_request({"address": {"layer": "swc"}, "form": form})

    @pytest.mark.parametrize(
        "key",
        [
            {"derived": ["base_type"]},
            {"join": [{"with": {"address": {"layer": "connectors"}}, "on": [{"left": "a", "right": "b"}], "as": "x"}]},
        ],
        ids=["derived", "join"],
    )
    def test_full_beside_derived_or_join_is_refused(self, key: dict[str, Any]) -> None:
        with pytest.raises(query_request.QueryError, match='states `form: "index"`'):
            query_request.parse_request({"address": {"layer": "swc"}, "form": "full", **key})


def _run(argv: list[str], monkeypatch, capsys) -> tuple[int, str, str]:
    return run_swcraft(["query", *argv], monkeypatch, capsys)


#: The sets the width case covers, each also paged: on ``swc`` a layer with a placement field, a member
#: slice, a member path, a map collection and a whole-value scalar collection; layers of several
#: top-level kinds (``interfaces``, ``hardware``, ``implementation_types``), one keyed by ``id``
#: (``requirements``), ones with no placement table and nested models (``connectors``, ``tasks``,
#: ``compositions``) and their member slices; and a filter on the placement field and on a member field.
#: Each entry is a :data:`WidthCase`.
WIDTH_CASES: list[WidthCase] = [
    ("swc", None, []),
    ("swc", "ports", []),
    ("swc", "runnables.calls", []),
    ("swc", "memory", []),
    ("swc", "satisfies", []),
    ("interfaces", None, []),
    ("interfaces", "data_elements", []),
    ("hardware", None, []),
    ("implementation_types", None, []),
    ("requirements", None, []),
    ("connectors", None, []),
    ("tasks", None, []),
    ("tasks", "runnables", []),
    ("compositions", None, []),
    ("compositions", "members", []),
    ("swc", None, [("group", "application")]),
    ("swc", "ports", [("direction", "provide")]),
]


def _case_id(case: WidthCase) -> str:
    layer, members, where = case
    return "-".join([layer, *([members] if members else []), *(f"{field}={value}" for field, value in where)])


def _where_flags(where: list[tuple[str, str]]) -> list[str]:
    return [flag for field, value in where for flag in ("--where", f"{field}={value}")]


class TestTheWidthIsTheProtocols:
    """``swcraft query --form full --format json`` equals ``read`` at ``form: "full"`` — the one-backend width case."""

    @staticmethod
    def _protocol(
        layer: str, members: str | None, where: list[tuple[str, str]], limit: int | None = None, after: Any = None
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": "read", "address": {"layer": layer}, "form": "full"}
        if members is not None:
            request["members"] = members
        if where:
            request["where"] = [{"field": field, "equals": value} for field, value in where]
        if limit is not None:
            request["limit"] = limit
        if after is not None:
            request["after"] = after
        response = engine.handle_request(request, project=str(BENCH_MANIFEST))
        assert response["response"] == "read", response
        return response

    @staticmethod
    def _assert_equal(document: dict[str, Any], response: dict[str, Any]) -> None:
        assert document["schemaVersion"] == query_request.QUERY_FULL_SCHEMA_VERSION
        assert document["form"] == "full"
        assert document["elements"] == response["value"]
        # Byte for byte: the key order is the protocol's.
        assert json.dumps(document["elements"]) == json.dumps(response["value"])
        assert document.get("page") == response.get("page")
        assert document["elements"], "the case answered nothing, so the equality is vacuous"

    @pytest.mark.parametrize("case", WIDTH_CASES, ids=_case_id)
    @pytest.mark.parametrize("paged", [False, True], ids=["whole", "paged"])
    def test_through_the_flags(self, case, paged: bool, monkeypatch, capsys) -> None:
        layer, members, where = case
        flags = (["--members", members] if members else []) + _where_flags(where)
        limit = 1 if paged else None
        if limit is not None:
            flags += ["--limit", str(limit)]
        code, out, err = _run(
            [layer, *flags, "--form", "full", "--format", "json", "--project", str(BENCH_MANIFEST)], monkeypatch, capsys
        )
        assert code == 0, err
        document = json.loads(out)
        self._assert_equal(document, self._protocol(layer, members, where, limit))
        if paged:
            cursor = document["page"]["next"]
            code, out, err = _run(
                [
                    layer,
                    *flags,
                    "--after",
                    cursor if isinstance(cursor, str) else json.dumps(cursor),
                    "--form",
                    "full",
                    "--format",
                    "json",
                    "--project",
                    str(BENCH_MANIFEST),
                ],
                monkeypatch,
                capsys,
            )
            assert code == 0, err
            self._assert_equal(json.loads(out), self._protocol(layer, members, where, limit, cursor))

    @pytest.mark.parametrize("case", WIDTH_CASES, ids=_case_id)
    def test_through_a_request_file(self, case, tmp_path: Path, monkeypatch, capsys) -> None:
        """A request file carrying ``form: "full"`` answers ``full``, in both formats the flags' bytes."""
        layer, members, where = case
        body: dict[str, Any] = {"address": {"layer": layer}, "form": "full"}
        flags: list[str] = _where_flags(where)
        if members is not None:
            body["members"] = members
            flags = ["--members", members, *flags]
        if where:
            body["where"] = [{"field": field, "equals": value} for field, value in where]
        request = tmp_path / "request.json"
        request.write_text(json.dumps(body), encoding="utf-8", newline="\n")
        documents: dict[str, str] = {}
        for output in ("json", "text"):
            by_request = _run(
                ["--request", str(request), "--format", output, "--project", str(BENCH_MANIFEST)], monkeypatch, capsys
            )
            by_flags = _run(
                [layer, *flags, "--form", "full", "--format", output, "--project", str(BENCH_MANIFEST)],
                monkeypatch,
                capsys,
            )
            assert by_request[0] == 0, by_request[2]
            assert by_request == by_flags, output
            documents[output] = by_flags[1]
        self._assert_equal(json.loads(documents["json"]), self._protocol(layer, members, where))


class TestTheFlagIsASliceArgument:
    """``--form`` is absent unless given: presence beside ``--request``, truthiness beside a walk."""

    def test_a_walk_without_form_still_answers(self, monkeypatch, capsys) -> None:
        code, out, err = _run(["--dependents", "IBrakeRequest", "--project", str(BENCH_MANIFEST)], monkeypatch, capsys)
        assert code == 0, err
        assert out

    @pytest.mark.parametrize("form", ["index", "full"])
    def test_form_beside_a_walk_is_refused(self, form: str, monkeypatch, capsys) -> None:
        code, _out, err = _run(
            ["--form", form, "--dependents", "IBrakeRequest", "--project", str(BENCH_MANIFEST)], monkeypatch, capsys
        )
        assert code == 1
        assert err.startswith("Error: --dependents")
        assert "--form" in err

    def test_form_index_beside_a_request_file_is_refused(self, tmp_path: Path, monkeypatch, capsys) -> None:
        request = tmp_path / "request.json"
        request.write_text(json.dumps({"address": {"layer": "swc"}}), encoding="utf-8", newline="\n")
        code, _out, err = _run(
            ["--form", "index", "--request", str(request), "--project", str(BENCH_MANIFEST)], monkeypatch, capsys
        )
        assert code == 1
        assert "--request FILE" in err and "--form" in err

    @pytest.mark.parametrize(
        "extra",
        [
            {"derived": ["base_type"]},
            {"join": [{"with": {"address": {"layer": "connectors"}}, "on": [{"left": "a", "right": "b"}], "as": "x"}]},
        ],
        ids=["derived", "join"],
    )
    def test_a_full_request_beside_derived_or_join_is_refused(
        self, extra: dict[str, Any], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        request = tmp_path / "request.json"
        request.write_text(
            json.dumps({"address": {"layer": "connectors"}, "form": "full", **extra}), encoding="utf-8", newline="\n"
        )
        code, _out, err = _run(["--request", str(request), "--project", str(BENCH_MANIFEST)], monkeypatch, capsys)
        assert code == 1
        assert '`form: "index"`' in err

    def test_a_form_outside_the_choices_is_a_usage_error(self, monkeypatch, capsys) -> None:
        """A ``--form`` value outside ``index`` and ``full`` is argparse's usage error, exit 2."""
        code, _out, err = _run(["swc", "--form", "json", "--project", str(BENCH_MANIFEST)], monkeypatch, capsys)
        assert code == 2
        assert "--form" in err
