"""The diagram layout bridge: ``writer/diagrams/layout.py`` and ``elk-layout.mjs``.

The unit half replaces ``subprocess.run`` in the bridge module (the one seam the test rules permit a mock at) and pins
what Python hands Node and what it does with the answer. The Node half runs the real script over the staged elkjs
payload (``node_support.py``).

What these tests do not reach: that ELK's positions are *good* (the renderer's goldens and a reader's eye), and any Node
but the one this host resolves.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import node_support
import pytest
from layout_seam_support import refuse_to_spawn
from node_support import requires_node
from swcraft_tools.writer.diagrams import layout


@pytest.fixture
def node() -> str:
    return requires_node()


def _two_node_graph() -> dict[str, Any]:
    """A root wrapping two members with a port each and one connector between the ports."""
    port_options = {"elk.port.side": "EAST"}
    return {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "RIGHT",
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.spacing.nodeNode": "33.333",
        },
        "children": [
            {
                "id": "A",
                "width": 101.4567,
                "height": 60.3333,
                "layoutOptions": {"elk.portConstraints": "FIXED_SIDE"},
                "ports": [{"id": "A.out", "width": 28, "height": 14, "layoutOptions": port_options}],
            },
            {
                "id": "B",
                "width": 99.9,
                "height": 47.123,
                "layoutOptions": {"elk.portConstraints": "FIXED_SIDE"},
                "ports": [
                    {"id": "B.in", "width": 28, "height": 14, "layoutOptions": {"elk.port.side": "WEST"}},
                ],
            },
        ],
        "edges": [{"id": "e1", "sources": ["A.out"], "targets": ["B.in"]}],
    }


def _framed(answer: str) -> str:
    """*answer* as ``elk-layout.mjs`` writes it: an empty line, the marker line, the answer line."""
    return f"\n{layout.ANSWER_MARKER}\n{answer}\n"


class _Recorder:
    """A ``subprocess.run`` stand-in answering ``--version`` and the layout call.

    Both runs are bytes, as the bridge reads them: a ``str`` given here is encoded to UTF-8, ``bytes`` go out as they
    are.
    """

    def __init__(
        self,
        *,
        version: str | bytes = "v22.23.2\n",
        stdout: str | bytes = _framed("[]"),
        returncode: int = 0,
        stderr: str | bytes = "",
    ):
        self.version = version
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if list(argv)[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_as_bytes(self.version), stderr=b"")
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=_as_bytes(self.stdout), stderr=_as_bytes(self.stderr)
        )


def _as_bytes(text: str | bytes) -> bytes:
    return text.encode("utf-8") if isinstance(text, str) else text


# The unit half


def test_no_graphs_are_laid_out_without_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout.subprocess, "run", refuse_to_spawn)
    monkeypatch.setattr(layout, "resolve_node", refuse_to_spawn)

    assert layout.layout([]) == []


def test_the_argv_is_node_and_the_script_and_the_document_goes_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(stdout=_framed(json.dumps([{"id": "root"}, {"id": "second"}])))
    monkeypatch.setattr(layout.subprocess, "run", recorder)
    graphs = [_two_node_graph(), {"id": "second", "children": []}]

    layout.layout(graphs, node="/opt/node/bin/node")

    (version_argv, _), (argv, kwargs) = recorder.calls
    assert version_argv == ["/opt/node/bin/node", "--version"]
    assert argv == ["/opt/node/bin/node", str(layout.LAYOUT_SCRIPT)]
    assert isinstance(kwargs["input"], bytes), "the document is encoded before any process starts"
    assert json.loads(kwargs["input"].decode("utf-8")) == graphs, "one JSON array, the graphs in the order given"
    assert "encoding" not in kwargs and "text" not in kwargs, "nothing is encoded inside communicate()"
    assert kwargs["capture_output"] is True
    assert kwargs.get("shell", False) is False
    assert kwargs.get("check", False) is False, "the exit code is the bridge's to read"


def test_the_script_is_package_data_beside_the_bridge() -> None:
    """The packaged script sits beside the bridge module, which is where the bridge runs it from.

    That the script finds the staged ``elkjs`` and frames its answer under the bridge's marker is the real run of
    :func:`test_a_graph_is_laid_out_with_positions_on_nodes_ports_and_edge_sections`. Not reached: which payload file
    the script imports, and the marker as a constant shared by the two halves (observed only through a run).
    """
    assert layout.LAYOUT_SCRIPT == Path(layout.__file__).with_name("elk-layout.mjs")
    assert layout.LAYOUT_SCRIPT.is_file()


def test_every_coordinate_comes_back_rounded_to_two_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = [
        {
            "id": "root",
            "x": 0,
            "y": 0.0,
            "width": 212.34567,
            "height": -0.001,
            "layoutOptions": {"elk.spacing.nodeNode": "33.333"},
            "children": [{"id": "A", "x": 12.004, "y": 7.12999999, "ports": [{"id": "p", "x": 1.994, "y": 2.5}]}],
            "edges": [
                {
                    "id": "e1",
                    "sections": [
                        {
                            "startPoint": {"x": 40.123, "y": 7.777},
                            "endPoint": {"x": 80.456, "y": 7.771},
                            "bendPoints": [{"x": 60.009, "y": 7.7749}],
                        }
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(stdout=_framed(json.dumps(answer))))

    (root,) = layout.layout([{"id": "root"}], node="node")

    assert root["width"] == 212.35
    assert root["height"] == 0.0 and str(root["height"]) == "0.0", "a negative zero is not a coordinate"
    assert root["x"] == 0 and isinstance(root["x"], int), "an integer stays an integer"
    assert root["layoutOptions"] == {"elk.spacing.nodeNode": "33.333"}, "strings are not coordinates"
    (child,) = root["children"]
    assert (child["x"], child["y"]) == (12.0, 7.13)
    assert (child["ports"][0]["x"], child["ports"][0]["y"]) == (1.99, 2.5)
    (section,) = root["edges"][0]["sections"]
    assert section["startPoint"] == {"x": 40.12, "y": 7.78}
    assert section["endPoint"] == {"x": 80.46, "y": 7.77}
    assert section["bendPoints"] == [{"x": 60.01, "y": 7.77}]


def test_the_installation_node_is_chosen_over_the_one_on_path(tmp_path: Path) -> None:
    executables = tmp_path / "versions" / "0.12.0" / ("Scripts" if os.name == "nt" else "bin")
    executables.mkdir(parents=True)
    interpreter = executables / ("python.exe" if os.name == "nt" else "python")
    interpreter.write_bytes(b"")
    installed = executables / ("node.exe" if os.name == "nt" else "node")
    installed.write_bytes(b"")
    on_path = tmp_path / "elsewhere" / "node"

    chosen = layout.resolve_node(executable=str(interpreter), which=lambda _name: str(on_path))

    assert chosen == str(installed)


def test_path_node_is_the_fallback_when_the_installation_has_none(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    asked: list[str] = []

    def which(name: str) -> str:
        asked.append(name)
        return "/usr/local/bin/node"

    assert layout.resolve_node(executable=str(interpreter), which=which) == "/usr/local/bin/node"
    assert asked == ["node"]


def test_no_node_anywhere_is_a_refusal_naming_node(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")

    with pytest.raises(layout.DiagramLayoutError, match=r"\bnode\b") as raised:
        layout.resolve_node(executable=str(interpreter), which=lambda _name: None)
    assert "Node.js" in str(raised.value)
    # The shared sentence names no location (an MCP reply carries it); a terminal's names the directory.
    assert str(interpreter.parent) not in str(raised.value)
    assert str(interpreter.parent) in raised.value.render(full_path=True)
    assert raised.value.render() == str(raised.value)


def test_a_script_exit_1_is_a_refusal_carrying_its_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(returncode=1, stdout="", stderr="graph 1: Referenced shape does not exist: ghost\n")
    monkeypatch.setattr(layout.subprocess, "run", recorder)

    with pytest.raises(layout.DiagramLayoutError, match="graph 1: Referenced shape does not exist: ghost"):
        layout.layout([{"id": "a"}, {"id": "b"}], node="node")


def test_a_script_failure_outside_its_own_shapes_keeps_nodes_text_for_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What Node printed with the payload's entry file removed: an uncaught error whose stack names the installation's
    # files. An MCP reply carries str(); it may not.
    stderr = (
        "node:internal/modules/esm/resolve:274\n"
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/opt/swcraft/diagrams/elkjs/lib/main.js' "
        "imported from /opt/swcraft/diagrams/elk-layout.mjs\n"
        "    at finalizeResolution (node:internal/modules/esm/resolve:274:11)\n"
    )
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(returncode=1, stdout="", stderr=stderr))

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="node")

    assert "/opt/swcraft" not in str(raised.value)
    assert "exit 1" in str(raised.value) and "without a message of its own" in str(raised.value)
    assert "/opt/swcraft/diagrams/elkjs/lib/main.js" in raised.value.render(full_path=True)


def test_only_the_scripts_own_line_reaches_the_shared_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = "(node:1) Warning: something at /home/me/x.mjs\ngraph 0: Referenced shape does not exist: ghost\n"
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(returncode=1, stdout="", stderr=stderr))

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="node")

    assert str(raised.value).endswith("graph 0: Referenced shape does not exist: ghost")
    assert "/home/me" not in str(raised.value) and "/home/me" in raised.value.render(full_path=True)


def test_a_missing_layout_engine_is_refused_by_name_before_node_is_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "elk-layout.mjs"
    script.write_text("import ELK from './elkjs/lib/main.js';\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(layout, "LAYOUT_SCRIPT", script)
    monkeypatch.setattr(layout.subprocess, "run", refuse_to_spawn)
    monkeypatch.setattr(layout, "resolve_node", refuse_to_spawn)

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}])

    assert "elkjs" in str(raised.value) and "dev.py install" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert str(tmp_path) in raised.value.render(full_path=True)


def test_a_notice_printed_before_the_version_and_around_the_answer_is_read_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A node shim (proto's) may write a line of its own first; JSON-shaped lines before and after the answer, one with
    # no line end, are read past too.
    notice = '{"level":"info","message":"proto notice"}\n'
    answer = _framed('[{"id": "root"}]')
    recorder = _Recorder(version=f"{notice}v22.13.0\n", stdout=f"{notice}[1, 2]{answer}{notice}[3]")
    monkeypatch.setattr(layout.subprocess, "run", recorder)

    assert layout.node_version("node") == (22, 13, 0)
    assert layout.layout([{"id": "root"}], node="node") == [{"id": "root"}]


def test_both_runs_carry_a_timeout_scaled_to_the_graphs(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(stdout=_framed("[{}, {}, {}]"))
    monkeypatch.setattr(layout.subprocess, "run", recorder)

    layout.layout([{}, {}, {}], node="node")

    (_, version_kwargs), (_, layout_kwargs) = recorder.calls
    assert version_kwargs["timeout"] == layout.VERSION_TIMEOUT_SECONDS
    assert layout_kwargs["timeout"] == layout.LAYOUT_TIMEOUT_BASE_SECONDS + 3 * layout.LAYOUT_TIMEOUT_PER_GRAPH_SECONDS


@pytest.mark.parametrize("which", ["version", "layout"])
def test_a_run_that_does_not_finish_is_a_refusal(monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    def hangs(argv, **kwargs):
        if (list(argv)[1:] == ["--version"]) == (which == "version"):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout=b"v22.13.0\n", stderr=b"")

    monkeypatch.setattr(layout.subprocess, "run", hangs)

    with pytest.raises(layout.DiagramLayoutError, match=r"within \d+ s") as raised:
        layout.layout([{"id": "a"}], node="/slow/node")
    assert "/slow" not in str(raised.value)


def test_a_node_that_answers_its_version_but_cannot_be_started_again_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def second_start_fails(argv, **_kwargs):
        if list(argv)[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"v22.13.0\n", stderr=b"")
        raise PermissionError(13, "Permission denied", argv[0])

    monkeypatch.setattr(layout.subprocess, "run", second_start_fails)

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="/locked/node")
    assert "Permission denied" in str(raised.value) and "/locked" not in str(raised.value)
    assert "/locked/node" in raised.value.render(full_path=True)


def test_a_node_below_the_floor_is_refused_naming_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(version="v12.22.12\n")
    monkeypatch.setattr(layout.subprocess, "run", recorder)
    floor = ".".join(str(part) for part in layout.NODE_FLOOR)

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="/old/node")

    message = raised.value.render(full_path=True)
    assert "12.22.12" in message and floor in message and "/old/node" in message
    shared = str(raised.value)
    assert "12.22.12" in shared and floor in shared and "/old/node" not in shared
    assert len(recorder.calls) == 1, "nothing is laid out by a Node below the floor"


def test_the_floor_is_accepted_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    floor = ".".join(str(part) for part in layout.NODE_FLOOR)
    recorder = _Recorder(version=f"v{floor}\n", stdout=_framed("[{}]"))
    monkeypatch.setattr(layout.subprocess, "run", recorder)

    assert layout.layout([{"id": "a"}], node="node") == [{}]


@pytest.mark.parametrize("version", ["", "22.1.0", "vX.Y.Z\n", "v22\n"])
def test_an_unreadable_version_is_a_refusal(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(version=version))

    with pytest.raises(layout.DiagramLayoutError, match="version"):
        layout.layout([{"id": "a"}], node="node")


def test_a_node_that_cannot_be_started_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def cannot_start(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(layout.subprocess, "run", cannot_start)

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="/gone/node")
    assert "/gone/node" in raised.value.render(full_path=True)
    assert "/gone" not in str(raised.value) and "No such file or directory" in str(raised.value)


@pytest.mark.parametrize("version", ["", "vX.Y.Z\n"])
def test_an_unreadable_version_names_the_node_to_a_terminal_only(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(version=version))

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="/odd/node")
    assert "/odd/node" in raised.value.render(full_path=True)
    assert "/odd" not in str(raised.value)


@pytest.mark.parametrize(
    "stdout",
    [
        _framed("not json"),
        _framed("{}"),
        _framed("[1, 2]"),
        _framed("[{}, {}]"),
        "[{}]",
        f"[{{}}]\n{layout.ANSWER_MARKER}",
        f"\n{layout.ANSWER_MARKER}\n\n[{{}}]\n",
    ],
    ids=["not-json", "object", "not-graphs", "two-graphs", "no-marker", "marker-last", "answer-not-next"],
)
def test_an_answer_that_is_not_one_laid_out_graph_per_graph_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(stdout=stdout))

    with pytest.raises(layout.DiagramLayoutError):
        layout.layout([{"id": "a"}], node="node")


def test_a_document_utf8_cannot_hold_is_refused_before_any_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document UTF-8 cannot hold is refused before the engine lookup, the Node lookup and both runs.

    Encoded inside ``communicate()``, a lone surrogate was met after the layout process had started, and on Windows that
    process hung.
    """
    monkeypatch.setattr(layout, "_refuse_a_missing_engine", refuse_to_spawn)
    monkeypatch.setattr(layout, "resolve_node", refuse_to_spawn)
    monkeypatch.setattr(layout, "node_version", refuse_to_spawn)
    monkeypatch.setattr(layout.subprocess, "run", refuse_to_spawn)

    with pytest.raises(UnicodeEncodeError):
        layout.layout([{"id": "root", "labels": [{"text": "Odd \ud800 title"}]}])


