"""Tests for `swcraft query` at the CLI boundary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from cli_projects import dump_yaml, write_query_project
from conftest import (
    REPO_ROOT,
    _write_manifest,
    parser_option_strings,
    parser_positional_dests,
    subcommand_parser,
    write_model_element,
)
from swcraft_tools import query
from swcraft_tools.cli import main, query_command
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.editing import engine
from swcraft_tools.model_registry import production_registry


def _write_runnable_query_project(project_dir: Path) -> Path:
    """:func:`write_query_project`, with runnable accesses and an interface stub.

    The provider has **two** runnables writing one ``(port, element)`` pair, so a dotted
    ``--members`` path answers rows only their ``through`` chain tells apart. The stub lists
    a member **name** under ``data_elements``, which both surfaces skip.
    """
    manifest = write_query_project(project_dir)
    model_root = project_dir / "model"
    speed = model_root / "swc" / "body" / "SpeedSwc.yaml"
    document = yaml.safe_load(speed.read_text(encoding="utf-8"))
    document["runnables"] = [
        {
            "name": "SpeedRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "writes": [{"port": "pSpeed", "element": "speed"}],
        },
        {
            "name": "SpeedSlowRun",
            "min_start_interval": 0.1,
            "events": [{"type": "timing", "period": 0.1}],
            "writes": [{"port": "pSpeed", "element": "speed"}],
        },
    ]
    dump_yaml(speed, document)
    ctrl = model_root / "swc" / "control" / "CtrlSwc.yaml"
    document = yaml.safe_load(ctrl.read_text(encoding="utf-8"))
    document["runnables"] = [
        {
            "name": "CtrlRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "reads": [{"port": "rSpeed", "element": "speed"}],
        }
    ]
    dump_yaml(ctrl, document)
    write_model_element(
        model_root,
        {
            "name": "IOutside",
            "kind": "external_interface",
            "external_ref": "/Outside/IOutside",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            "data_elements": ["outsideSpeed"],
        },
    )
    return manifest


def _write_derived_query_project(project_dir: Path, *, duplicate_delegation: bool = False) -> Path:
    """:func:`_write_runnable_query_project`, with a connector into a composition that fans out.

    The boundary port ``gIn`` of ``CtrlGroup`` is delegated to two consumers, so
    ``connection_ends`` gives the connector's row twice. With *duplicate_delegation* the port
    is delegated to ``CtrlSwc.rSpeed`` twice -- an invalid model whose identical rows collapse.
    """
    manifest = _write_runnable_query_project(project_dir)
    model_root = project_dir / "model"
    ctrl = yaml.safe_load((model_root / "swc" / "control" / "CtrlSwc.yaml").read_text(encoding="utf-8"))
    dump_yaml(model_root / "swc" / "control" / "LogSwc.yaml", {**ctrl, "name": "LogSwc"})
    delegations = [
        {"name": "gIn_to_ctrl", "port": "gIn", "member": "ctrl", "member_port": "rSpeed"},
        {"name": "gIn_to_log", "port": "gIn", "member": "log", "member_port": "rSpeed"},
    ]
    if duplicate_delegation:
        delegations.append({"name": "gIn_to_ctrl_again", "port": "gIn", "member": "ctrl", "member_port": "rSpeed"})
    dump_yaml(
        model_root / "compositions" / "CtrlGroup.yaml",
        {
            "name": "CtrlGroup",
            "title": "Control group",
            "ports": [{"name": "gIn", "direction": "require", "interface": "ISpeed"}],
            "members": [{"name": "ctrl", "type": "CtrlSwc"}, {"name": "log", "type": "LogSwc"}],
            "delegations": delegations,
        },
    )
    dump_yaml(
        model_root / "compositions" / "Root.yaml",
        {
            "name": "Root",
            "title": "Root composition",
            "root": True,
            "members": [{"name": "speed", "type": "SpeedSwc"}, {"name": "group", "composition": "CtrlGroup"}],
        },
    )
    dump_yaml(
        model_root / "connectors" / "Speed_to_Group.yaml",
        {
            "name": "Speed_to_Group",
            "from": {"swc": "SpeedSwc", "port": "pSpeed"},
            "to": {"composition": "CtrlGroup", "port": "gIn"},
        },
    )
    return manifest


class TestCliQuery:
    """swcraft query — filtered, member-sliced and paged slices, and the two walks.

    Read-only: a successful run exits 0, an empty slice included; a project it could not
    *read*, or a **request** the declarations cannot answer, exits non-zero. The projections
    are tested in `test_query.py`; these tests own the CLI boundary, plus that the CLI and
    the editing protocol are one answer and that `query --dependents` is not `impact`'s bytes.
    """

    def test_cli_query_requires_project(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "swc"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_query_unreadable_project_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "swc", "--project", str(tmp_path / "nope" / "swcraft.yaml")]
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_cli_query_one_unparseable_element_exits_nonzero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """One broken element is a project the command could not read: exit non-zero."""
        manifest = write_query_project(tmp_path)
        (tmp_path / "model" / "swc" / "body" / "SpeedSwc.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "swc", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    def test_cli_query_prints_the_layer_in_the_read_order(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`list` answers the readers' order, `query` the read order (kind rank, then reader order).

        Both commands are run: the claim is about two outputs, not two derivations.
        """
        manifest = write_query_project(tmp_path)

        monkeypatch.setattr("sys.argv", ["swcraft", "query", "implementation_types", "--project", str(manifest)])
        main()
        queried = capsys.readouterr().out.splitlines()

        monkeypatch.setattr("sys.argv", ["swcraft", "list", "implementation_types", "--project", str(manifest)])
        main()
        listed = capsys.readouterr().out.splitlines()

        assert queried == ["implementation_types: Speed", "implementation_types: Alias"]
        assert listed == ["implementation_types: Alias", "implementation_types: Speed"]
        assert sorted(queried) == sorted(listed)

    def test_cli_query_where_filters_and_states_the_scope(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", "base_types", "--where", "encoding=IEEE754", "--project", str(manifest)],
        )

        main()  # must not raise SystemExit

        assert capsys.readouterr().out.splitlines() == ["base_types: float32", "", "(where: encoding=IEEE754)"]

    def test_cli_query_where_is_repeatable_and_and_composed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "base_types",
                "--where",
                "kind=base_type",
                "--where",
                "size=8",
                "--project",
                str(manifest),
            ],
        )

        main()

        out = capsys.readouterr().out
        assert "base_types: uint8" in out
        assert "float32" not in out

    def test_cli_query_where_reads_a_json_value_and_a_bare_string(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`size=8` reaches an `int` field and `encoding=NONE` a `str` one.

        A value is offered to `json.loads` first and is the bare string when that refuses --
        the rule `--after` uses too, because a composite cursor is an object.
        """
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "base_types", "--where", "size=8", "--project", str(manifest)]
        )
        main()
        assert capsys.readouterr().out.splitlines()[0] == "base_types: uint8"

        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "base_types", "--where", "encoding=NONE", "--project", str(manifest)]
        )
        main()
        assert capsys.readouterr().out.splitlines()[0] == "base_types: uint8"

    def test_cli_query_exists_keeps_the_elements_that_carry_the_field(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "implementation_types", "--exists", "unit", "--project", str(manifest)]
        )

        main()

        assert capsys.readouterr().out.splitlines() == ["implementation_types: Speed", "", "(where: unit exists)"]

    def test_cli_query_members_answers_every_member_with_its_owner(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """ "Every port of the layer, with its owner" — the question no address asks."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "swc", "--members", "ports", "--project", str(manifest)])

        main()

        assert capsys.readouterr().out.splitlines() == [
            "swc: SpeedSwc.pSpeed",
            "swc: CtrlSwc.rSpeed",
            "",
            "(members: ports)",
        ]

    def test_cli_query_members_slices_the_compositions_layer(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``--members members`` slices the compositions layer, which needs no argument of its own."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "compositions", "--members", "members", "--project", str(manifest)]
        )

        main()  # must not raise SystemExit

        assert capsys.readouterr().out.splitlines() == [
            "compositions: Root.speed",
            "compositions: Root.ctrl",
            "",
            "(members: members)",
        ]

    def test_cli_query_members_and_where_compose(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "swc",
                "--members",
                "ports",
                "--where",
                "direction=require",
                "--project",
                str(manifest),
            ],
        )

        main()

        assert capsys.readouterr().out.splitlines()[0] == "swc: CtrlSwc.rSpeed"

    def test_cli_query_pages_and_the_cursor_continues_the_walk(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "implementation_types", "--limit", "1", "--project", str(manifest)]
        )
        main()
        first = capsys.readouterr().out.splitlines()
        assert first == [
            "implementation_types: Speed",
            "",
            "(page: 1 of 2, truncated; next: after=Speed)",
        ]

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "implementation_types",
                "--limit",
                "1",
                "--after",
                "Speed",
                "--project",
                str(manifest),
            ],
        )
        main()
        assert capsys.readouterr().out.splitlines()[0] == "implementation_types: Alias"

    def test_cli_query_where_answers_an_integer_too_wide_for_a_float(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A `--where` over an integer too wide for a float matches it, never an `OverflowError`.

        Guards a defect: the comparison key reduced every number through `float(value)`. The
        clause must MATCH the member holding the value, which separates the arm from a bare
        `except: pass`; 401 digits is over `float`'s ~309 and under CPython's 4 300-digit
        `repr` limit.
        """
        manifest = write_query_project(tmp_path)
        wide = 10**400
        dump_yaml(
            tmp_path / "model" / "interfaces" / "IWide.yaml",
            {
                "name": "IWide",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "wide", "implementation_type": "uint8", "init": wide},
                    {"name": "narrow", "implementation_type": "uint8", "init": 1},
                ],
            },
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "interfaces",
                "--members",
                "data_elements",
                "--where",
                f"init={wide}",
                "--project",
                str(manifest),
            ],
        )

        main()

        assert capsys.readouterr().out.splitlines()[0] == "interfaces: IWide.wide"

    def test_cli_query_json_carries_the_schema_version_and_the_echo(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "base_types",
                "--where",
                "kind=base_type",
                "--limit",
                "1",
                "--format",
                "json",
                "--project",
                str(manifest),
            ],
        )

        main()

        document = json.loads(capsys.readouterr().out)
        assert document["schemaVersion"] == 3
        assert document["layer"] == "base_types"
        assert document["identityField"] == "name"
        assert document["where"] == [{"field": "kind", "equals": "base_type"}]
        assert document["elements"] == ["float32"]
        assert document["page"] == {"total": 2, "returned": 1, "truncated": True, "next": "float32"}

    def test_cli_query_json_refuses_a_non_finite_where_value_instead_of_emitting_it(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`--where value_min=NaN` never reaches the JSON document as a bare `NaN`.

        `json.loads` accepts `NaN`, and `json.dumps` would echo a document that is not RFC
        8259. `_json_document` renders with `allow_nan=False` inside `cmd_query`'s `try`, so the
        refusal is the ordinary `Error:` path. The text renderer is not covered by the flag:
        `--format text` prints its (empty) slice and exits 0.
        """
        manifest = write_query_project(tmp_path)
        for value in ("NaN", "Infinity", "-Infinity"):
            monkeypatch.setattr(
                "sys.argv",
                [
                    "swcraft",
                    "query",
                    "implementation_types",
                    "--where",
                    f"value_min={value}",
                    "--format",
                    "json",
                    "--project",
                    str(manifest),
                ],
            )
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 1
            captured = capsys.readouterr()
            assert captured.out == "", "a document that cannot be rendered must not be half-written"
            assert captured.err.startswith("Error: "), "the refusal must take the clean path, not a traceback"

        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", "implementation_types", "--where", "value_min=NaN", "--project", str(manifest)],
        )
        main()
        assert capsys.readouterr().out == "(where: value_min=NaN)\n"

    @staticmethod
    def _protocol_index_request(layer: str, flags: list[str]) -> dict[str, Any]:
        """The same request as an editing-protocol ``read`` at ``form: "index"``.

        Composed from the argv, so the two halves cannot drift into two different requests.
        """
        request: dict[str, Any] = {"operation": "read", "address": {"layer": layer}, "form": "index"}
        where: list[dict[str, Any]] = []
        rest = list(flags)
        while rest:
            flag, value, rest = rest[0], rest[1], rest[2:]
            if flag == "--where":
                name, _, raw = value.partition("=")
                try:
                    where.append({"field": name, "equals": json.loads(raw)})
                except json.JSONDecodeError:
                    where.append({"field": name, "equals": raw})
            elif flag == "--exists":
                where.append({"field": value, "exists": True})
            elif flag == "--members":
                request["members"] = value
            elif flag == "--limit":
                request["limit"] = int(value)
            elif flag == "--after":
                # A member-slice cursor is an object; a raw string would compare the two
                # backends on a request only one of them could take.
                request["after"] = query_command._query_value(value)
        if where:
            request["where"] = where
        return request

    def _assert_one_backend(
        self, layer: str, flags: list[str], tmp_path: Path, monkeypatch, capsys, project: Any = None
    ) -> None:
        """The CLI and ``engine.handle_request`` at `form: "index"` give one answer.

        The `cli` package may not import `editing/`, so the filter, the page and the member
        slice are stated twice; nothing but these cases holds them to one answer. The
        **whole** page block is compared, not ``total`` alone: ``next`` is what a client
        round-trips.

        Does **not** reach the width: `--form full` is held to `read` at `full` by
        `test_query_form_full.py::TestTheWidthIsTheProtocols`.
        """
        manifest = (project or write_query_project)(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", layer, *flags, "--format", "json", "--project", str(manifest)]
        )
        main()
        document = json.loads(capsys.readouterr().out)

        response = engine.handle_request(self._protocol_index_request(layer, flags), project=str(manifest))
        assert response["response"] == "read", response
        assert document["elements"] == response["value"]["identities"]
        assert document.get("page") == response.get("page")
        assert document["elements"], "the case answered nothing, so the equality is vacuous"

    @pytest.mark.parametrize(
        ("layer", "flags"),
        [
            ("implementation_types", []),
            ("base_types", []),
            ("base_types", ["--where", "kind=base_type"]),
            ("base_types", ["--where", "kind=base_type", "--where", "size=8"]),
            ("base_types", ["--exists", "encoding"]),
            ("implementation_types", ["--limit", "1"]),
            ("implementation_types", ["--limit", "1", "--after", "Speed"]),
        ],
    )
    def test_cli_query_json_is_the_protocols_index_answer_at_a_layer(
        self, layer: str, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        self._assert_one_backend(layer, flags, tmp_path, monkeypatch, capsys)

    @pytest.mark.parametrize(
        "flags",
        [
            ["--members", "ports"],
            ["--members", "ports", "--where", "direction=require"],
            ["--members", "memory"],
            ["--members", "ports", "--limit", "1"],
            # The one case where the backends compute an identity through different code
            # (`differ.CollectionRule.member_identity` vs `address.member_identity_of`).
            ["--members", "ports", "--limit", "1", "--after", '{"element": "SpeedSwc", "member": "pSpeed"}'],
        ],
    )
    def test_cli_query_json_is_the_protocols_index_answer_at_a_member_slice(
        self, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The member-slice case, split from the layer case so no parametrization enumerates two layers."""
        self._assert_one_backend("swc", flags, tmp_path, monkeypatch, capsys)

    @pytest.mark.parametrize(
        "flags",
        [
            ["--members", "runnables.writes"],
            ["--members", "runnables.reads"],
            ["--members", "runnables.writes", "--where", "port=pSpeed"],
            ["--members", "runnables.writes", "--limit", "1"],
            # The cursor carries the owner chain: without `through` it would
            # name two rows, the two runnables' writes of one pair.
            [
                "--members",
                "runnables.writes",
                "--limit",
                "1",
                "--after",
                '{"element": "SpeedSwc", "through": ["SpeedRun"], "member": {"port": "pSpeed", "element": "speed"}}',
            ],
            ["--members", "runnables.events"],
        ],
    )
    def test_cli_query_json_is_the_protocols_index_answer_at_a_member_path(
        self, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The dotted ``--members`` path, held to the protocol's rows."""
        self._assert_one_backend("swc", flags, tmp_path, monkeypatch, capsys, _write_runnable_query_project)

    def test_cli_query_skips_an_interface_stubs_member_names_as_the_protocol_does(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An interface stub's member names are rows on neither surface, never a ``null`` member."""
        self._assert_one_backend(
            "interfaces", ["--members", "data_elements"], tmp_path, monkeypatch, capsys, _write_runnable_query_project
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "query",
                "interfaces",
                "--members",
                "data_elements",
                "--format",
                "json",
                "--project",
                str(tmp_path / "swcraft.yaml"),
            ],
        )
        main()
        document = json.loads(capsys.readouterr().out)
        assert document["elements"] == [{"element": "ISpeed", "member": "speed"}]

    # `--request FILE`

    @staticmethod
    def _run_query(argv: list[str], monkeypatch, capsys) -> tuple[str, str]:
        monkeypatch.setattr("sys.argv", ["swcraft", "query", *argv])
        main()
        captured = capsys.readouterr()
        return captured.out, captured.err

    @staticmethod
    def _request_file(tmp_path: Path, request: Any, name: str = "request.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(request), encoding="utf-8", newline="\n")
        return path

    def _assert_request_is_the_flags(
        self, layer: str, flags: list[str], tmp_path: Path, monkeypatch, capsys, project: Any = None
    ) -> None:
        """``--request`` spelling what the flags spell prints the flags' bytes, in both formats.

        Run with ``form: "index"`` stated and omitted: the parser accepts both.
        """
        manifest = (project or write_query_project)(tmp_path)
        protocol_request = self._protocol_index_request(layer, flags)
        for stated_form in (True, False):
            body = {key: value for key, value in protocol_request.items() if key != "operation"}
            if not stated_form:
                del body["form"]
            request = self._request_file(tmp_path, body)
            for output in ("json", "text"):
                by_flags, _ = self._run_query(
                    [layer, *flags, "--format", output, "--project", str(manifest)], monkeypatch, capsys
                )
                by_request, _ = self._run_query(
                    ["--request", str(request), "--format", output, "--project", str(manifest)], monkeypatch, capsys
                )
                assert by_request == by_flags, (body, output)
        document = json.loads(
            self._run_query(
                ["--request", str(request), "--format", "json", "--project", str(manifest)], monkeypatch, capsys
            )[0]
        )
        assert document["schemaVersion"] == 3  # QUERY_SCHEMA_VERSION
        response = engine.handle_request(protocol_request, project=str(manifest))
        assert response["response"] == "read", response
        assert document["elements"] == response["value"]["identities"]
        assert document["elements"], "the case answered nothing, so the equality is vacuous"

    @pytest.mark.parametrize(
        "flags",
        [
            [],
            ["--where", "kind=value_type"],
            ["--exists", "base"],
            ["--limit", "1"],
            ["--limit", "1", "--after", "Speed"],
        ],
    )
    def test_cli_query_request_answers_a_layer_as_the_flags_do(
        self, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        self._assert_request_is_the_flags("implementation_types", flags, tmp_path, monkeypatch, capsys)

    @pytest.mark.parametrize(
        "flags",
        [
            ["--members", "ports"],
            ["--members", "memory"],
            ["--members", "ports", "--where", "direction=require"],
            ["--members", "ports", "--limit", "1", "--after", '{"element": "SpeedSwc", "member": "pSpeed"}'],
        ],
    )
    def test_cli_query_request_answers_a_member_slice_as_the_flags_do(
        self, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        self._assert_request_is_the_flags("swc", flags, tmp_path, monkeypatch, capsys)

    @pytest.mark.parametrize(
        "flags",
        [
            ["--members", "runnables.writes"],
            ["--members", "runnables.reads", "--limit", "1"],
            [
                "--members",
                "runnables.writes",
                "--limit",
                "1",
                "--after",
                '{"element": "SpeedSwc", "through": ["SpeedRun"], "member": {"port": "pSpeed", "element": "speed"}}',
            ],
        ],
    )
    def test_cli_query_request_answers_a_member_path_as_the_flags_do(
        self, flags: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        self._assert_request_is_the_flags("swc", flags, tmp_path, monkeypatch, capsys, _write_runnable_query_project)

    def _assert_exists_both_ways(
        self, layer: str, field: str, carried: list[str], not_carried: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`exists` through `swcraft query --request` and the protocol's `read` agree.

        A field not carried, a `null` and the empty string are no value; `0` and `false` are
        values. The empty map is asserted in `test_query.py`: no non-collection field holds one.
        """
        manifest = write_query_project(tmp_path)
        model_root = tmp_path / "model"
        dump_yaml(
            model_root / "tasks" / "T_blank.yaml",
            {"name": "T_blank", "period": 0.01, "priority": 0, "partition": "PARTITION0", "description": ""},
        )
        dump_yaml(
            model_root / "tasks" / "T_stated.yaml",
            {
                "name": "T_stated",
                "period": 0.02,
                "priority": 1,
                "partition": "PARTITION0",
                "wcet": "500us",
                "description": "x",
            },
        )
        for exists, expected in ((True, carried), (False, not_carried)):
            body = {"address": {"layer": layer}, "where": [{"field": field, "exists": exists}]}
            request = self._request_file(tmp_path, body, name=f"request-{exists}.json")
            out, _ = self._run_query(
                ["--request", str(request), "--format", "json", "--project", str(manifest)], monkeypatch, capsys
            )
            response = engine.handle_request({"operation": "read", "form": "index", **body}, project=str(manifest))
            assert json.loads(out)["elements"] == response["value"]["identities"] == expected

    @pytest.mark.parametrize(
        ("field", "carried", "not_carried"),
        [
            # `""` and `"x"`: a stated empty description is no value, a text is one.
            ("description", ["T_stated"], ["T_blank"]),
            # `null` and a value: an unstated `wcet` dumps as null.
            ("wcet", ["T_stated"], ["T_blank"]),
            # `0` is a value.
            ("priority", ["T_blank", "T_stated"], []),
        ],
        ids=["empty-string-and-text", "null", "zero"],
    )
    def test_exists_asks_whether_a_task_carries_a_value(
        self, field: str, carried: list[str], not_carried: list[str], tmp_path: Path, monkeypatch, capsys
    ) -> None:
        self._assert_exists_both_ways("tasks", field, carried, not_carried, tmp_path, monkeypatch, capsys)

    def test_exists_counts_false_as_a_value(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # Every component carries `external: false`.
        self._assert_exists_both_ways("swc", "external", ["SpeedSwc", "CtrlSwc"], [], tmp_path, monkeypatch, capsys)

    def test_exists_counts_a_field_not_carried_and_null_as_no_value(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # The processor and the memory do not declare `hw_id`; the core declares it and states none.
        self._assert_exists_both_ways("hardware", "hw_id", [], ["CPU", "CORE0", "RAM0"], tmp_path, monkeypatch, capsys)

    def test_cli_query_request_reaches_exists_false_which_no_flag_spells(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``{"exists": false}`` is the one clause the flags cannot state; the request reaches it, as ``read`` does."""
        manifest = write_query_project(tmp_path)
        body = {"address": {"layer": "implementation_types"}, "where": [{"field": "unit", "exists": False}]}
        request = self._request_file(tmp_path, body)
        out, _ = self._run_query(
            ["--request", str(request), "--format", "json", "--project", str(manifest)], monkeypatch, capsys
        )
        document = json.loads(out)
        response = engine.handle_request({"operation": "read", "form": "index", **body}, project=str(manifest))
        assert document["elements"] == response["value"]["identities"] == ["Alias"]
        assert document["where"] == [{"field": "unit", "exists": False}]
        text, _ = self._run_query(["--request", str(request), "--project", str(manifest)], monkeypatch, capsys)
        assert text == "implementation_types: Alias\n\n(where: unit absent)\n"

    @pytest.mark.parametrize(
        ("beside", "named"),
        [
            (["swc"], "LAYER"),
            (["--where", "kind=value_type"], "--where"),
            (["--exists", "base"], "--exists"),
            (["--members", "ports"], "--members"),
            (["--limit", "1"], "--limit"),
            (["--after", "Speed"], "--after"),
            (["--dependents", "ISpeed"], "--dependents"),
            (["--deps", "SpeedSwc"], "--deps"),
            (["--depth", "1"], "--depth"),
            # Given empty is still given: presence decides, never truthiness.
            (["--members", ""], "--members"),
            (["--dependents", ""], "--dependents"),
            (["--deps", ""], "--deps"),
        ],
    )
    def test_cli_query_request_is_refused_beside_any_slice_or_walk_argument(
        self, beside: list[str], named: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`--request` beside a slice or walk argument: exit 1, the argument named, before the project is read."""
        request = self._request_file(tmp_path, {"address": {"layer": "swc"}})
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", *beside, "--request", str(request), "--project", str(tmp_path / "absent.yaml")],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert f"--request FILE carries the whole request; {named} cannot be given beside it" in err

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (None, "cannot read the file"),
            (b"{not json", "not a JSON document (Expecting property name enclosed in double quotes"),
            (b'{"address": {"layer": "swc"}, "limit": NaN}', "not a JSON document (NaN is not a JSON value)"),
            (b'{"address": {"layer": "swc"}, "after": Infinity}', "not a JSON document (Infinity is not a JSON value)"),
            (b'{"address": {"layer": "swc"}, "after": -Infinity}', "(-Infinity is not a JSON value)"),
            (
                b'{"address": {"layer": "swc"}, "where": [{"field": "name", "equals": 1e400}]}',
                "not a JSON document (1e400 is beyond the range of a finite number)",
            ),
            (
                b'{"address": {"layer": "swc"}, "limit": ' + b"1" * 4301 + b"}",
                "not a JSON document (Exceeds the limit (4300 digits)",
            ),
            (b"[" * 200_000 + b"]" * 200_000, "not a JSON document (maximum recursion depth exceeded"),
            (b"\xff\xfe{}", "not UTF-8 text"),
            (b'["swc"]', "a request is a JSON object, not list"),
            (b'{"operation": "read", "address": {"layer": "swc"}}', "carries no ['operation']"),
            (b'{"address": {"layer": "swc", "element": "SpeedSwc"}}', "names a layer and nothing in it"),
            (b'{"address": {"layer": "swc"}, "form": "summary"}', "`full`"),
            (b'{"address": {"layer": "swc"}, "members": "ports."}', "no step is empty"),
            (b'{"address": {"layer": "swc"}, "derived": ["base_type"]}', 'states `form: "index"`'),
        ],
        # Short ids: Windows refuses an environment variable over 32 767 characters,
        # and pytest writes the node id into PYTEST_CURRENT_TEST.
        ids=[
            "unreadable",
            "not-json",
            "nan",
            "infinity",
            "neg-infinity",
            "1e400",
            "4301-digits",
            "200k-nesting",
            "not-utf8",
            "list",
            "operation-key",
            "element-in-layer",
            "form-summary",
            "empty-step",
            "derived-needs-index",
        ],
    )
    def test_cli_query_request_file_it_cannot_take_exits_one_and_is_named(
        self, content: bytes | None, expected: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A missing, unreadable or malformed file: exit 1 before the project, the file named as spelled."""
        monkeypatch.chdir(tmp_path)
        if content is not None:
            (tmp_path / "req.json").write_bytes(content)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--request", "req.json", "--project", str(tmp_path / "absent.yaml")]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: --request req.json: "), err
        assert expected in err

    # Identity rows: `derived` on both surfaces

    def _assert_rows_one_backend(
        self, body: dict[str, Any], tmp_path: Path, monkeypatch, capsys, *, duplicate_delegation: bool = False
    ) -> dict[str, Any]:
        """``--request``'s ``rows`` document and the protocol's ``read`` are one answer.

        The rows, the page block and the echo must be equal; the rows' computation is the
        ``query`` package's for both surfaces.
        """
        manifest = _write_derived_query_project(tmp_path, duplicate_delegation=duplicate_delegation)
        request = self._request_file(tmp_path, body)
        out, _ = self._run_query(
            ["--request", str(request), "--format", "json", "--project", str(manifest)], monkeypatch, capsys
        )
        document = json.loads(out)
        assert document["schemaVersion"] == query.QUERY_ROWS_SCHEMA_VERSION
        assert "elements" not in document
        response = engine.handle_request({"operation": "read", **body}, project=str(manifest))
        assert response["response"] == "read", response
        for key in ("derived", "join", "members", "where"):
            assert document.get(key) == body.get(key) == response.get(key), key
        assert document["rows"] == response["value"]["identities"]
        assert document.get("page") == response.get("page")
        assert document["rows"], "the case answered nothing, so the equality is vacuous"
        return document

    def test_cli_query_request_answers_connection_ends_as_the_protocol_does(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        body = {"address": {"layer": "connectors"}, "form": "index", "derived": ["connection_ends"]}
        document = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys)
        assert [row["derived"]["connection_ends"]["requester"] for row in document["rows"]] == [
            {"swc": "CtrlSwc", "port": "rSpeed"},
            {"swc": "LogSwc", "port": "rSpeed"},
        ]

    @pytest.mark.parametrize("path", ["runnables.writes", "runnables.reads"])
    def test_cli_query_request_answers_base_type_as_the_protocol_does(
        self, path: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        body = {"address": {"layer": "swc"}, "form": "index", "members": path, "derived": ["base_type"]}
        document = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys)
        assert {json.dumps(row["derived"]) for row in document["rows"]} == {
            json.dumps({"base_type": {"implementation": "Speed", "base": "float32"}})
        }

    def test_cli_query_request_rows_filter_before_the_values(self, tmp_path: Path, monkeypatch, capsys) -> None:
        body = {
            "address": {"layer": "swc"},
            "form": "index",
            "members": "runnables.writes",
            "where": [{"field": "port", "equals": "pSpeed"}],
            "derived": ["base_type"],
        }
        document = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys)
        assert document["where"] == body["where"]

    def test_cli_query_request_walks_multiplied_rows_once_on_both_surfaces(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A set-valued name multiplies one connector's row; a whole-row cursor pages each row once."""
        body = {"address": {"layer": "connectors"}, "form": "index", "derived": ["connection_ends"]}
        whole = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys)["rows"]
        assert [row["element"] for row in whole] == ["Speed_to_Group", "Speed_to_Group"]
        walked: list[Any] = []
        page_body: dict[str, Any] = {**body, "limit": 1}
        while True:
            document = self._assert_rows_one_backend(page_body, tmp_path / f"p{len(walked)}", monkeypatch, capsys)
            walked.extend(document["rows"])
            if "next" not in document["page"]:
                break
            page_body = {**body, "limit": 1, "after": document["page"]["next"]}
        assert walked == whole

    def test_cli_query_request_collapses_an_invalid_models_identical_rows(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        body = {"address": {"layer": "connectors"}, "form": "index", "derived": ["connection_ends"]}
        document = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys, duplicate_delegation=True)
        assert len(document["rows"]) == 2

    def test_cli_query_request_rows_in_text_are_one_json_line_each(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_derived_query_project(tmp_path)
        body = {"address": {"layer": "connectors"}, "form": "index", "derived": ["connection_ends"], "limit": 1}
        request = self._request_file(tmp_path, body)
        out, _ = self._run_query(
            ["--request", str(request), "--format", "text", "--project", str(manifest)], monkeypatch, capsys
        )
        lines = out.splitlines()
        row = json.loads(lines[0])
        assert row["element"] == "Speed_to_Group"
        assert lines[0] == json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        assert lines[1] == ""
        assert lines[2].startswith("(derived: connection_ends; page: 1 of 2, truncated; next: after=")

    # Identity rows: `join` on both surfaces

    #: A sender-receiver table: each write, the connector that carries it, and each
    #: read of the same data element at the requester end.
    PILOT_JOIN: ClassVar[dict[str, Any]] = {
        "address": {"layer": "swc"},
        "form": "index",
        "members": "runnables.writes",
        "derived": ["base_type"],
        "join": [
            {
                "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
                "on": [
                    {"left": "element", "right": "derived.connection_ends.provider.swc"},
                    {"left": "member.port", "right": "derived.connection_ends.provider.port"},
                ],
                "as": "connection",
            },
            {
                "with": {"address": {"layer": "swc"}, "members": "runnables.reads"},
                "on": [
                    {"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
                    {"left": "connection.derived.connection_ends.requester.port", "right": "member.port"},
                    {"left": "member.element", "right": "member.element"},
                ],
                "as": "reader",
            },
        ],
    }

    def test_cli_query_request_answers_the_pilot_join_as_the_protocol_does(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The connector fans out to two requesters, so each write is a row per reader."""
        document = self._assert_rows_one_backend(self.PILOT_JOIN, tmp_path, monkeypatch, capsys)
        assert {(row["element"], row["reader"]["element"]) for row in document["rows"]} == {
            ("SpeedSwc", "CtrlSwc"),
            ("SpeedSwc", "LogSwc"),
        }
        assert all(row["member"]["element"] == row["reader"]["member"]["element"] for row in document["rows"])

    def test_cli_query_request_answers_a_one_entry_join_without_derived(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A join alone makes a rows answer: the outer rows are wrapped and nothing is echoed as `derived`."""
        entry = {
            **self.PILOT_JOIN["join"][0],
            "on": [{"left": "element", "right": "derived.connection_ends.provider.swc"}],
        }
        body = {"address": {"layer": "swc"}, "form": "index", "join": [entry]}
        document = self._assert_rows_one_backend(body, tmp_path, monkeypatch, capsys)
        assert "derived" not in document
        assert [row["element"] for row in document["rows"]] == ["SpeedSwc", "SpeedSwc"]
        assert [set(row) for row in document["rows"]] == [{"element", "connection"}] * 2

    def test_cli_query_request_walks_a_joined_answer_once_on_both_surfaces(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        whole = self._assert_rows_one_backend(self.PILOT_JOIN, tmp_path, monkeypatch, capsys)["rows"]
        walked: list[Any] = []
        page_body: dict[str, Any] = {**self.PILOT_JOIN, "limit": 1}
        while True:
            document = self._assert_rows_one_backend(page_body, tmp_path / f"p{len(walked)}", monkeypatch, capsys)
            walked.extend(document["rows"])
            if "next" not in document["page"]:
                break
            page_body = {**self.PILOT_JOIN, "limit": 1, "after": document["page"]["next"]}
        assert walked == whole

    def test_cli_query_request_join_in_text_names_the_aliases(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = _write_derived_query_project(tmp_path)
        request = self._request_file(tmp_path, self.PILOT_JOIN)
        out, _ = self._run_query(
            ["--request", str(request), "--format", "text", "--project", str(manifest)], monkeypatch, capsys
        )
        lines = out.splitlines()
        assert json.loads(lines[0])["reader"]["element"] in {"CtrlSwc", "LogSwc"}
        assert lines[-1] == "(members: runnables.writes; derived: base_type; join: connection, reader)"

    @pytest.mark.parametrize(
        ("on", "joined", "expected"),
        [
            (
                [{"left": "member", "right": "element"}],
                {"address": {"layer": "connectors"}},
                "join 'connection': `left` path 'member' names no value of the row so far",
            ),
            (
                [{"left": "element", "right": "derived.connection_ends"}],
                {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
                "join 'connection': `right` path 'derived.connection_ends' names no value of the joined read's row",
            ),
            (
                [{"left": "element", "right": "element"}],
                {"address": {"layer": "no_such_layer"}},
                "join 'connection': the `address` names no declared layer: 'no_such_layer'",
            ),
            (
                [{"left": "element", "right": "element"}],
                {"address": {"layer": "connectors"}, "derived": ["base_type"]},
                "join 'connection': `derived` names 'base_type', which is computed at the members",
            ),
            (
                [{"left": "element", "right": "element"}],
                {"address": {"layer": "swc"}, "members": "runnables.bogus"},
                "join 'connection': `members` 'runnables.bogus': 'bogus' is not a declared field",
            ),
            (
                [{"left": "element", "right": "element"}],
                {"address": {"layer": "swc"}, "where": [{"field": "bogus", "equals": 1}]},
                "join 'connection': `where` 'bogus': not a declared field",
            ),
        ],
        ids=["left-outside", "right-outside", "joined-layer", "joined-derived", "joined-members", "joined-where"],
    )
    def test_cli_query_request_refuses_a_join_the_declarations_cannot_answer(
        self, on: Any, joined: dict[str, Any], expected: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _write_derived_query_project(tmp_path)
        body = {
            "address": {"layer": "swc"},
            "form": "index",
            "members": "runnables.writes",
            "join": [{"with": joined, "on": on, "as": "connection"}],
        }
        request = self._request_file(tmp_path, body, name="req.json")
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--request", str(request), "--format", "json", "--project", str(manifest)]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith(f"Error: --request {request}: "), captured.err
        assert expected in captured.err

    def _refused_on_both_surfaces(
        self, body: dict[str, Any], tmp_path: Path, monkeypatch, capsys
    ) -> tuple[str, dict[str, Any]]:
        """*body* refused by ``swcraft query --request`` (its stderr) and by ``engine.handle_request`` (its error)."""
        manifest = _write_derived_query_project(tmp_path)
        request = self._request_file(tmp_path, body, name="req.json")
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--request", str(request), "--format", "json", "--project", str(manifest)]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        response = engine.handle_request({"operation": "read", **body}, project=str(manifest))
        assert response["response"] == "error", response
        return capsys.readouterr().err, response

    @staticmethod
    def _self_joins(entries: int) -> dict[str, Any]:
        """A layer read joined *entries* times with itself on ``element``, each entry under its own alias."""
        return {
            "address": {"layer": "swc"},
            "form": "index",
            "join": [
                {"with": {"address": {"layer": "swc"}}, "on": [{"left": "element", "right": "element"}], "as": f"s{n}"}
                for n in range(1, entries + 1)
            ],
        }

    def test_both_surfaces_answer_a_join_of_max_entries_entries(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """``query.JOIN_MAX_ENTRIES`` (8) entries are admitted, and answered alike."""
        document = self._assert_rows_one_backend(
            self._self_joins(query.JOIN_MAX_ENTRIES), tmp_path, monkeypatch, capsys
        )
        assert document["rows"]
        assert all(row[f"s{query.JOIN_MAX_ENTRIES}"]["element"] == row["element"] for row in document["rows"])

    def test_both_surfaces_refuse_a_join_over_max_entries_alike(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A ninth entry is refused at parse: ``schema-violation`` on the protocol, exit 1 on the CLI, one sentence."""
        err, response = self._refused_on_both_surfaces(
            self._self_joins(query.JOIN_MAX_ENTRIES + 1), tmp_path, monkeypatch, capsys
        )
        expected = query.join_entries_refusal(query.JOIN_MAX_ENTRIES + 1)
        assert expected.startswith("`join` carries 9 entries, above the 8 a join may carry")
        assert expected in err, err
        assert response["error"] == "schema-violation"
        assert response["failure_class"] == "invalid"
        assert expected in response["message"], response

    @pytest.mark.parametrize(
        ("key", "value", "other_sentence"),
        [
            ("as", "bad.alias", "a join alias matches"),
            ("on", [], "`on` is an array of at least one condition"),
        ],
        ids=["dotted-alias", "empty-on"],
    )
    def test_both_surfaces_refuse_a_malformed_ninth_entry_by_the_cap(
        self, key: str, value: Any, other_sentence: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Nine entries, the ninth malformed: both surfaces count before they parse, so both give the cap sentence."""
        body = self._self_joins(query.JOIN_MAX_ENTRIES + 1)
        body["join"][-1][key] = value
        err, response = self._refused_on_both_surfaces(body, tmp_path, monkeypatch, capsys)
        expected = query.join_entries_refusal(query.JOIN_MAX_ENTRIES + 1)
        assert expected in err, err
        assert response["error"] == "schema-violation"
        assert expected in response["message"], response
        assert other_sentence not in err, err
        assert other_sentence not in response["message"], response

    def test_both_surfaces_refuse_an_outer_where_before_a_derived_name(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        body = {
            "address": {"layer": "swc"},
            "form": "index",
            "members": "runnables.writes",
            "where": [{"field": "bogus", "equals": 1}],
            "derived": ["nope"],
        }
        err, response = self._refused_on_both_surfaces(body, tmp_path, monkeypatch, capsys)
        assert "`where` 'bogus'" in err and "`derived`" not in err, err
        assert response["error"] == "unknown-field"
        assert "'bogus'" in response["message"] and "`derived`" not in response["message"], response

    def test_both_surfaces_refuse_a_later_entrys_path_before_an_earlier_joined_where(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Every entry's declarations are decided before any joined read's ``where`` is answered."""
        body = {
            "address": {"layer": "swc"},
            "form": "index",
            "join": [
                {
                    "with": {"address": {"layer": "connectors"}, "where": [{"field": "bogus", "equals": 1}]},
                    "on": [{"left": "element", "right": "element"}],
                    "as": "connection",
                },
                {"with": {"address": {"layer": "swc"}}, "on": [{"left": "nope", "right": "element"}], "as": "reader"},
            ],
        }
        err, response = self._refused_on_both_surfaces(body, tmp_path, monkeypatch, capsys)
        expected = "join 'reader': `left` path 'nope' names no value of the row so far"
        assert expected in err, err
        assert response["error"] == "schema-violation"
        assert response["message"].startswith(expected), response

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                {"address": {"layer": "swc"}, "form": "index", "members": "runnables.writes", "derived": ["nope"]},
                "`derived` names 'nope', which is no computed value this server publishes",
            ),
            (
                {"address": {"layer": "swc"}, "form": "index", "derived": ["base_type"]},
                "`derived` names 'base_type', which is computed at the members",
            ),
            (
                {
                    "address": {"layer": "swc"},
                    "form": "index",
                    "members": "runnables.writes",
                    "derived": ["base_type"],
                    "limit": 1,
                    "after": {"element": "SpeedSwc"},
                },
                'no row of this answer is {"element": "SpeedSwc"}, so there is no page to continue past it; '
                "`after` is a row this request answered, sent back verbatim",
            ),
        ],
        ids=["outside-the-vocabulary", "at-another-set", "cursor-not-a-row"],
    )
    def test_cli_query_request_refuses_a_derived_request_the_declarations_cannot_answer(
        self, body: dict[str, Any], expected: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = _write_derived_query_project(tmp_path)
        request = self._request_file(tmp_path, body, name="req.json")
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--request", str(request), "--format", "json", "--project", str(manifest)]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith(f"Error: --request {request}: "), captured.err
        assert expected in captured.err

    def test_cli_query_request_naming_no_declared_layer_exits_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The pattern admits ``no_such_layer``; the registry does not, and that is the declarations' refusal."""
        manifest = write_query_project(tmp_path)
        request = self._request_file(tmp_path, {"address": {"layer": "no_such_layer"}})
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--request", str(request), "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith(f"Error: --request {request}: "), err
        assert "names no declared layer: 'no_such_layer'" in err

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                {"address": {"layer": "swc"}, "members": "name"},
                "`members` 'name': declares no collection — it is one value per element. Filter the layer by it "
                'instead (a `where` clause {"field": "name", ...})',
            ),
            (
                {"address": {"layer": "swc"}, "members": "runnables.bogus"},
                "`members` 'runnables.bogus': 'bogus' is not a declared field",
            ),
            (
                {"address": {"layer": "swc"}, "where": [{"field": "bogus", "equals": 1}]},
                "`where` 'bogus': not a declared field of any top-level element kind of layer 'swc'",
            ),
            (
                {"address": {"layer": "swc"}, "where": [{"field": "ports", "exists": True}]},
                "`where` 'ports': declares a collection and a clause tests one value; slice it instead (\"members\": "
                '"ports")',
            ),
            (
                {"address": {"layer": "swc"}, "limit": 1, "after": "NoSuchSwc"},
                "`after` NoSuchSwc: no element or member of this slice carries that identity",
            ),
        ],
        # Short ids: the generated ones carry the whole expected sentence.
        ids=["members-scalar", "members-undeclared", "where-undeclared", "where-collection", "after-unknown"],
    )
    def test_cli_query_request_declarations_refusal_names_the_file_and_its_keys(
        self, body: dict[str, Any], expected: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A declarations refusal names the file and the key as the file spells it, never a flag.

        A flag would send the reader to an argument the command refuses beside ``--request``.
        """
        manifest = _write_runnable_query_project(tmp_path)
        request = self._request_file(tmp_path, body)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--request", str(request), "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith(f"Error: --request {request}: "), err
        assert expected in err, err
        assert "--members" not in err and "--where" not in err and "--after" not in err, err

    def test_cli_query_request_file_with_a_byte_order_mark_is_read(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """RFC 8259 lets a parser skip a leading BOM, and Windows PowerShell 5.1 writes one: the answer is unchanged."""
        manifest = write_query_project(tmp_path)
        body = json.dumps({"address": {"layer": "implementation_types"}}).encode("utf-8")
        plain = tmp_path / "plain.json"
        plain.write_bytes(body)
        marked = tmp_path / "marked.json"
        marked.write_bytes(b"\xef\xbb\xbf" + body)
        by_plain, _ = self._run_query(["--request", str(plain), "--project", str(manifest)], monkeypatch, capsys)
        by_marked, _ = self._run_query(["--request", str(marked), "--project", str(manifest)], monkeypatch, capsys)
        assert by_marked == by_plain
        assert by_plain

    def test_cli_query_names_the_request_file_when_nothing_is_given(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "(or the whole request as --request FILE)" in capsys.readouterr().err

    # Without --request an empty walk name or an empty --members is no argument at all:
    # presence is the --request exclusivity's rule only.
    @pytest.mark.parametrize("walk", ["--dependents", "--deps"])
    def test_cli_query_layer_beside_an_empty_walk_name_answers_the_slice(
        self, walk: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        expected, _ = self._run_query(["swc", "--project", str(manifest)], monkeypatch, capsys)
        out, err = self._run_query(["swc", walk, "", "--project", str(manifest)], monkeypatch, capsys)
        assert out == expected
        assert out
        assert err == ""

    def test_cli_query_walk_beside_an_empty_members_answers_the_walk(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        expected, _ = self._run_query(["--dependents", "ISpeed", "--project", str(manifest)], monkeypatch, capsys)
        out, err = self._run_query(
            ["--dependents", "ISpeed", "--members", "", "--project", str(manifest)], monkeypatch, capsys
        )
        assert out == expected
        assert out
        assert err == ""

    def test_cli_query_an_empty_walk_name_alone_is_refused_as_no_argument(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--dependents", "", "--project", str(manifest)])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "swcraft query takes either a LAYER to slice or one of --dependents/--deps NAME to walk" in captured.err

    def test_cli_query_request_help_names_the_read_request(self) -> None:
        """The flag's help states what the file holds and that it stands alone, so the reference page does too."""
        rendered = " ".join(subcommand_parser(build_parser(), "query").format_help().split())
        assert "a JSON object with the keys of the MCP read request minus `operation`" in rendered
        assert "given alone, beside no LAYER, slice or walk argument" in rendered

    def test_cli_query_members_help_names_the_dotted_path(self) -> None:
        """The flag's help states the path, so the generated reference page does too."""
        rendered = " ".join(subcommand_parser(build_parser(), "query").format_help().split())
        assert "a dotted path of collections (runnables.writes)" in rendered

    def test_cli_query_dependents_walks_and_is_not_impacts_bytes(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`query --dependents` is not `impact`'s bytes: both run over one project and compared."""
        manifest = write_query_project(tmp_path)

        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--dependents", "ISpeed", "--project", str(manifest)])
        main()
        queried = capsys.readouterr().out

        monkeypatch.setattr("sys.argv", ["swcraft", "impact", "ISpeed", "--project", str(manifest)])
        main()
        reported = capsys.readouterr().out

        assert queried.startswith("Dependents of 'ISpeed':")
        assert reported.startswith("Impact of 'ISpeed':")
        assert queried != reported
        for name in ("SpeedSwc", "CtrlSwc"):
            assert name in queried and name in reported

    def test_cli_query_dependents_of_a_core_reach_its_partition_and_then_its_tasks(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A core's tasks are two hops away: `CORE0` -> `PARTITION0` -> `Task_10ms`.

        At depth 1 the task is not reached, which proves the hop is real.
        """
        project = tmp_path / "basic"
        shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", project / "model")
        manifest = _write_manifest(project)

        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--dependents", "CORE0", "--depth", "2", "--project", str(manifest)]
        )
        main()
        two_hops = capsys.readouterr().out.splitlines()

        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "--dependents", "CORE0", "--depth", "1", "--project", str(manifest)]
        )
        main()
        one_hop = capsys.readouterr().out.splitlines()

        assert "  hardware PARTITION0 (core CORE0)" in two_hops
        assert "  task Task_10ms (partition PARTITION0)" in two_hops
        assert "  hardware PARTITION0 (core CORE0)" in one_hop
        assert not any("Task_10ms" in line for line in one_hop)

    def test_cli_impact_gained_depth_and_format_and_nothing_else(self) -> None:
        """`swcraft impact` takes `--project`, `--depth`, `--format` and one positional, read off the parser.

        `direction` and the `limit`/`after` page stay the MCP tool's alone.
        """
        impact_parser = subcommand_parser(build_parser(), "impact")
        assert parser_option_strings(impact_parser) == {"-h", "--help", "--project", "--depth", "--format"}
        assert parser_positional_dests(impact_parser) == ["name"]

    def test_cli_query_deps_walks_the_other_direction(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--deps", "SpeedSwc", "--project", str(manifest)])

        main()

        out = capsys.readouterr().out
        assert out.startswith("Dependencies of 'SpeedSwc':")
        assert "interface ISpeed" in out

    def test_cli_query_depth_bounds_the_walk_and_is_echoed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", "--dependents", "float32", "--depth", "1", "--project", str(manifest)],
        )
        main()
        bounded = capsys.readouterr().out

        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--dependents", "float32", "--project", str(manifest)])
        main()
        unbounded = capsys.readouterr().out

        assert bounded.rstrip("\n").endswith("(depth: 1)")
        assert "(depth" not in unbounded
        # Depth 1 reaches the value type alone.
        assert "SpeedSwc" not in bounded
        assert "SpeedSwc" in unbounded

    def test_cli_query_a_depth_below_one_exits_one_with_the_engines_sentence(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``--depth 0`` exits 1 with the engine's sentence, for a carried name and an unknown one alike.

        Guards a defect: a name no layer carried answered "not found" instead of refusing.
        """
        manifest = write_query_project(tmp_path)
        for name in ("float32", "NoSuchElement"):
            monkeypatch.setattr(
                "sys.argv",
                ["swcraft", "query", "--dependents", name, "--depth", "0", "--project", str(manifest)],
            )
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1, name
            assert "depth must be at least 1" in capsys.readouterr().err, name

    def test_cli_query_a_where_without_a_field_name_is_refused_by_name(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``--where =red`` and ``--where kind`` are refused as ``FIELD=VALUE`` misspellings.

        Without the ``not name`` guard the first came back as ``unknown-field ''``, which
        reads as a claim about the model.
        """
        manifest = write_query_project(tmp_path)
        for argument in ("=red", "kind"):
            monkeypatch.setattr(
                "sys.argv",
                ["swcraft", "query", "implementation_types", "--where", argument, "--project", str(manifest)],
            )
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1, argument
            assert "expected FIELD=VALUE" in capsys.readouterr().err, argument

    def test_cli_query_a_member_slice_cursor_round_trips_through_after(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """``--after`` takes the cursor object a member slice printed, read from the first page's JSON."""
        manifest = write_query_project(tmp_path)
        base = ["swcraft", "query", "swc", "--members", "ports", "--limit", "1", "--format", "json"]
        monkeypatch.setattr("sys.argv", [*base, "--project", str(manifest)])
        main()
        first = json.loads(capsys.readouterr().out)
        assert first["elements"] == [{"element": "SpeedSwc", "member": "pSpeed"}]
        assert first["page"]["next"] == {"element": "SpeedSwc", "member": "pSpeed"}

        cursor = json.dumps(first["page"]["next"])
        monkeypatch.setattr("sys.argv", [*base, "--after", cursor, "--project", str(manifest)])
        main()
        second = json.loads(capsys.readouterr().out)
        assert second["elements"] == [{"element": "CtrlSwc", "member": "rSpeed"}]
        assert second["page"] == {"total": 2, "returned": 1, "truncated": True}

    def test_cli_query_walk_json_is_versioned(self, tmp_path: Path, monkeypatch, capsys) -> None:
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", "--dependents", "ISpeed", "--format", "json", "--project", str(manifest)],
        )

        main()

        document = json.loads(capsys.readouterr().out)
        assert document["schemaVersion"] == 3
        assert document["name"] == "ISpeed"
        assert document["direction"] == "dependents"
        assert document["depth"] is None
        assert {row["name"] for row in document["matched"]["interface"]} >= {"SpeedSwc", "CtrlSwc"}

    def test_cli_query_layer_choices_come_from_the_registry(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The accepted layer set is the contract's own, so a new layer is queryable with no edit."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "nope", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        message = capsys.readouterr().err
        for layer in production_registry().display_order:
            assert layer in message

    def test_cli_query_an_empty_answer_exits_zero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A slice that matched nothing is an answer, and an uninteresting one."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "query", "implementation_types", "--where", "kind=enum", "--project", str(manifest)]
        )

        main()  # must not raise SystemExit

        assert capsys.readouterr().out == "(where: kind=enum)\n"

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            (["implementation_types", "--where", "colour=red"], "'colour'"),
            (["implementation_types", "--where", "kind"], "FIELD=VALUE"),
            (["swc", "--where", "ports=pSpeed"], "--members ports"),
            (["swc", "--members", "sockets"], "--members 'sockets'"),
            (["swc", "--members", "type"], "--where type=VALUE"),
            (["implementation_types", "--limit", "2", "--after", "NoSuchType"], "--after NoSuchType"),
            (["implementation_types", "--after", "uint8"], "needs --limit"),
            (["implementation_types", "--limit", "0"], "at least 1"),
            (["implementation_types", "--depth", "2"], "--depth bounds a walk"),
            (["--dependents", "ISpeed", "--limit", "2"], "do not apply"),
            (["implementation_types", "--dependents", "ISpeed"], "either a LAYER"),
            ([], "either a LAYER"),
        ],
    )
    def test_cli_query_a_request_it_cannot_answer_exits_nonzero(
        self, arguments: list[str], expected: str, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A refused *request* exits non-zero, each message naming its argument."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "query", *arguments, "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        assert expected in capsys.readouterr().err

    def test_cli_query_refuses_both_walk_flags_together(self, tmp_path: Path, monkeypatch) -> None:
        """argparse's own mutually exclusive group, which the guide page renders."""
        manifest = write_query_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "query", "--dependents", "ISpeed", "--deps", "ISpeed", "--project", str(manifest)],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_cli_query_writes_nothing_into_the_project(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Read-only, checked rather than asserted in prose."""
        manifest = write_query_project(tmp_path)
        before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
        for arguments in (["swc"], ["swc", "--members", "ports"], ["--dependents", "ISpeed"]):
            monkeypatch.setattr("sys.argv", ["swcraft", "query", *arguments, "--project", str(manifest)])
            main()
            capsys.readouterr()
        assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
