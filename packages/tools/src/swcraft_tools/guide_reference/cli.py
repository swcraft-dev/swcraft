"""The guide's command reference: every command, option and default.

Its source is the live ``argparse`` tree from ``cli.dispatch.build_parser()`` -- every subcommand, positional,
option, ``choices`` list, default and ``help`` string, in declaration order. The product version never
reaches this page. An action class, an ``nargs`` spelling or a subcommand with no rendering raises
:class:`UnrenderableAction`.

**What this page does not reach:**

* the assistant's own command line (``swcraft agent``);
* whether a command's effect is what its ``help`` says;
* which commands write nothing, always exit 0 or can refuse the request: declared tuples here, so a
  command added to the parser and not to them renders without its sentence;
* the requirement ``--project`` really has: argparse takes it as optional.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator

import swcraft_tools.guide_reference.render as render
from swcraft_tools.cli.dispatch import build_parser

__all__ = [
    "ALWAYS_EXITS_ZERO",
    "ANSWERS_A_HOOK_ON_STDOUT",
    "CLI_PATH",
    "REFUSES_AN_UNANSWERABLE_REQUEST",
    "REFUSES_A_MODEL_CHECK_MODEL_REFUSES",
    "REPORT_FILES",
    "UnrenderableAction",
    "WRITES_A_CHANGE_REPORT",
    "WRITES_A_PASSPORT",
    "WRITES_NOTHING",
    "WRITES_ONLY_WITH_REPORT",
    "all_artifacts",
    "cli_page",
    "commands",
    "invocation",
    "option_row",
]

#: The relative path of the page this module renders.
CLI_PATH = "reference/cli.md"

#: The commands whose bodies write no file. ``check-model`` writes nothing and is still a gate, so it
#: exits non-zero on a model it refuses and is not in :data:`ALWAYS_EXITS_ZERO`.
WRITES_NOTHING = ("check-model", "diff", "impact", "list", "query")

#: The commands that write a file only when ``--report`` asks. A run without the flag writes nothing
#: and every gate runs that form, so each is in :data:`ALWAYS_EXITS_ZERO` and not in the unqualified
#: :data:`WRITES_NOTHING`. Their sentence names the file (:data:`REPORT_FILES`) and its limit: a
#: project pinned to another release gets no file, though the run still answers.
WRITES_ONLY_WITH_REPORT = ("metrics", "resource-report")

#: The file each command of :data:`WRITES_ONLY_WITH_REPORT` writes under ``--report``. Literals;
#: ``test_guide_reference_cli.py`` holds each to the module that writes it.
REPORT_FILES = {
    "metrics": "<dist_root>/metrics/metrics.md",
    "resource-report": "<dist_root>/resource-report/resource-report.md",
}

#: The commands that always exit 0 on a successful run: the read-only side of the "``swcraft
#: validate`` is the gate" rule. Failing to *read* the project is still a non-zero exit.
#:
#: ``query`` is deliberately not a member: it has a second failure class (an unanswerable request),
#: and widening this sentence to cover it would misstate every other member's contract.
ALWAYS_EXITS_ZERO = ("diff", "impact", "list", "metrics", "resource-report")

#: The read-only commands that can also refuse the **request**: ``swcraft query`` alone. It reads the
#: project and still exits non-zero when it cannot answer, for two causes, both named: the model's
#: **declarations** (an undeclared ``--where``/``--exists`` field, ``--members`` naming no declared
#: collection, an ``--after`` cursor the slice does not carry, a ``--request`` naming no declared
#: layer, a ``derived`` name outside the vocabulary or computed at another set, a ``where`` over a
#: computed value not asked for, naming no value of it or asking a set-valued one for no value, a
#: ``join`` path naming no value of the row it reads) and the **arguments** (a ``--limit`` or
#: ``--depth`` below 1, an unreadable or malformed ``--request`` file, the three combinations that mix
#: the layer slice with the walk, and a ``--request`` beside a slice or walk argument).
REFUSES_AN_UNANSWERABLE_REQUEST = ("query",)

#: The commands that write an experiment's report and refuse a model the gate refuses (for
#: ``allocation``; a ``trace`` search refuses only an ambiguous identity, ``trace --apply`` writes into
#: the model and ``trace --documents`` writes nothing). ``swcraft experiment`` alone, in none of the
#: tuples above: it writes, and its failures are its own (a model ``check-model`` refuses, a
#: configuration, name or ``output`` it refuses, the solver absent or failing, a malformed proposals
#: file, a toolchain-pin mismatch). Its own sentence names what it does not reach: argparse's usage
#: error, exit 2 before the command runs.
REFUSES_A_MODEL_CHECK_MODEL_REFUSES = ("experiment",)

#: The commands that answer an agent host's hook on stdout: ``swcraft guard`` alone, in none of the
#: tuples above. It writes nothing but reads a hook payload rather than a project, and its exit rule is
#: per host: under Copilot, Codex and Claude Code it exits 0 once it runs, a denial included, because
#: the decision is on stdout; under Kiro a denial is exit 2 and one stderr line, how Kiro's hook blocks
#: a tool. Its sentence names what it does not reach: argparse's usage error.
ANSWERS_A_HOOK_ON_STDOUT = ("guard",)

#: The commands that write a change report: ``swcraft change-report`` alone, in none of the tuples
#: above. It writes its report, can exit 1 on ``--fail-on`` after a full read, and reports the
#: findings of a model ``check-model`` refuses rather than refusing it. Its sentence names what it
#: does not reach: argparse's usage error.
WRITES_A_CHANGE_REPORT = ("change-report",)

#: The commands that write a model's passport: ``swcraft passport`` alone, in none of the tuples
#: above. It writes to stdout or ``--output``, a toolchain-pin mismatch refuses it as it refuses every
#: writing command, and it passes no verdict on the model. Its sentence names what it does not reach:
#: argparse's usage error.
WRITES_A_PASSPORT = ("passport",)

_TABLE_HEADERS = ("Option or argument", "Accepted values", "Default", "What it is")

#: The action classes the ``swcraft`` parser uses, each with a rendering below. A set of the classes
#: themselves, so a subclass (which exists because it behaves differently) is refused rather than
#: rendered as its base. ``_AppendAction`` is a repeatable option, one AND-composed clause per use.
_RENDERED_ACTIONS = frozenset(
    {
        argparse._HelpAction,
        argparse._VersionAction,
        argparse._StoreAction,
        argparse._StoreTrueAction,
        argparse._AppendAction,
    }
)

#: The ``nargs`` spellings this module renders: ``None`` one value, ``0`` a switch or help action,
#: ``"?"`` an optional positional, ``argparse.REMAINDER`` the passthrough of ``swcraft agent``.
#: Anything else would need a different "Accepted values" sentence, so it is refused.
_RENDERED_NARGS = (None, 0, "?", argparse.REMAINDER)


class UnrenderableAction(ValueError):
    """A parser action this generator has no rendering for.

    Raised instead of skipping the row: a reference missing the one option a reader looks for would
    still read as the complete list.
    """

    def __init__(self, command: str, dest: str, reason: str) -> None:
        self.command = command
        self.dest = dest
        self.reason = reason
        super().__init__(f"{command} {dest}: {reason}")


def _parser(parser: argparse.ArgumentParser | None) -> argparse.ArgumentParser:
    """The parser to render from, built at call time and never at definition time."""
    return build_parser() if parser is None else parser


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """The one dispatcher action of the top-level parser; a parser without subcommands is refused."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise UnrenderableAction(parser.prog, "command", "the parser declares no subcommands")