def test_an_answer_line_that_is_not_utf8_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = b"\n" + layout.ANSWER_MARKER.encode("ascii") + b'\n[{"id": "\xff"}]\n'
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(stdout=stdout))

    with pytest.raises(layout.DiagramLayoutError, match="not UTF-8"):
        layout.layout([{"id": "a"}], node="node")


def test_a_diagnostic_that_is_not_utf8_is_replaced_and_the_exit_refusal_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = b"(node:1) Warnung \xfc\ngraph 0: Referenced shape does not exist: g\xe9ost\n"
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(returncode=1, stdout=b"\xff", stderr=stderr))

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="node")

    assert str(raised.value) == "the diagram layout failed (exit 1): graph 0: Referenced shape does not exist: g�ost"
    assert "Warnung �" in raised.value.render(full_path=True)


def test_a_notice_in_another_code_page_around_the_answer_is_read_past(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Windows shim may print in its console's code page (cp1252's 0xE9 is not UTF-8); only the
    # answer line is decoded strictly.
    stdout = b"proto notice: caf\xe9\n" + _framed('[{"id": "root"}]').encode("ascii") + b"tail \xff"
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(stdout=stdout))

    assert layout.layout([{"id": "root"}], node="node") == [{"id": "root"}]


def test_a_notice_in_another_code_page_around_the_version_is_read_past(monkeypatch: pytest.MonkeyPatch) -> None:
    # The version run is the first a shim prints into; a strict decode inside communicate() raised an untyped
    # UnicodeDecodeError there. Decoded with replacement, the notice is read past.
    recorder = _Recorder(version=b"proto notice: caf\xe9\r\nv22.13.0\r\n", stdout=_framed('[{"id": "root"}]'))
    monkeypatch.setattr(layout.subprocess, "run", recorder)

    assert layout.node_version("node") == (22, 13, 0)
    assert layout.layout([{"id": "root"}], node="node") == [{"id": "root"}]
    (_, version_kwargs), *_ = recorder.calls
    assert "encoding" not in version_kwargs and "text" not in version_kwargs, "the version run is read as bytes"


