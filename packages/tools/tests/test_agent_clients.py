"""The client table and ``swcraft init --client``.

The client set is one declared table (``templates/clients.json``). This module pins its reader
(:mod:`swcraft_tools.agent_clients`), each shape check seen to fire, and the ``--client`` command line:
repeatable, the table's names as its choices; an unknown name, or ``--client`` without ``--integration
moon``, a usage error (exit 2) that writes nothing; a table naming another template refused (exit 1),
writing nothing; a conflict's warning; an existing file kept. Which files each set writes is
``test_template_contents.py``'s ``TestClientSets``. What this does not reach: the extension's reading of
the same file.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from swcraft_tools import agent_clients
from swcraft_tools.agent_clients import ClientTableError, load_client_table, parse_client_table
from swcraft_tools.cli import main
from swcraft_tools.cli.dispatch import build_parser

TABLE_PATH = Path(agent_clients.__file__).resolve().parent / "templates" / "clients.json"

#: The one line Kiro's two conflicts share.
KIRO_WARNING = (
    "Kiro also reads AGENTS.md, so it reads SWCraft's instructions twice, the second time cut short; "
    'see the guide\'s "In Kiro" page.'
)


def _document() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def _init(monkeypatch: pytest.MonkeyPatch, target: Path, *extra: str) -> None:
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *extra])
    main()


class TestTheReader:
    def test_the_shipped_table_parses(self) -> None:
        table = load_client_table()
        assert table.template == "vehicle-architecture"
        assert [client.label for client in table.clients] == [
            "GitHub Copilot",
            "Codex (OpenAI)",
            "Claude Code (Anthropic)",
            "Kiro",
        ]

    def test_the_table_sits_beside_the_template_not_inside_it(self) -> None:
        """Inside the template tree ``init``'s walk would copy it into every project."""
        assert TABLE_PATH.is_file()
        assert TABLE_PATH.parent.name == "templates"
        assert (TABLE_PATH.parent / load_client_table().template).is_dir()

    def test_every_guide_section_names_a_heading_of_its_page(self) -> None:
        table = load_client_table()
        sections = {client.guide_section for client in table.clients} | {c.guide_section for c in table.conflicts}
        for section in sections:
            page, _, anchor = section.partition("#")
            text = (REPO_ROOT / "docs" / "guide" / page).read_text(encoding="utf-8")
            assert re.search(r"^#+ .*\{ #" + re.escape(anchor) + r" \}$", text, re.MULTILINE), section

    def test_the_module_imports_the_standard_library_alone(self) -> None:
        tree = ast.parse(Path(agent_clients.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        assert imported <= {"__future__", "functools", "json", "collections", "dataclasses", "importlib", "typing"}

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            pytest.param(lambda d: d.pop("default"), "must hold the keys", id="missing-key"),
            pytest.param(lambda d: d.update(extra=1), "must hold the keys", id="extra-key"),
            pytest.param(lambda d: d["clients"][0].pop("label"), "exactly the keys", id="row-key"),
            pytest.param(lambda d: d["clients"].append(dict(d["clients"][0])), "twice", id="duplicate-client"),
            pytest.param(lambda d: d["clients"][1].update(hook="other.json"), "one of its files", id="stray-hook"),
            pytest.param(lambda d: d["clients"][1]["files"].append("../x"), "template-relative", id="climbing-path"),
            pytest.param(lambda d: d["clients"][1].update(guideSection="vscode.md"), "<page>.md#", id="no-anchor"),
            pytest.param(lambda d: d.update(default=["cursor"]), "names no client", id="unknown-default"),
            pytest.param(lambda d: d["conflicts"][0].update(clients=["copilot"]), "at least two", id="lone-conflict"),
            pytest.param(lambda d: d["conflicts"][0].update(warning="a\nb"), "one line", id="two-line-warning"),
            pytest.param(lambda d: d["copilotCompanionSettings"].update(settings={}), "non-empty", id="no-settings"),
            pytest.param(
                lambda d: d["clients"][3].update(preselectInHosts="kiro"), "non-empty list", id="hosts-not-a-list"
            ),
            pytest.param(lambda d: d["clients"][3].update(preselectInHosts=[]), "non-empty list", id="hosts-empty"),
            pytest.param(
                lambda d: d["clients"][3].update(preselectInHosts=[""]), "non-empty string", id="hosts-empty-string"
            ),
            pytest.param(
                lambda d: d["clients"][3].update(preselectInHosts=[7]), "non-empty string", id="hosts-non-string"
            ),
            pytest.param(
                lambda d: d["clients"][3].update(preselectInHosts=["kiro", "kiro"]), "twice", id="hosts-repeated"
            ),
            pytest.param(
                lambda d: d["clients"][0].update(preselectInHosts=["kiro"]), "'kiro' in two rows", id="host-two-rows"
            ),
        ],
    )
    def test_a_malformed_table_is_refused(self, mutate, fragment: str) -> None:
        document = _document()
        mutate(document)
        with pytest.raises(ClientTableError, match=re.escape(fragment)):
            parse_client_table(json.dumps(document))

    def test_preselect_in_hosts_is_optional_and_read_where_given(self) -> None:
        """A host's ``uriScheme`` named in a row preselects that row; an unnamed host gets the ``default``."""
        table = load_client_table()
        assert {client.name: client.preselect_in_hosts for client in table.clients} == {
            "copilot": (),
            "codex": (),
            "claude": (),
            "kiro": ("kiro",),
        }
        assert table.default == ("copilot",)
        document = _document()
        document["clients"][3].pop("preselectInHosts")
        assert parse_client_table(json.dumps(document)).client("kiro").preselect_in_hosts == ()

    def test_init_keeps_the_default_in_any_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``swcraft init`` has no host: Kiro's ``preselectInHosts`` does not make it write Kiro's files."""
        target = tmp_path / "project"
        _init(monkeypatch, target, "--integration", "moon")
        table = load_client_table()
        assert all((target / path).is_file() for path in table.files_for(table.default))
        assert not any((target / path).exists() for path in table.client("kiro").files)

    def test_conflicts_and_the_companion_rule_answer_the_chosen_set(self) -> None:
        table = load_client_table()
        assert table.warnings_for(["copilot", "claude"]) == (table.conflicts[0].warning,)
        assert table.warnings_for(["copilot", "codex", "claude"]) == (table.conflicts[0].warning,)
        for chosen in (
            ["copilot"],
            ["claude"],
            ["copilot", "codex"],
            ["codex", "claude"],
            ["kiro"],
            ["kiro", "copilot"],
        ):
            assert table.warnings_for(chosen) == (), chosen
        assert table.warnings_for(["kiro", "codex"]) == (KIRO_WARNING,)
        assert table.warnings_for(["kiro", "claude"]) == (KIRO_WARNING,)
        assert table.companion.applies(["copilot", "codex"])
        assert table.companion.applies(["copilot", "claude"])
        assert not table.companion.applies(["copilot"])
        assert not table.companion.applies(["codex", "claude"])

    def test_kiro_s_two_conflicts_share_one_line_and_its_guide_section(self) -> None:
        kiro = [conflict for conflict in load_client_table().conflicts if "kiro" in conflict.clients]
        assert [conflict.clients for conflict in kiro] == [("kiro", "codex"), ("kiro", "claude")]
        assert {conflict.warning for conflict in kiro} == {KIRO_WARNING}
        assert {conflict.guide_section for conflict in kiro} == {"kiro.md#kiro-with-codex-or-claude-code"}

    def test_a_line_two_met_conflicts_share_is_given_once_in_first_appearance_order(self) -> None:
        table = load_client_table()
        assert table.warnings_for(["kiro", "codex", "claude"]) == (KIRO_WARNING,)
        assert table.warnings_for(["copilot", "codex", "claude", "kiro"]) == (table.conflicts[0].warning, KIRO_WARNING)

    def test_the_drop_of_a_repeated_line_is_the_reader_s_not_the_table_s(self) -> None:
        """A synthetic table: three entries met, two sharing a line, in an order the result keeps."""
        document = _document()
        document["conflicts"] = [
            {"clients": ["codex", "claude"], "warning": "B", "guideSection": "vscode.md#a"},
            {"clients": ["copilot", "codex"], "warning": "A", "guideSection": "vscode.md#a"},
            {"clients": ["copilot", "claude"], "warning": "B", "guideSection": "vscode.md#a"},
        ]
        table = parse_client_table(json.dumps(document))
        assert table.warnings_for(["copilot", "codex", "claude"]) == ("B", "A")


class TestTheCommandLine:
    def test_client_is_repeatable_with_the_tables_names_as_its_choices(self) -> None:
        args = build_parser().parse_args(
            ["init", "p", "--integration", "moon", "--client", "codex", "--client", "claude"]
        )
        assert args.client == ["codex", "claude"]
        assert build_parser().parse_args(["init", "p", "--integration", "moon"]).client is None

    def test_an_unknown_client_is_argparse_s_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(["init", "p", "--integration", "moon", "--client", "cursor"])
        assert raised.value.code == 2
        err = capsys.readouterr().err
        for name in load_client_table().names():
            assert name in err

    def test_client_without_integration_is_a_usage_error_writing_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "plain"
        with pytest.raises(SystemExit) as raised:
            _init(monkeypatch, target, "--client", "codex")
        assert raised.value.code == 2
        assert "--client needs --integration moon" in capsys.readouterr().err
        assert not target.exists()

    def test_a_table_of_another_template_is_refused_writing_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The table's ``template`` is read: its paths mean something only in the template it names."""
        document = _document()
        document["template"] = "another-template"
        monkeypatch.setattr(
            "swcraft_tools.cli.init_command.load_client_table", lambda: parse_client_table(json.dumps(document))
        )
        target = tmp_path / "project"
        with pytest.raises(SystemExit) as raised:
            _init(monkeypatch, target, "--integration", "moon")
        assert raised.value.code == 1
        assert "the client table describes the another-template template" in capsys.readouterr().err
        assert not any(path.is_file() for path in target.rglob("*"))

    def test_the_help_names_each_client_from_the_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["init", "--help"])
        help_text = " ".join(capsys.readouterr().out.split())
        for client in load_client_table().clients:
            assert f"'{client.name}' for {client.label}" in help_text
        assert "default: copilot" in help_text

    @pytest.mark.parametrize(
        ("clients", "warnings"),
        [
            pytest.param(("copilot", "claude"), ("copilot",), id="copilot-claude"),
            pytest.param(("copilot", "codex", "claude"), ("copilot",), id="all-three"),
            pytest.param(("copilot", "codex"), (), id="copilot-codex"),
            pytest.param(("claude",), (), id="claude"),
            pytest.param((), (), id="default"),
            pytest.param(("kiro",), (), id="kiro"),
            pytest.param(("kiro", "copilot"), (), id="kiro-copilot"),
            pytest.param(("kiro", "codex"), ("kiro",), id="kiro-codex"),
            pytest.param(("kiro", "codex", "claude"), ("kiro",), id="kiro-codex-claude-once"),
            pytest.param(("copilot", "codex", "claude", "kiro"), ("copilot", "kiro"), id="all-four"),
        ],
    )
    def test_a_conflicting_choice_prints_its_warning_and_asks_nothing(
        self,
        clients: tuple[str, ...],
        warnings: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.stdin", None)  # nothing is read: the choice is the confirmation
        words = [word for name in clients for word in ("--client", name)]
        _init(monkeypatch, tmp_path / "project", "--integration", "moon", *words)
        err = capsys.readouterr().err
        lines = {"copilot": load_client_table().conflicts[0].warning, "kiro": KIRO_WARNING}
        expected = [f"Warning: {lines[key]}" for key in warnings]
        assert [line for line in err.splitlines() if line.startswith("Warning:")] == expected

    def test_an_existing_client_file_is_kept_and_nothing_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A user's own ``.mcp.json`` survives choosing ``claude``; a Codex file survives not choosing ``codex``."""
        target = tmp_path / "project"
        (target / ".codex").mkdir(parents=True)
        (target / ".mcp.json").write_text('{"mine": true}\n', encoding="utf-8", newline="\n")
        (target / ".codex" / "hooks.json").write_text("{}\n", encoding="utf-8", newline="\n")

        _init(monkeypatch, target, "--integration", "moon", "--client", "claude")

        assert (target / ".mcp.json").read_text(encoding="utf-8") == '{"mine": true}\n'
        assert (target / ".codex" / "hooks.json").read_text(encoding="utf-8") == "{}\n"
        assert not (target / ".codex" / "config.toml").exists()
        out = capsys.readouterr().out
        assert f"Skipped {target / '.mcp.json'}" in out
        assert ".codex" not in out, "an unchosen client's file is neither written nor reported"

    def test_an_existing_settings_file_gets_no_companion_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "project"
        (target / ".vscode").mkdir(parents=True)
        (target / ".vscode" / "settings.json").write_text('{"custom": true}', encoding="utf-8", newline="\n")

        _init(monkeypatch, target, "--integration", "moon", "--client", "copilot", "--client", "codex")

        assert (target / ".vscode" / "settings.json").read_text(encoding="utf-8") == '{"custom": true}'

    def test_a_client_named_twice_is_one_choice(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "project"
        _init(monkeypatch, target, "--integration", "moon", "--client", "codex", "--client", "codex")
        assert (target / ".codex" / "config.toml").is_file()
        assert not (target / ".github").exists()


def test_init_with_kiro_writes_its_three_files_and_no_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    _init(monkeypatch, target, "--integration", "moon", "--client", "kiro")
    for relative in (".kiro/settings/mcp.json", ".kiro/steering/swcraft.md", ".kiro/hooks/swcraft-guard.json"):
        assert (target / relative).is_file(), relative
    for absent in ("AGENTS.md", "CLAUDE.md", ".github", ".codex", ".claude", ".mcp.json"):
        assert not (target / absent).exists(), absent
