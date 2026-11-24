"""The guide's command reference: the content oracle, the refusal path, the version hazard.

``swcraft_tools.guide_reference.cli`` reads the ``argparse`` tree and no declaration of the model.
Each check is derived from the parser, never transcribed:

* **the content oracle** — every subcommand has a section and every option and positional a row,
  computed by walking ``cli.dispatch.build_parser()`` independently of the generator's own walk;
* **the sensitivity test** — an option added to a copy of the parser changes the rendered bytes,
  so a generator emitting a frozen string fails;
* **the refusal path** — an action class, an ``nargs`` spelling and a missing help string with no
  rendering each raise :class:`~swcraft_tools.guide_reference.cli.UnrenderableAction`, shown firing;
* **the version hazard** — ``--version`` carries the release number in its ``version`` attribute:
  the bytes carry no version, and the row carries the ``help`` and not the ``version`` (asserted with
  a canary version, so it holds whatever the release number is spelled as).

**What this module does not reach.** The comparison with the committed file (the drift gate's);
whether any command does what its ``help`` says (no command is run); whether the narrative pages'
fenced invocations parse (the command gate's); and whether the declared behaviour tuples are true
of the command bodies (``test_cli.py``) -- here only that each name is a declared command.
"""

from __future__ import annotations

import argparse
import re

import pytest
from swcraft_tools import __version__, guide_reference
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.guide_reference import cli
from swcraft_tools.guide_reference.render import escape_markup


def _parser() -> argparse.ArgumentParser:
    return build_parser()


def _page() -> str:
    return cli.cli_page()


def _flat(text: str) -> str:
    """Whitespace-collapsed, and ``<``-escaped the way the emitters escape.

    The escape is composed, not restated, so "carried verbatim" stays a claim about the sentence and no
    second copy of the escaping rule can diverge. A no-op on the page, whose ``<`` is already escaped.
    """
    return escape_markup(re.sub(r"\s+", " ", text))


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The declared subcommands, walked here rather than through the generator's own helper."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the swcraft parser declares no subcommands")


def _invocations(parser: argparse.ArgumentParser) -> list[str]:
    """How every action of one parser is written, derived from argparse and not from the generator.

    A second implementation on purpose: an oracle calling the generator's renderer would agree by construction.
    """
    written = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.option_strings:
            written.append(", ".join(action.option_strings))
        else:
            written.append(action.metavar or action.dest.upper())
    return written


# The content oracle: every command, every option, from the parser


def test_every_declared_subcommand_has_a_section() -> None:
    """The page presents itself as every command there is, so the parser's list is the oracle."""
    page = _page()
    missing = [name for name in _subcommands(_parser()) if f"## `swcraft {name}`" not in page]
    assert missing == [], f"commands the page renders no section for: {missing}"


def test_every_declared_subcommand_is_listed_in_the_command_table() -> None:
    """The table is the page's own index; a command reachable only by scrolling is a lost row."""
    page = _page()
    missing = [name for name in _subcommands(_parser()) if f"[`swcraft {name}`](#swcraft-{name})" not in page]
    assert missing == [], f"commands the page's index does not link to: {missing}"


def test_every_command_table_link_points_at_a_heading_on_the_page() -> None:
    """The links target the headings' explicit anchors, derived from the same two strings; they must agree."""
    page = _page()
    headings = {"#" + anchor for anchor in re.findall(r"^## .+ \{ #([^ }]+) \}$", page, flags=re.MULTILINE)}
    targets = set(re.findall(r"\]\((#[a-z0-9-]+)\)", page))
    assert targets, "the page's command table links to nothing"
    assert targets <= headings, f"links to anchors no heading on the page mints: {sorted(targets - headings)}"


def test_every_option_of_every_subcommand_has_a_row() -> None:
    """The oracle proper: the parser's actions, walked here, against the page's rows."""
    page = _page()
    missing = []
    for name, sub in _subcommands(_parser()).items():
        missing.extend(
            f"{name} {written}"
            for written in _invocations(sub)
            if f"| `{written}` |" not in page.split(f"## `swcraft {name}`")[1].split("\n## ")[0]
        )
    assert missing == [], f"options the page renders no row for: {missing}"