def test_an_unreadable_version_in_another_code_page_is_the_named_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(version=b"Zugriff verweigert: \xfc\r\n"))

    with pytest.raises(layout.DiagramLayoutError, match="version") as raised:
        layout.layout([{"id": "a"}], node="node")
    assert "\ufffd" in raised.value.render(full_path=True), "the undecodable byte is replaced, not raised"


def test_a_crlf_framed_answer_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # The layout run is bytes, so no newline translation happens: a Windows shim's CRLF leaves a trailing b"\r" on the
    # marker and the answer lines.
    stdout = b"\r\n" + layout.ANSWER_MARKER.encode("ascii") + b'\r\n[{"id":"root"}]\r\n'
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(stdout=stdout))

    assert layout.layout([{"id": "root"}], node="node") == [{"id": "root"}]


# The probe's policy -- node_support.requires_node


def _outcome_of(call) -> BaseException | None:
    """What *call* raised -- a pytest skip or failure included.

    ``pytest.raises`` of the other kind would let those through as the test's own outcome and so hide them.
    """
    try:
        call()
    except BaseException as raised:  # noqa: BLE001 — the outcome IS the subject
        return raised
    return None


def test_the_probe_answers_the_resolved_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_support, "_probe", lambda: ("/found/node", ""))
    monkeypatch.setattr(node_support, "SKIP_NODE", False)

    assert requires_node() == "/found/node"