def commands(parser: argparse.ArgumentParser) -> tuple[tuple[str, argparse.ArgumentParser, str], ...]:
    """``(name, sub-parser, help)`` for every subcommand, in declaration order.

    The help is the dispatcher's own (``_choices_actions``), the string ``swcraft --help`` prints. A
    subcommand with no help is refused.
    """
    action = _subparsers_action(parser)
    helps = {choice.dest: choice.help for choice in action._choices_actions}
    rows = []
    for name, sub in action.choices.items():
        summary = helps.get(name)
        if not summary:
            raise UnrenderableAction(name, "command", "the subcommand declares no help text")
        rows.append((name, sub, summary))
    return tuple(rows)


def _rendered_actions(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    """Every action of one parser except the subcommand dispatcher, in declaration order."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        yield action


def _placeholder(action: argparse.Action) -> str:
    """The value placeholder argparse prints for *action*: its ``metavar``, or its ``dest`` upper-cased.

    Raises:
        TypeError: a tuple ``metavar`` (one name per value of an ``nargs`` count), which has no
            single placeholder for the reference to print.
    """
    if isinstance(action.metavar, tuple):
        raise TypeError(f"{action.dest}: a tuple metavar has no single placeholder in the reference")
    return action.metavar or action.dest.upper()


def invocation(action: argparse.Action) -> str:
    """How one option or argument is written on the command line.

    An option is its option strings joined; a positional is its placeholder. An option's value
    placeholder is left to the "Accepted values" column.
    """
    if action.option_strings:
        return ", ".join(action.option_strings)
    return _placeholder(action)


def _values_cell(action: argparse.Action) -> str:
    """The "Accepted values" column: the choices, the placeholder, or that there is no value.

    A repeatable option says so here: it is a fact about the argument, not the command.
    """
    if action.choices is not None:
        return "one of " + ", ".join(render.code(render.literal(choice)) for choice in action.choices)
    if action.nargs == 0:
        return "nothing — the option is a switch"
    if action.nargs == argparse.REMAINDER:
        return "everything written after the command, passed on unchanged"
    if not action.option_strings:
        # A positional's placeholder is already the first column.
        return "one value"
    placeholder = render.code(_placeholder(action))
    if isinstance(action, argparse._AppendAction):
        return f"{placeholder}, repeatable"
    return placeholder


def _default_cell(action: argparse.Action) -> str:
    """The "Default" column, in three states a reader can act on.

    ``argparse.SUPPRESS`` (``--help``, ``--version``) and a ``None`` default render "—"; a required
    positional says so. A ``None`` default renders "—" even where the command body supplies a real one
    (``swcraft diff``'s ``REV_A``/``REV_B``): that default belongs in the ``help`` string, and a table
    of real defaults here would be a second truth going stale.
    """
    if action.default is argparse.SUPPRESS:
        return "—"
    if not action.option_strings and action.nargs is None:
        return "required"
    if action.default is None:
        return "—"
    if action.default is False:
        return "off"
    if action.default is True:
        return "on"
    return render.code(render.literal(action.default))


def option_row(command: str, action: argparse.Action) -> tuple[str, ...]:
    """One row of a command's option table.

    The description is the action's ``help``, verbatim, and the only attribute of ``--version`` read
    here: its ``version`` would put a release number into a committed page.
    """
    if type(action) not in _RENDERED_ACTIONS:
        raise UnrenderableAction(command, action.dest, f"no guide rendering for the {type(action).__name__} action")
    if action.nargs not in _RENDERED_NARGS:
        raise UnrenderableAction(command, action.dest, f"no guide rendering for nargs {action.nargs!r}")
    if not action.help:
        raise UnrenderableAction(command, action.dest, "the option declares no help text")
    return (
        render.code(invocation(action)),
        _values_cell(action),
        _default_cell(action),
        action.help,
    )


def _exclusive_sentences(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """One sentence per set of options the parser refuses to take together, which the table cannot show."""
    sentences = []
    for group in parser._mutually_exclusive_groups:
        options = [render.code(invocation(action)) for action in group._group_actions]
        if len(options) < 2:
            continue
        sentences.append(" and ".join(options) + " cannot be given together.")
    return tuple(sentences)


def _behaviour_sentence(name: str) -> str:
    """What the command does to the project, for the commands where it is worth saying.

    Writing nothing and always exiting 0 are independent facts, so each gets its own sentence.
    """
    parts = []
    if name in WRITES_NOTHING:
        parts.append("This command writes no file.")
    if name in WRITES_ONLY_WITH_REPORT:
        parts.append(
            "Without `--report` this command writes no file. With it, it also writes "
            f"`{REPORT_FILES[name]}`, nothing into the model, and prints where. "
            "A report that cannot be written is a warning and moves no exit code; a project pinned to another "
            "SWCraft release gets its answer and a warning, and no file."
        )
    if name in ALWAYS_EXITS_ZERO:
        parts.append(
            "It always succeeds on a run that could read the project — an empty or an uncomfortable answer is "
            "still an answer. A project it could not read is the failure it reports."
        )
    if name in REFUSES_AN_UNANSWERABLE_REQUEST:
        parts.append(
            "It always succeeds on a run that could read the project and answer the request — an empty answer "
            "is still an answer. It reports two failures: a project it could not read, and a request it "
            "refuses — one the model's declarations cannot answer (a filter naming an undeclared field, "
            "`--members` naming no declared collection, an `--after` cursor the slice does not carry, a "
            "`--request` naming no declared layer, a computed value it names that none is or that is "
            "computed at another set, a filter over a computed value it does not ask for or naming no value of "
            "it, a filter asking a set-valued computed value for no value, which yields no row to find, or a "
            "join path that names no value of the row it reads), or one "
            "the arguments themselves refuse (a `--limit` or a `--depth` below 1, an unreadable or malformed "
            "`--request` file, and the combinations that mix the layer slice, the walk and `--request`)."
        )
    if name in REFUSES_A_MODEL_CHECK_MODEL_REFUSES:
        parts.append(
            "This command writes its report under `<dist_root>/experiments/<name>/`, or into the directory its "
            "configuration's `output` names inside the project: `report.md` and `result.json`, and for `trace` also "
            "`proposals.json`, which each run rewrites whole. A search writes nothing into the model; `trace "
            "--apply FILE` is the one form that does: it writes the links decided in that proposals file into the "
            "model, records the decisions in `trace-decisions.yaml`, marks them done in the file, all or nothing, "
            "and with `--dry-run` writes nothing; `trace --documents` writes nothing either: it prints each "
            "document's requirements and links and the pass the configuration gives it. It succeeds on a run that "
            "could read the project and answer the "
            "experiment: for `allocation`, no improvement at any number of moves and no component free to move are "
            "still answers; for `trace`, no requirement in scope and a requirement with no candidate are, and for "
            "`--apply`, nothing to do and every decision stale. It reports the failures it has, and writes nothing "
            "on any of them: a flag given to a form it does not apply to, a project it could not read, a "
            "configuration file or a name in it that it refuses, a "
            "report directory outside the project, over the model or on a file, a configured one inside `.git` or "
            "already holding a report another tool or person wrote, and a project pinned to another "
            "SWCraft release; for `allocation` also a model `check-model` refuses (a warning is not a refusal, and "
            "`--warnings-as-errors` is `check-model`'s flag, not this command's), the solver absent, an experiment "
            "the solver's integers cannot represent and a solver that fails to run the experiment; for a `trace` "
            "search also a model in which a name an element's address or a requirement id is built from is "
            "declared twice, a malformed or unreadable `trace-decisions.yaml`, and a previous proposals file "
            "holding decisions not yet applied, one it cannot read as a proposals file or one changed while the "
            "search ran, unless `--discard-pending` is given, while every other finding, an error included, is "
            "reported and refuses nothing (`--documents` refuses as a search does, a previous proposals file "
            "excepted); for `--apply` also a malformed or unreadable proposals or decisions "
            "file, a proposals file outside the project, a plan that differs from the one `--expect-plan` names, "
            "a file changed while it ran, and a failed write, which is rolled back, except a file the rollback "
            "could not restore, which the error names. What this does not reach: a usage error — a missing KIND, "
            "or a KIND other than `allocation` and `trace` — exits 2 before the command runs."
        )
    if name in ANSWERS_A_HOOK_ON_STDOUT:
        parts.append(
            "This command is the model guard an agent host's hook runs: it reads one tool call as JSON on stdin "
            "and answers its host — under Copilot, Codex and Claude Code by printing its decision on stdout, `{}` "
            "to allow, and under Kiro as described below. With the default host (Copilot) it is the hook "
            "the project's hook file under `.github/hooks/` names; of the file and search tools it knows, it "
            "denies a read naming a path in the model root, and a search rooted at the model "
            "root, inside it or above it, its file pattern included; a call among them it cannot judge, such as "
            "arguments it cannot decode, a path it cannot resolve or a pattern that could climb with `..`, is "
            "denied too, as a read or search is when the guard itself fails unexpectedly. A "
            "saved Copilot answer in the system temporary directory stays allowed, and so is every other tool. "
            "The project is the `swcraft.yaml` in the call's working directory, and no other: without one it "
            "allows everything, and with one it cannot read it denies every file read and search but a saved "
            "answer. It changes nothing and exits 0 once it runs, a denial included, under every host but Kiro, "
            "whose denial is exit 2 (below); a project pinned to another "
            "SWCraft release is a warning and the decision stands. With `--host codex` it is the hook "
            "`.codex/hooks.json` names: it denies a patch naming a file in the model root, and a shell command "
            "with a word naming the model root or a path in it, a command it cannot parse, and an edit of a "
            "file the guards rest on — `swcraft.yaml`, `.mcp.json`, `.codex/`, `.claude/`, `.github/hooks/`, "
            "`.kiro/hooks/`, `.kiro/settings/` or `.kiro/agents/`, or of the `.github` or `.kiro` folder holding "
            "some of them; "
            "the project is the nearest folder upward holding both `swcraft.yaml` and "
            "`.codex/hooks.json`, and a deny carries its decision only inside `hookSpecificOutput`, the shape "
            "Codex reads. With `--host claude` it is the hook the project's `.claude/settings.json` names: it "
            "judges a read or search as for Copilot, denies an edit or write of a file in the model root, a shell "
            "command (`Bash` or `Monitor`) as under Codex, every PowerShell call, an edit of those same files, and "
            "a literal path into the review's own folder or naming a folder above it; the project is the "
            "nearest folder upward holding `swcraft.yaml` and `.claude/settings.json` or "
            "`.claude/settings.local.json`, and also the folder `CLAUDE_PROJECT_DIR` names when it holds them, "
            "each judged; a deny has the same `hookSpecificOutput` shape, the one Claude Code reads. With "
            "`--host kiro` it is the hook a file under the project's `.kiro/hooks/` names: it judges Kiro's file "
            "reads and listings as for Copilot, and its searches rooted at their include pattern, so a search "
            "over the whole project is denied; it denies an edit, write or delete of a file in the model root or "
            "of those same files, a shell command as under Codex, every PowerShell or cmd call, a code operation "
            "that changes files, a call whose arguments it does not know, and every tool it does not know, MCP "
            "tools excepted; the project is the nearest folder upward holding `swcraft.yaml` and `.kiro/hooks/`, "
            "from the call's working directory and from the hook's own, each judged, and a payload it cannot read "
            "is denied there. A deny is exit 2 with exactly one line on stderr, the reason, and nothing on "
            "stdout, the form Kiro reads; an allow prints nothing and exits 0. "
            "What this does not reach: a usage error exits 2 before the command runs; with the "
            "default host the terminal, file edits, a tool or path field outside its tables, and the other "
            "limits "
            + render.link("The limits under Copilot", "../model-guard.md#copilot-does-not-reach")
            + " lists; with `--host codex` a command that reaches the model without naming it, and the "
            "other limits of "
            + render.link("The limits under Codex", "../model-guard.md#codex-does-not-reach")
            + "; with `--host kiro` the limits "
            + render.link(
                "What the guard under Kiro does not reach", "../kiro.md#what-the-guard-under-kiro-does-not-reach"
            )
            + " lists. A hook that cannot run at all — no `swcraft` on PATH, or one with no `guard` — "
            "makes Copilot deny every tool call in the project, SWCraft's own tools included, until this "
            "release's `swcraft` is on PATH. Under Codex a hook that cannot run is a failed hook, and Codex runs "
            "the tool anyway; an older `swcraft` invoked with `--host codex` rejects the option with a usage "
            "error, which Codex reads as a block of every shell command and edit. Claude Code does the same: "
            "with no `swcraft` it runs the tool, and an older one invoked with `--host claude` blocks every tool "
            "the hook names. Under Kiro a hook that cannot run, or that runs past its timeout, lets the tool run, "
            "and an older `swcraft` invoked with `--host kiro` exits 2 with its usage error, which Kiro reads as a "
            "block of its file and shell tools."
        )
    if name in WRITES_A_CHANGE_REPORT:
        parts.append(
            "This command writes `change-report.md`, `change-report-pr.md` (the same report without its "
            "pictures, for a pull request), `change-report.json` and the overview pictures under "
            "`<dist_root>/change-report/`, replacing the previous run's, and nothing into the model. It succeeds "
            "on a run that could read the project at both revisions, whatever the change introduced — a model "
            "with errors at either revision is reported, not refused. It fails, and writes nothing, on a project "
            "or a revision it could not read (an unknown revision, one that does not hold the model root, a "
            "model file that does not parse), a name declared twice at either revision, a report directory "
            "outside the project, over the model or on a file, and a project pinned to another SWCraft release. "
            "With `--fail-on` it fails after writing the report when the change introduced a finding of that "
            "level or above. What this does not reach: a usage error exits 2 before the command runs."
        )
    if name in WRITES_A_PASSPORT:
        parts.append(
            "This command writes the model's passport as JSON, to stdout or to the file `--output` names, and "
            "nothing into the model. The passport holds counts, distributions and lengths, and no name, no value "
            "and no text of the model. It succeeds on a run that could read the project and its model, whatever "
            "`check-model` would say of it: a model with errors still gets its passport. It fails, and writes no "
            "passport, on a project or a model file it could not read, a file `--output` names that it could not "
            "write, and a project pinned to another SWCraft release; its messages name no absolute path, and a "
            "model file it could not read is named by its layer and the kind of error alone, never by its name "
            "or its content, because they may travel with the passport. What this does not reach: which file "
            "it was, which `swcraft check-model` says; and a usage error exits 2 before the command runs."
        )
    return " ".join(parts)


def _command_section(name: str, sub: argparse.ArgumentParser, summary: str, prog: str) -> tuple[str, ...]:
    """One command: its heading, what it does, how it behaves, its options and its exclusions."""
    rows = [option_row(name, action) for action in _rendered_actions(sub)]
    blocks: list[str] = [
        render.heading(
            2,
            render.code(f"{prog} {name}"),
            anchor=_anchor(prog, name),
            purpose=f"Running `{prog} {name}`: what it does, each option, and what it does when left out",
        ),
        render.paragraph(summary),
    ]
    if behaviour := _behaviour_sentence(name):
        blocks.append(render.paragraph(behaviour))
    if rows:
        blocks.append(render.table(_TABLE_HEADERS, rows))
    blocks.extend(render.paragraph(sentence) for sentence in _exclusive_sentences(sub))
    return tuple(blocks)


def _anchor(prog: str, name: str) -> str:
    """The explicit anchor of one command's heading, and the target of its link in the command table.

    Derived from the strings the heading is built from, in MkDocs' slug shape (lower case, backticks
    dropped, space a hyphen).
    """
    return f"{prog}-{name}".lower()


def cli_page(*, parser: argparse.ArgumentParser | None = None) -> str:
    """The whole page: the intro, the command table, the top-level options, then one section per command."""
    resolved = _parser(parser)
    prog = resolved.prog
    declared = commands(resolved)
    blocks: list[str] = [
        render.GENERATED_COMMENT,
        render.heading(
            1,
            "The command line",
            purpose=f"Every `{prog}` command and option, and what each option does when left out",
        ),
        render.GENERATED_NOTICE,
        render.paragraph(
            f"Everything SWCraft does to a project is one `{prog}` command. Most of them are pointed at a "
            "project with `--project` and the path to its `swcraft.yaml`; every one of them is listed below "
            "with each option it accepts, what that option takes and what it does when you leave it out."
        ),
        render.paragraph(
            f"`{prog} agent` hands everything written after it to the assistant, whose own commands and options "
            "are declared by another program and are not listed here; the page about the assistant covers them."
        ),
        render.table(
            ("Command", "What it does"),
            [
                (render.link(render.code(f"{prog} {name}"), f"#{_anchor(prog, name)}"), summary)
                for name, _, summary in declared
            ],
        ),
        render.heading(
            2,
            "Options that come before the command",
            anchor="options-that-come-before-the-command",
            purpose=f"The options written between `{prog}` and a command, answered without reading a project",
        ),
        render.paragraph(
            f"These are written between `{prog}` and the command, and are answered without reading a project."
        ),
        render.table(_TABLE_HEADERS, [option_row(prog, action) for action in _rendered_actions(resolved)]),
    ]
    for name, sub, summary in declared:
        blocks.extend(_command_section(name, sub, summary, prog))
    return render.document(*blocks)


def all_artifacts(*, parser: argparse.ArgumentParser | None = None) -> dict[str, str]:
    """The one page this module renders, keyed by its path relative to ``docs/guide/``.

    Takes the *parser* it reads, so a test can render the page from a perturbed copy.
    """
    return {CLI_PATH: cli_page(parser=parser)}