def test_every_top_level_option_has_a_row() -> None:
    """`--help` and `--version` are written before the command and have a section of their own."""
    section = _page().split("## Options that come before the command")[1].split("\n## ")[0]
    missing = [written for written in _invocations(_parser()) if f"| `{written}` |" not in section]
    assert missing == [], f"top-level options the page renders no row for: {missing}"


def test_every_help_string_is_carried_verbatim() -> None:
    """The page's sentence about an option is the program's own — one sentence about it, not two."""
    flat = _flat(_page())
    parser = _parser()
    parsers = {"swcraft": parser, **_subcommands(parser)}
    missing = [
        f"{name} {action.dest}"
        for name, owner in parsers.items()
        for action in owner._actions
        if not isinstance(action, argparse._SubParsersAction) and _flat(action.help) not in flat
    ]
    assert missing == [], f"help strings the page does not carry verbatim: {missing}"


def test_every_subcommand_help_string_is_carried_verbatim() -> None:
    """The command table's second column is what `swcraft --help` prints, not a paraphrase of it."""
    flat = _flat(_page())
    missing = [name for name, _, summary in cli.commands(_parser()) if _flat(summary) not in flat]
    assert missing == [], f"subcommand help strings the page does not carry: {missing}"


def test_every_declared_choice_is_rendered() -> None:
    """A `choices` member the page leaves out is a value a reader would never learn is accepted."""
    page = _page()
    missing = []
    for name, sub in _subcommands(_parser()).items():
        for action in sub._actions:
            if action.choices is None:
                continue
            missing.extend(f"{name} {action.dest}={choice}" for choice in action.choices if f"`{choice}`" not in page)
    assert missing == [], f"accepted values the page never names: {missing}"


def test_the_layer_choices_come_from_the_model_and_not_from_a_literal() -> None:
    """The one contract-derived string on the page, asserted to be the registry's own set wherever it renders."""
    from swcraft_tools.model_registry import current_registry

    page = _page()
    missing = [layer for layer in current_registry().display_order if f"`{layer}`" not in page]
    assert missing == [], f"declared layers `swcraft list` accepts that the page never names: {missing}"


def test_the_read_only_commands_state_their_exit_rule() -> None:
    """The one claim the page makes that the parser does not supply, and that a reader acts on."""
    page = _page()
    for name in cli.ALWAYS_EXITS_ZERO + cli.REFUSES_AN_UNANSWERABLE_REQUEST:
        section = page.split(f"## `swcraft {name}`")[1].split("\n## ")[0]
        assert "always succeeds" in section, f"`swcraft {name}` does not state its exit rule"


def test_the_command_that_can_refuse_a_request_says_so_and_the_others_do_not() -> None:
    """``swcraft query``'s second failure class, which the shared sentence does not admit.

    A run that read the project still exits 1 when the declarations cannot answer the request; the page states
    both that failure and the unreadable project's.
    """
    page = _page()
    query = page.split("## `swcraft query`")[1].split("\n## ")[0]
    assert "cannot answer" in query
    assert "`--members` naming no declared collection" in query

    for name in cli.ALWAYS_EXITS_ZERO:
        section = page.split(f"## `swcraft {name}`")[1].split("\n## ")[0]
        assert "cannot answer" not in section, f"`swcraft {name}` claims a refusal class it does not have"