def test_no_node_without_skip_node_fails_rather_than_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_support, "_probe", lambda: (None, "Node.js was not found: no node on PATH."))
    monkeypatch.setattr(node_support, "SKIP_NODE", False)

    outcome = _outcome_of(requires_node)

    assert type(outcome) is pytest.fail.Exception, f"a failure, not {type(outcome).__name__}"
    assert "--skip-node" in str(outcome)
    assert "Node.js was not found" in str(outcome), "the resolution's own refusal is carried"


def test_no_node_with_skip_node_skips_naming_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_support, "_probe", lambda: (None, "Node.js was not found."))
    monkeypatch.setattr(node_support, "SKIP_NODE", True)

    outcome = _outcome_of(requires_node)

    assert type(outcome) is pytest.skip.Exception, f"a skip, not {type(outcome).__name__}"
    assert "--skip-node was passed" in str(outcome)


def test_skip_node_does_not_skip_a_host_that_has_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_support, "_probe", lambda: ("/found/node", ""))
    monkeypatch.setattr(node_support, "SKIP_NODE", True)

    assert requires_node() == "/found/node"


# The Node half -- the real script over the staged elkjs payload


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers(item)]
    if isinstance(value, list):
        return [number for item in value for number in _numbers(item)]
    return []


def test_a_graph_is_laid_out_with_positions_on_nodes_ports_and_edge_sections(node: str) -> None:
    (root,) = layout.layout([_two_node_graph()], node=node)

    children = {child["id"]: child for child in root["children"]}
    assert set(children) == {"A", "B"}
    for child in children.values():
        assert {"x", "y", "width", "height"} <= set(child)
        (port,) = child["ports"]
        assert {"x", "y"} <= set(port)
    assert children["A"]["x"] < children["B"]["x"], "RIGHT puts the source member first"
    (edge,) = root["edges"]
    (section,) = edge["sections"]
    assert {"startPoint", "endPoint"} <= set(section)
    numbers = _numbers(root)
    assert numbers, "the answer carries coordinates"
    assert all(round(number, 2) == number for number in numbers), "every coordinate has at most two decimals"
    assert any(number != int(number) for number in numbers), "the fixture's fractional sizes reach the answer"