def test_the_declared_behaviour_tuples_name_only_declared_commands() -> None:
    """Hand-held data, held to the parser: a renamed command must not leave a dangling entry."""
    declared = set(_subcommands(_parser()))
    assert set(cli.WRITES_NOTHING) <= declared
    assert set(cli.WRITES_ONLY_WITH_REPORT) <= declared
    # A run without `--report` writes nothing; each tuple's sentence is unqualified or qualified, never both.
    assert set(cli.ALWAYS_EXITS_ZERO) <= set(cli.WRITES_NOTHING + cli.WRITES_ONLY_WITH_REPORT)
    assert set(cli.REFUSES_AN_UNANSWERABLE_REQUEST) <= set(cli.WRITES_NOTHING)
    assert set(cli.WRITES_NOTHING) & set(cli.WRITES_ONLY_WITH_REPORT) == set()
    # The two exit-rule tuples are alternatives, not a hierarchy: each renders a
    # sentence of its own, and a command in both would render two.
    assert set(cli.ALWAYS_EXITS_ZERO) & set(cli.REFUSES_AN_UNANSWERABLE_REQUEST) == set()


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("metrics", "`<dist_root>/metrics/metrics.md`"),
        ("resource-report", "`<dist_root>/resource-report/resource-report.md`"),
    ],
)
def test_the_report_flags_tuple_renders_its_own_sentence_and_not_the_shared_one(name: str, path: str) -> None:
    """``--report`` writes, so the unqualified sentence would be false of either command.

    The exit rule stays (a failed write and a pin mismatch both exit 0); each sentence names its own file.
    """
    assert cli.WRITES_ONLY_WITH_REPORT == ("metrics", "resource-report")
    sentence = cli._behaviour_sentence(name)
    section = _page().split(f"## `swcraft {name}`")[1].split("\n## ")[0]
    assert sentence in _flat(section) or sentence in section
    assert "Without `--report` this command writes no file." in sentence
    assert path in sentence
    assert [other for other in cli.REPORT_FILES.values() if f"`{other}`" in sentence] == [path.strip("`")]
    assert "a project pinned to another SWCraft release" in sentence
    assert "always succeeds on a run that could read the project" in sentence
    assert "This command writes no file." not in sentence


def test_each_report_file_is_the_one_its_command_writes() -> None:
    """The sentence's literal path, held to the constants the two commands write by."""
    from swcraft_tools import metrics_render, resource_report

    assert set(cli.REPORT_FILES) == set(cli.WRITES_ONLY_WITH_REPORT)
    assert cli.REPORT_FILES == {
        "metrics": f"<dist_root>/{metrics_render.REPORT_DIRECTORY}/{metrics_render.MARKDOWN_FILE}",
        "resource-report": f"<dist_root>/{resource_report.REPORT_DIRECTORY}/{resource_report.MARKDOWN_FILE}",
    }


def test_the_change_report_tuple_is_declared_disjoint_and_on_the_page() -> None:
    """The sixth tuple, held as the experiment's is.

    Declared, disjoint from the other five, its own sentence on the page naming the failures it has and what it
    does not reach.
    """
    declared = set(_subcommands(_parser()))
    tuple_ = set(cli.WRITES_A_CHANGE_REPORT)
    assert tuple_ == {"change-report"}
    assert tuple_ <= declared
    for other in (
        cli.WRITES_NOTHING,
        cli.WRITES_ONLY_WITH_REPORT,
        cli.ALWAYS_EXITS_ZERO,
        cli.REFUSES_AN_UNANSWERABLE_REQUEST,
        cli.REFUSES_A_MODEL_CHECK_MODEL_REFUSES,
        cli.ANSWERS_A_HOOK_ON_STDOUT,
    ):
        assert tuple_ & set(other) == set()

    sentence = cli._behaviour_sentence("change-report")
    section = _page().split("## `swcraft change-report`")[1].split("\n## ")[0]
    assert sentence in section
    assert "under `<dist_root>/change-report/`" in sentence
    assert "a name declared twice at either revision" in sentence
    assert "a project pinned to another SWCraft release" in sentence
    assert "With `--fail-on` it fails after writing the report" in sentence
    assert "exits 2 before the command runs" in sentence
    assert "This command writes no file." not in section
    assert "always succeeds" not in section


def test_the_hook_sentence_states_its_exit_rule_per_host() -> None:
    """``swcraft guard``'s exit rule is per host: exit 2 and one stderr line for a Kiro denial, 0 elsewhere.

    Argparse's usage error is exit 2 under every host. Never the unqualified "exits 0" a reader of the
    Kiro hook would take for its rule.
    """
    sentence = cli._behaviour_sentence("guard")
    section = _page().split("## `swcraft guard`")[1].split("\n## ")[0]
    assert sentence in section
    assert "exits 0 once it runs, a denial included, under every host but Kiro, whose denial is exit 2" in sentence
    assert "A deny is exit 2 with exactly one line on stderr, the reason, and nothing on stdout" in sentence
    assert "exits 0 once it runs, a denial included;" not in sentence
    assert "a usage error exits 2 before the command runs" in sentence
    assert "kiro" in section


def test_the_experiment_tuple_is_declared_disjoint_and_on_the_page() -> None:
    """The fourth tuple: declared, disjoint from the exit-rule tuples and ``WRITES_NOTHING``, its sentence on the page.

    The command writes its report and its failures are not the read-only commands' (widening a shared sentence
    past the behaviour would propagate it into a generated page). Its sentence names what it does not reach:
    argparse's usage error, exit 2.
    """
    declared = set(_subcommands(_parser()))
    tuple_ = set(cli.REFUSES_A_MODEL_CHECK_MODEL_REFUSES)
    assert tuple_ == {"experiment"}
    assert tuple_ <= declared
    assert tuple_ & set(cli.WRITES_NOTHING) == set()
    assert tuple_ & set(cli.ALWAYS_EXITS_ZERO) == set()
    assert tuple_ & set(cli.REFUSES_AN_UNANSWERABLE_REQUEST) == set()

    sentence = cli._behaviour_sentence("experiment")
    section = _page().split("## `swcraft experiment`")[1].split("\n## ")[0]
    assert sentence in section
    assert "writes its report under `<dist_root>/experiments/<name>/`" in sentence
    assert "a model `check-model` refuses" in sentence
    assert "a project pinned to another SWCraft release" in sentence
    assert "exits 2 before the command runs" in sentence
    assert "for `trace` also `proposals.json`, which each run rewrites whole" in sentence
    assert "every other finding, an error included, is reported and refuses nothing" in sentence
    assert "a KIND other than `allocation` and `trace`" in sentence
    # Re-stated, not widened: the one form that writes into the model is named with what it writes.
    assert "A search writes nothing into the model; `trace --apply FILE` is the one form that does" in sentence
    assert "and nothing into the model:" not in sentence
    assert "records the decisions in `trace-decisions.yaml`" in sentence
    assert "and with `--dry-run` writes nothing" in sentence
    assert "for `--apply` also a malformed or unreadable proposals or decisions file" in sentence
    # The search's two refusals of its files: the decisions file, and a previous proposals file it cannot
    # read, which `--discard-pending` overrides like a pending one.
    assert "a malformed or unreadable `trace-decisions.yaml`" in sentence
    assert (
        "one it cannot read as a proposals file or one changed while the search ran, unless `--discard-pending` "
        "is given"
    ) in sentence
    assert "unless `--discard-pending` is given" in sentence
    assert "except a file the rollback could not restore, which the error names" in sentence
    assert "This command writes no file." not in section
    assert "always succeeds" not in section


def test_the_mutually_exclusive_options_are_stated() -> None:
    """A row per option says each exists; nothing in the table says they are alternatives."""
    assert "`--component` and `--all-components` cannot be given together." in _page()


def test_the_page_says_the_assistants_own_command_line_is_elsewhere() -> None:
    """The page states the handover to the assistant's own parser, which this module never reads.

    It names `swcraft agent`, not the `swcraft-agent` script; that spelling survives only in the heading's anchor.
    """
    page = _page()
    assert "`swcraft agent` hands everything written after it to the assistant" in page
    assert "`swcraft-agent`" not in page


def test_the_reference_index_lists_the_page_and_links_to_it() -> None:
    """The section index is rendered from the union, so a new page arrives in it with no second edit."""
    artifacts = guide_reference.all_artifacts()
    assert cli.CLI_PATH in artifacts
    assert "(cli.md)" in artifacts[guide_reference.INDEX_PATH]


# The version hazard


def test_no_rendered_page_carries_the_product_version() -> None:
    """The guide is not a sixth lockstep site; this generator's ``--version`` action holds the version."""
    carriers = [path for path, text in guide_reference.all_artifacts().items() if __version__ in text]
    assert carriers == [], f"pages embedding the product version {__version__!r}: {carriers}"