def test_graphs_come_back_in_the_order_they_were_given(node: str) -> None:
    first = _two_node_graph()
    second = {"id": "other", "children": [{"id": "only", "width": 10, "height": 10}]}

    laid_out = layout.layout([first, second], node=node)

    assert [graph["id"] for graph in laid_out] == ["root", "other"]


def test_an_elk_failure_names_the_graph_and_elks_own_text(node: str) -> None:
    broken = {
        "id": "broken",
        "children": [{"id": "only"}],
        "edges": [{"id": "e", "sources": ["only"], "targets": ["ghost"]}],
    }

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "fine", "children": []}, broken], node=node)

    message = str(raised.value)
    assert "graph 1" in message, "the index of the graph ELK refused"
    assert "ghost" in message, "ELK's own text reaches the refusal"


def test_two_runs_answer_byte_identically(node: str) -> None:
    graphs = [_two_node_graph(), _two_node_graph()]

    first = json.dumps(layout.layout(graphs, node=node), sort_keys=True)
    second = json.dumps(layout.layout(graphs, node=node), sort_keys=True)

    assert first == second


def test_the_resolved_node_is_above_the_floor(node: str) -> None:
    assert layout.node_version(node) >= layout.NODE_FLOOR


#: Every character Python's ``str.splitlines`` breaks a line at -- ``JSON.stringify`` escapes
#: the ones below U+0020 and writes NEL, LINE SEPARATOR and PARAGRAPH SEPARATOR as they are.
_LINE_BREAKING_TEXT = [" ", " ", "\x85", "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"]