def test_an_optional_positional_renders_no_default_and_names_it_in_the_help() -> None:
    """The `_default_cell` decision, pinned in both halves so it cannot rot into a gap.

    ``swcraft diff``'s ``REV_A`` / ``REV_B`` have no argparse default, so the column renders "—"; that is
    defensible only while the help string names the real default, so both halves are asserted.
    """
    row = next(line for line in _page().splitlines() if line.startswith("| `REV_A` |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[2] == "\u2014", f"the optional positional renders a default it does not carry: {row}"
    assert "default: HEAD" in cells[3], f"the help cell no longer names the real default: {row}"


def test_the_version_row_carries_the_help_and_not_the_version_attribute() -> None:
    """The mechanism behind the assertion above, independent of the version's spelling."""
    parser = argparse.ArgumentParser(prog="swcraft")
    parser.add_argument("--version", action="version", version="swcraft 9.9.9-canary")
    parser.add_subparsers(dest="command").add_parser("only", help="the one command")
    page = cli.cli_page(parser=parser)
    assert "9.9.9-canary" not in page
    assert "show program's version number and exit" in page


# Sensitivity: the page moves when the parser does


def test_an_option_added_to_the_parser_changes_the_rendered_bytes() -> None:
    """Without this the oracle would pass against a generator that had stopped walking."""
    before = cli.cli_page(parser=_parser())
    parser = _parser()
    _subcommands(parser)["validate"].add_argument("--invented", default=None, metavar="X", help="An invented option")
    after = cli.cli_page(parser=parser)
    assert after != before
    assert "`--invented`" in after


def test_a_subcommand_added_to_the_parser_gets_a_section_and_a_row() -> None:
    """The command table and the sections are both walked, so both move."""
    parser = _parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.add_parser("invented", help="An invented command")
            break
    page = cli.cli_page(parser=parser)
    assert "## `swcraft invented`" in page
    assert "[`swcraft invented`](#swcraft-invented)" in page


def test_two_renders_of_the_same_parser_are_byte_identical() -> None:
    """No sorting, no clock, no set iteration order reaching the output."""
    parser = _parser()
    assert cli.cli_page(parser=parser) == cli.cli_page(parser=parser)


# The refusal path, shown firing


def _one_command_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """A minimal ``swcraft``-shaped parser and its one subcommand, for the refusals to perturb."""
    parser = argparse.ArgumentParser(prog="swcraft")
    sub = parser.add_subparsers(dest="command").add_parser("only", help="the one command")
    return parser, sub


def test_an_action_class_with_no_rendering_is_refused() -> None:
    """A `count` would need a different "Accepted values" sentence than any rendered one.

    An action class the generator really has no sentence for (`append` is a rendering now).
    """
    parser, sub = _one_command_parser()
    sub.add_argument("--verbose", action="count", default=0, help="Say more")
    with pytest.raises(cli.UnrenderableAction, match="no guide rendering for the _CountAction action"):
        cli.cli_page(parser=parser)


def test_an_nargs_spelling_with_no_rendering_is_refused() -> None:
    """A repeated positional renders as one row that says nothing about how many values it takes."""
    parser, sub = _one_command_parser()
    sub.add_argument("names", nargs="*", help="Any number of names")
    with pytest.raises(cli.UnrenderableAction, match="no guide rendering for nargs"):
        cli.cli_page(parser=parser)


def test_an_option_with_no_help_is_refused() -> None:
    """A blank description column reads as a statement about the option rather than the gap it is."""
    parser, sub = _one_command_parser()
    sub.add_argument("--silent", default=None, metavar="X")
    with pytest.raises(cli.UnrenderableAction, match="the option declares no help text"):
        cli.cli_page(parser=parser)


def test_a_subcommand_with_no_help_is_refused() -> None:
    """The command table's second column would otherwise be blank for it."""
    parser = argparse.ArgumentParser(prog="swcraft")
    parser.add_subparsers(dest="command").add_parser("only")
    with pytest.raises(cli.UnrenderableAction, match="the subcommand declares no help text"):
        cli.cli_page(parser=parser)


def test_a_parser_with_no_subcommands_is_refused() -> None:
    """A page of one section that claimed to list every command is worse than a page that fails to build."""
    with pytest.raises(cli.UnrenderableAction, match="declares no subcommands"):
        cli.cli_page(parser=argparse.ArgumentParser(prog="swcraft"))


def test_a_renderable_perturbation_still_renders() -> None:
    """The negative control: the harness itself does not make everything fail."""
    parser, sub = _one_command_parser()
    sub.add_argument("--fine", default="yes", metavar="X", help="A renderable option")
    page = cli.cli_page(parser=parser)
    assert "| `--fine` |" in page
    assert "## `swcraft only`" in page


def test_a_remainder_positional_is_rendered_with_a_sentence_of_its_own() -> None:
    """The passthrough positional of ``swcraft agent``: ``argparse.REMAINDER`` has a sentence of its own.

    Added deliberately, not the guard loosened: ``nargs="*"`` stays refused (the negative control above).
    """
    parser, sub = _one_command_parser()
    sub.add_argument("passthrough", nargs=argparse.REMAINDER, metavar="ARGS", help="Handed on unchanged")
    page = cli.cli_page(parser=parser)
    row = next(line for line in page.splitlines() if line.startswith("| `ARGS` |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1] != "one value", "a whole command line is not 'one value'"
    assert "unchanged" in cells[1], row


def test_the_remainder_spelling_is_declared_renderable() -> None:
    """Read off the module's own tuple.

    So the rendering above cannot be reached by a special case somewhere else in the emitter.
    """
    assert argparse.REMAINDER in cli._RENDERED_NARGS


def test_the_agent_command_renders_its_passthrough_row() -> None:
    """The product's own parser, not a perturbation.

    ``swcraft agent`` is the reason the rendering exists, and a page that dropped its one row would still pass
    every test built on a synthetic parser.
    """
    page = _page()
    assert "## `swcraft agent`" in page
    section = page.split("## `swcraft agent`", 1)[1]
    row = next(line for line in section.splitlines() if line.startswith("| `ARGS` |"))
    assert "unchanged" in row


def test_the_two_exit_rule_tuples_are_what_the_page_was_generated_from() -> None:
    """The read-only exit rules, pinned by value rather than by shape.

    A toolchain pin mismatch refuses a writing command and leaves the read-only six answering: it is neither
    an unreadable project nor an unanswerable request, and neither sentence may grow a clause about it, since
    widening one shared sentence past the behaviour would propagate it into a generated page.

    What this does not reach: whether the sentences are true of the command bodies (``test_cli.py``).
    """
    assert cli.ALWAYS_EXITS_ZERO == ("diff", "impact", "list", "metrics", "resource-report")
    assert cli.REFUSES_AN_UNANSWERABLE_REQUEST == ("query",)

    assert cli._behaviour_sentence("list") == (
        "This command writes no file. It always succeeds on a run that could read the project — an empty or "
        "an uncomfortable answer is still an answer. A project it could not read is the failure it reports."
    )
    assert cli._behaviour_sentence("query") == (
        "This command writes no file. It always succeeds on a run that could read the project and answer the "
        "request — an empty answer is still an answer. It reports two failures: a project it could not read, "
        "and a request it refuses — one the model's declarations cannot answer (a filter naming an undeclared "
        "field, `--members` naming no declared collection, an `--after` cursor the slice does not carry, a "
        "`--request` naming no declared layer, a computed value it names that none is or that is computed at "
        "another set, a filter over a computed value it does not ask for or naming no value of it, a filter "
        "asking a set-valued computed value for no value, which yields no row to find, or a join "
        "path that names no value of the row it reads), or "
        "one the arguments themselves refuse (a `--limit` or a `--depth` below 1, an unreadable or malformed "
        "`--request` file, and the combinations that mix the layer slice, the walk and `--request`)."
    )


def test_no_command_section_mentions_the_toolchain_pin() -> None:
    """The toolchain pin reaches the page through no generated sentence: it is no parser surface."""
    assert "toolchain_version" not in _page()


def test_a_tuple_metavar_has_no_single_placeholder() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_argument("--pair", nargs=2, metavar=("FROM", "TO"))
    with pytest.raises(TypeError, match="pair: a tuple metavar"):
        cli._placeholder(action)