@pytest.mark.parametrize(
    "text",
    [*_LINE_BREAKING_TEXT, "\nswcraft-diagram-layout-answer\n[]\n"],
    ids=[*(f"U+{ord(text[0]):04X}{'-and-more' if len(text) > 1 else ''}" for text in _LINE_BREAKING_TEXT), "framing"],
)
def test_model_text_of_any_character_comes_back_unchanged(node: str, text: str) -> None:
    """Model text holding any line-breaking character comes back unchanged.

    Reading only the last ``splitlines()`` piece would lose U+2028 in a title; the last case spells the answer's own
    framing on lines of its own, which no text may forge.
    """
    graph = _two_node_graph()
    label = f"before{text}after"
    graph["children"][0]["labels"] = [{"text": label, "width": 30, "height": 12}]
    graph["children"][0]["swcraft"] = {"description": label, "title": f"a{text}b{text}c"}

    (root,) = layout.layout([graph], node=node)

    first = next(child for child in root["children"] if child["id"] == "A")
    assert first["labels"][0]["text"] == label
    assert first["swcraft"] == {"description": label, "title": f"a{text}b{text}c"}


def test_an_unreadable_version_shares_no_output_of_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shim's output can name a path or the environment, and ``str()`` is the MCP reply's.

    The exit code is shared; what ``node --version`` printed is the local terminal's alone.
    """
    printed = "proto: cannot find /Users/someone/.proto/tools/node/22 (PROTO_HOME=/Users/someone/.proto)\n"
    monkeypatch.setattr(layout.subprocess, "run", _Recorder(version=printed))

    with pytest.raises(layout.DiagramLayoutError) as raised:
        layout.layout([{"id": "a"}], node="node")

    assert "/Users/someone" not in str(raised.value) and "PROTO_HOME" not in str(raised.value)
    assert "version" in str(raised.value) and "exit 0" in str(raised.value)
    assert "/Users/someone/.proto/tools/node/22" in raised.value.render(full_path=True)
