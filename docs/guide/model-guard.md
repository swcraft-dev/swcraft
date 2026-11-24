<!-- draft: not yet reviewed by the product's author -->

# The model guard's rules { #model-guard }

<!-- purpose: Every rule of the model guard, swcraft guard, for Copilot, Codex, Claude Code and Kiro -->

`swcraft guard` is the model guard: the command an assistant's hook runs before a tool call, so that
the assistant's own file and shell tools stay off your model directory and the model changes only
through the SWCraft tools and their review. You never type it. [The model guard](vscode.md#the-model-guard)
in *In VS Code* and [The model guard under Kiro](kiro.md#the-model-guard-under-kiro) say what it means
for your work; this page states every rule, for every assistant, and what each one does not reach.

The reviewed path is the SWCraft tools and the review tab; the guard keeps an assistant from stepping
round them by accident, not on purpose.

## How the guard is run { #how-the-guard-is-run }

<!-- purpose: Which assistant runs the guard from which hook file, and how the guard answers each one -->

Copilot runs the command a project's `.github/hooks/*.json` names, Codex the one its
`.codex/hooks.json` names, and Claude Code the one its `.claude/settings.json` names, before every
tool call. Each hands the guard the call as one JSON object on its standard input and reads the
answer from its standard output. Kiro runs the one a file under `.kiro/hooks/` names and reads a
denial from the exit code and the error output ([How the guard answers Kiro](#kiro-answers)).

The hook's command line names the assistant, once: `swcraft guard` is Copilot's, the default,
`swcraft guard --host codex` Codex's, `swcraft guard --host claude` Claude Code's and
`swcraft guard --host kiro` Kiro's. The guard never guesses the assistant from the call itself: the
assistants' calls overlap. The assistant decides how an answer is written:

- **Copilot.** A denial carries `permissionDecision` both at the top level and inside
  `hookSpecificOutput`, because Copilot reads either.
- **Codex.** A denial carries it only inside `hookSpecificOutput`, always with a reason that is not
  empty. Any other answer, a denial without a reason included, is a failed hook, after which Codex
  runs the tool.
- **Claude Code.** Written as for Codex ([Under Claude Code](#under-claude-code)).
- Under these three, `{}` allows. **Kiro** reads no JSON: an allow is nothing at all, and a denial
  exit code 2 with one line on the error output.

**The call** names its tool and its arguments under either of two pairs of keys, `tool_name` and
`tool_input` or `toolName` and `toolArgs`, and the guard reads both: a null under one key never hides
the other's value. A call carrying both pairs with different values — two tool names, or arguments
under both keys — is read as every pairing of a tool name with a set of arguments, each judged by the
rules below, and it is denied when any pairing is: which pair the assistant acts on is not known.

## Under Copilot { #under-copilot }

<!-- purpose: What the guard decides for Copilot's file reads and searches, and which project it guards -->

- **A read tool** — `Read`, `read`, `view`, `NotebookRead` and `LS` — with its path under `path`,
  `file_path` or `notebook_path`: every path written under every one of these keys is judged, never
  only the first — a non-empty text, or each non-empty text of a list. A null names none; any other
  value, or a list member that is not text, cannot be judged and is denied. A read is allowed as a
  saved Copilot answer — a file directly in the system's temporary folder whose name ends in
  `-copilot-tool-output-<hex digits>.txt` — only when every path it carries is one; it is denied when
  any path is the model directory or inside it; anything else is allowed.
- **A search tool** — `Grep`, `Glob`, `grep`, `glob`, `rg` and `search` — with its folders under
  every one of `path` and `paths`, a text or a list, read as a read tool's path is, else, with no
  folder or an empty list, the call's `cwd`: denied when a folder cannot be judged, or is the model
  directory, inside it or a folder above it — searching the project folder reaches the model.
- **A search's file pattern** — the `pattern` and `glob` of `Glob`, `glob` and `search`, and the
  `glob` of `Grep`, `grep` and `rg` — is judged the same way by its literal leading folders, taken
  from each folder of the search, so an absolute pattern — rooted at `/` or at a drive letter, whose
  drive is never read as part of the pattern — names its own folder. Its brace groups (`{a,b}`) are
  expanded first. A part after those literal folders that could match `..` cannot be judged against
  any folder, so the pattern is denied; which parts could is decided by their shape, never by a list
  of spellings. A pattern holding a backslash — an escape on macOS and Linux, a separator on
  Windows — or longer than 4 096 characters is denied without being judged.
- **A read or search whose arguments arrive but cannot be decoded** — a `toolArgs` text that is not a
  JSON object — names no path that can be judged, so in a guarded project it is denied.
- **Every other tool** is allowed: the SWCraft tools, `Edit` and `Write`, the terminal. A tool's name
  is matched against both lists whatever its case. Under Codex a patch or a shell tool is judged as
  well ([Under Codex](#under-codex)).

**The project** is `swcraft.yaml` in the call's `cwd`, else in the folder the guard runs in, and
nothing else; under Codex, Claude Code and Kiro their own rules below apply. The guard never looks
for it in a folder above, and never follows `SWCRAFT_MANIFEST`: either could guard another project.
Where nothing is at that path, everything is allowed: it is not a SWCraft project. Anything else
there is a manifest, and one that cannot be read or is not valid — a folder, a broken link, an entry
whose status cannot be read included — fails closed: every read and search is denied, a saved answer
excepted.

### The limits under Copilot { #copilot-does-not-reach }

<!-- purpose: The limits of the model guard under Copilot, and what it denies more than it needs to -->

- **The terminal**: what a command reads cannot be judged from its text. And `Edit` or `Write` on
  the model.
- **A key or a tool outside the lists above.** A path under another key, or a tool outside the two
  lists, is not judged: the lists are the whole of it, and a new tool or key has to be added there.
- **Tool names other than the ones seen.** In Agent mode and under the SWCraft Architect agent a read
  arrives as `Read` with `{path, view_range}`, a search as `Grep` with `{pattern, paths}`, and
  SWCraft's own tools as `SWCraft-<tool>`; both sets of names are listed. The names a custom agent's
  profile gives, `read` and `search`, are judged although no hook call naming them was seen, and
  their argument keys are not known, so the lists' keys are assumed: a `search`'s `pattern` is
  judged as a file pattern, which can deny a regular expression looking outside the model. The other
  custom-agent names — `execute`, `shell`, `edit`, `web`, `todo` and `agent` — read no file the guard
  can judge and are allowed.
- **A project other than `<cwd>/swcraft.yaml`.** A workspace whose root is a folder above the project
  guards nothing.
- **`~` and a `file:` URL.** A path written that way is taken literally, never expanded, so an
  assistant that expands either is not reached.
- **A `Grep` `pattern`**, which is a regular expression and never a path.
- **A file pattern past its first wildcard part below the search's folder.** The folders after it
  are judged only for whether they could climb with `..`. That rule is cautious, so it also denies
  patterns no engine would climb with: `.*` and `..*` (which some shells do match to `..`), any part
  made only of dots and pattern characters such as `[e]` or `?`, an excluding pattern such as
  `!**/x`, a part that is entirely a group such as `(draft)`, every pattern holding a backslash (one
  with Windows separators included), a pattern longer than 4 096 characters and one with more than
  256 brace expansions. It reads a pattern as an assistant's engine might, and runs none of them,
  so an engine whose pattern syntax goes beyond the characters `\`, `*`, `?`, `[`, `]`, `{`, `}`,
  `(`, `)`, `@`, `+`, `!` and `|` is not reached.
- **Network and device paths, and drives elsewhere.** A path or pattern written as a network or
  device path is denied even when it names a place outside the model — a project kept on a network
  share and opened by its network path has every file read and search denied — and so is one
  starting with a drive letter, or shaped `a:b`, on macOS or Linux, although such a system would read
  it as a relative name. On Windows a path rooted without a drive (`/x`) is resolved on the drive of
  the call's folder, whatever drive the assistant's process is on.
- **A project whose hook file is missing**: nothing runs and nothing is guarded.
- **A `swcraft` Copilot cannot find, or one too old to have `guard`**: a hook that cannot run, which
  makes Copilot deny every tool call, SWCraft's own included (*hook errored* for a `swcraft` missing
  from the path, *hook exited with code 2* for one with no `guard`).
- **A change of the name or the folder of Copilot's saved answers**, after which the assistant loses
  the saved answer while the model stays guarded.

## Under every assistant { #under-every-assistant }

<!-- purpose: The guard's rules that hold for every assistant: paths, the model's names, failures -->

### How a path is read { #how-a-path-is-read }

<!-- purpose: How the guard resolves and compares a path, and which spellings it cannot judge -->

A path is resolved and compared by the manifest's own rules of resolution and containment, with the
system's own separators and whatever its case, always — the direction that fails closed. The
saved-answer test and a build's `--project` naming the guarded manifest are exact comparisons: each
is an allowance, and ignoring case would widen it.

Every written path, search folder and pattern expansion is first read by the rules of both Windows
and macOS or Linux, because how a call spells a path does not depend on where the guard runs. One
that opens with two separators (a network path, a `\\?\` or `\\.\` device path, two slashes on macOS
or Linux), one relative to a drive (`C:x`), a drive other than a letter, or one starting with a drive
letter on a system whose paths have no drives cannot be judged, so in a guarded project it is denied.
A drive-letter path on Windows and every path without a drive resolve natively.

### The model's own names { #the-models-own-names }

<!-- purpose: Why a path that names the model directory or ends like a model file is denied anywhere -->

Under every assistant, each path the guard judges under that assistant — a read or search path and
each brace expansion of a pattern, under Codex, Claude Code and Kiro also an edit's or a write's
path, and the `cwd` of Kiro's `execute_bash`; Copilot's `Edit` and `Write` are not judged — each
reading of it, the one written first, that holds the model directory's path anywhere, or ends like an existing entry of the model, is denied
whatever the assistant then does with it. While the model directory cannot be listed whole, every
such path of two or more parts is denied. [The model guard](vscode.md#the-model-guard) gives the
examples, the limits of the listing and what these two rules do not reach. Shell words and Codex's
`workdir` are not judged by them; the shell rules below read them as the shell does.

### Reasons name no location { #reasons-name-no-location }

<!-- purpose: How a denial names the model directory without naming where the project is stored -->

No reason names where the project is stored: the model directory is named relative to the project
folder, with forward slashes, or as "the model directory" when it has no such form.

### An unexpected failure { #an-unexpected-failure }

<!-- purpose: What the guard answers when it fails while judging a call -->

The guard answers an unexpected failure of its own too, never with an error trace, and so it does
for a call that is JSON but cannot be read (nested past the interpreter's recursion limit, or an
integer past its digit limit). In a folder holding a `swcraft.yaml` — the call's `cwd`, else the
folder the guard runs in — a read or search tool, or a call whose tool cannot be named, is denied;
anything else is allowed. The error output says which, and names the kind of error but never its
message, which can carry a path. Only a call that is not JSON at all, or not a JSON object, is
allowed as naming no tool. Codex, Claude Code and Kiro add their own rules below.

## Under Codex { #under-codex }

<!-- purpose: What the guard decides for Codex's edits and shell commands, rule by rule -->

Under `--host codex` the guard judges two more kinds of tool call, both carrying their text under
`tool_input.command`: an edit, which Codex writes as a patch, and a shell command. Every other tool
is judged as under Copilot.

### Edits { #codex-edits }

<!-- purpose: How the guard judges a Codex edit, a patch, by the paths its header lines name -->

An edit is `apply_patch`, or `Edit` or `Write`, the names Codex's hook matcher gives it, matched
whatever their case. Every path a header line names — `*** Add File:`, `*** Update File:`,
`*** Delete File:`, `*** Move to:` — is resolved against the call's base folder, as a shell
command's words are: `tool_input.workdir` resolved against `cwd`, else `cwd`, a `workdir` that is not
a non-empty text or cannot be resolved denying the edit. The edit is denied when any path is the
model directory or inside it, is one of [the guard's own files](#codex-guard-files), or cannot be
judged. A move out of the model is denied by its `Update File:` header.

A `command` that is not text, a text whose first non-blank line is not `*** Begin Patch`, or a patch
naming no path is denied: what it touches cannot be said. The text is split into lines as Codex's own
reader splits it — a line ends at a line feed alone, and a header's leading blanks are the ones Codex
trims, Unicode's white space. Past 1 024 header lines, or 8 192 path parts resolved, the edit is
denied as too much to judge in time.

### How a shell command is read { #codex-reading-a-command }

<!-- purpose: How the guard turns a Codex shell command into the words it judges -->

A shell command is `Bash`. Its text — a list of texts is joined with spaces, any other value is
denied — first has bash's `$'...'` quoting read: decoded to bytes as bash decodes it — an octal escape
is its value's low eight bits, so `\555` is `m`, and `\xHH` is one byte — then read as UTF-8, a
body that does not decode being denied without being judged; an escape decoding to NUL ends its value,
the rest up to the closing quote dropped, as bash drops it. Its `$"..."` quoting is read too, and
every backslash-newline outside single quotes is removed, as a line continuation. Then the text is
split into words as Python's `shlex` splits in POSIX mode, with no comment character (a `#` inside a
word cuts nothing off), and the operators, a newline among them, as tokens of their own. A text that
cannot be split so — an unbalanced quote, a heredoc whose text holds one — is **denied, whatever it
names**: the guard fails closed and Codex words the command another way.

Every other token is a word, and so is:

- each part of a word around `=`, `,`, `:` and a backtick (a command substitution's words);
- each one's value after a leading `@`;
- of each one opening with a single `-`, every ending after its first two characters — a cluster of
  short options can end in an option whose value is glued to it (`-tmodel`, `sort -uomodel/x`,
  `tar -cvfmodel/x`).

The same parts are judged again with each `${name<op>word}` (`:-`, `:=`, `-`, `=`, `+`, `?`, with or
without the colon) read as its word. A `${` that the splitting broke apart is joined again and its
parts judged as words, a substituted command as a command of its own (`cat ${x:-$(rm swcraft.yaml)}`);
one still open at the end of the text, or a broken-apart run holding a quote, is denied without being
judged. The body of every backtick substitution is judged as a command too, before the text's own
words (the splitting never splits at a backtick).

A `sed` command's scripts are read for the files they write — from every word named `sed`, wherever
it stands (`env sed`, `xargs sed`, `find -exec sed`, `sudo sed`), each later one starting its options
afresh — after a `w` or `W` command and an `s` command's `w` flag, each to the end of its line, with a
bracket expression ended as both GNU and BSD sed end it. Each such file is judged as the one literal
path sed opens, never expanded or split (`sed -n wswcraft.yaml`), and a word after the command's name
is never read as an assignment (`sed -n 's=a=b=wswcraft.yaml'`). A `sed` word is not split again as
shell text unless some word of the command can hold an `e` command or an `s///e` flag, the only ways
sed hands a shell anything; and a word holding a backtick, `$(`, `<(` or `>(` is always split again:
the shell runs it whatever command the word is given to.

A text that defines a function (`name()`, `function name`) or runs `alias`, `unalias`, `enable`,
`hash`, `source`, `.` or `eval` makes none of its commands a reading command
([The guard's own files](#codex-guard-files)), since a command name in it may not run the command it
names.

A word holding blanks, a quote or a shell operator character after unquoting — a `bash -lc` script,
`bash -c 'echo>x'`, a `python -c` program — is split again, to a depth of four, past which it is
denied; a nested word that cannot be split (`-m "don't"`) is split at every character no judged path
holds, each part judged as a word.

### Which words are judged { #codex-which-words }

<!-- purpose: How the guard resolves every word of a Codex shell command, and which words it denies -->

**Every** word is resolved against the call's base folder — `tool_input.workdir` resolved against
`cwd`, else `cwd`; a `workdir` that is not a non-empty text, or cannot be resolved, is denied — and
the command is denied when any word is the model directory or inside it, or when the `workdir` is.
Nothing decides which words are paths.

A word, or a file a `sed` script writes, that cannot be resolved — one holding a NUL, and on Windows
one relative to a drive, `C:x` — is **denied without being judged**: a word that cannot be resolved
never counts as one naming nothing. On macOS and Linux, where bash knows no drives, a word is resolved
natively (`C:x` is a relative name). A word that opens with two separators and names something after
them (`//server/share/...`) is denied on Windows, where it can be a network path standing for any
folder; on macOS and Linux a run of leading slashes is read as one, as the system reads it, and the
word judged as that path (`grep '//TODO'` is allowed).

### Wildcards and brace groups { #codex-wildcards }

<!-- purpose: How the guard judges a Codex shell word holding a wildcard or a brace group -->

A word holding `*`, `?`, `[` or a brace group is brace-expanded, a sequence (`{a..e}`, `{01..10..2}`)
to the terms bash makes — in the shell rules alone. A word part past 256 expansions, 64 brace groups
or 4 096 characters is denied as too much to judge, whatever it names. The word part is also judged
as the one literal path it spells: a quoted pattern, or one matching nothing, which bash passes on as
written, opens a folder named `mod[el]`.

Of each expansion, the literal leading folders are resolved, and a part followed by `..` after
them collapses with it. A part that opens with a literal `.` and could match `..` is read as `..`,
as `.` and as the pattern it is, each reading judged (bash lets no other part match `.` or `..`). The rest is
matched against the model directory's path below those folders one part at a time, whatever the
case, with bracket expressions read as bash reads them — a leading `^` negates, and one holding
`[:class:]`, `[.x.]` or `[=x=]` matches any one character — `**` matching any number of parts, and a
name opening with `.` matched only by a part opening with a literal `.`, as bash matches without
`dotglob` (`*` names no `.codex`, `.c*` does). The command is denied when any reading could reach the
model directory or inside it (`ls */swc`, `rm -rf ?*`).

A text holding a bash extglob group — `!(...)`, `@(...)`, `+(...)`, `*(...)`, `?(...)` — which the
splitting would break at its `(`, is judged first with each group read as a part matching any name,
then as written, so `cat !(notmodel)/swc/x` is denied where bash runs with `extglob` on.

A pattern whose literal leading folders are an existing folder inside the project has its **real
matches** read too, part by part, `**` through every name below but through no linked folder. Each
match, the folders on the way included, is resolved and judged against the model directory and the
guard's own files, so a wildcard matching a link (`short*` matching a link to the model) is judged as
what the link points to. Past 1 024 entries read for one pattern, the command is denied as too much
to judge.

The read and search tools' denials of a backslash or of a climbing part are not applied to shell
words; their length and expansion limits are. On Windows a backslash is also read as a separator:
the text is judged a second time with each one a `/`.

### How long the guard judges { #codex-limits }

<!-- purpose: The limits that keep the guard's answer to Codex inside Codex's time limit -->

The work is bounded, so the hook answers inside Codex's time limit. A text longer than 65 536
characters, or one whose words, expansions and readings pass 8 192 path readings, is denied without
being judged. What no count sees — resolving a chain of links reads the status of every part of
every target — is bounded twice: a name past the system's own limit of links, which no command can
open, is denied without being judged after one status read; and each command or edit has three
seconds, checked with every reading and before every resolution and folder listing, past which it is
denied as too much to judge.

### The guard's own files { #codex-guard-files }

<!-- purpose: Which files and folders Codex may never change, and which reading commands may name them -->

The guard's own files are the project's `swcraft.yaml`, the `.codex` folder with
`.codex/hooks.json` in it, whole, the other assistants' guard files — `.claude`, `.mcp.json` and
`.github/hooks` — and Kiro's `.kiro/hooks`, `.kiro/settings` and `.kiro/agents`. The assistant never
writes them, nor a folder strictly between the project folder and one of them, `.github` or `.kiro`:
`rm -rf .github` removes Copilot's hook. A word, an edit's path or a wildcard match naming that folder
itself counts; a path inside it that is none of the guard's files does not (`rm -rf .github/workflows`).

A path names one of them when it does either resolved or as written — joined to the resolved base
folder, with `..` collapsed by name and no link followed. So a `.codex/hooks.json` that is a link out
of `.codex` is still the hook's file, and the target of every link directly inside one of these
folders is one of the guard's files too: the assistant runs what it holds. Past 1 024 entries in one
of these folders, or with a link there that cannot be resolved, every edit and shell command is
denied.

An edit naming one of them is denied, and so is a shell command with a word that could name one,
unless that word stands in a **reading command** — a command that runs nothing and writes nothing
the assistant names:

- `cat`, `head`, `tail`, `wc`, `less`, `more`, `file`, `stat`, `ls` and `grep`, which write nothing;
  and `swcraft` with a command that reads or checks and writes nothing, and no output option —
  `check-model`, never `validate` or `generate`, which write the build outputs the manifest names,
  and a manifest can name `.codex/hooks.json` as one;
- named by the bare word: a command word holding a path separator (`notes/cat`, `./ls`) may be any
  program, so it is none;
- found after unwrapping `command`, `exec`, `builtin`, `env`, `nice`, `nohup`, `stdbuf` and `timeout`,
  each by its bare name too. One of these whose arguments cannot be read, or `env` setting a
  variable, makes no reading command. `xargs` and `find` are none, and a text holding `xargs`
  anywhere has no reading command at all: it hands what a reading command prints on as names
  (`ls swcraft.yaml | xargs rm`);
- with no variable assignment before it and no command or process substitution in any of its words —
  a backtick, `$(`, `<(`, `>(`: the substituted command runs, so a `cat` of a backtick substitution
  that runs `rm swcraft.yaml` is no read;
- in a text that defines no function and runs none of `alias`, `unalias`, `enable`, `hash`,
  `source`, `.` and `eval` — after `cat() { rm "$@"; }` the name `cat` runs `rm`;
- and no redirection's target.

`sed`, `git`, `rg` and every other command are no reading command: each has an option that writes a
file or runs a program (`sed w`, `git diff --output`, `GIT_EXTERNAL_DIFF`, `rg --pre`). A guard the
guarded assistant can switch off is no guard, so Codex cannot raise the project's pinned release or
edit the manifest for you, nor `git diff` or `sed` them — it reads them with `cat`.

### A folder holding the model { #codex-model-folder }

<!-- purpose: Why removing or moving the folder that holds the model directory is denied -->

A folder strictly between the project folder and the model directory — `arch` for a model directory
`arch/model`, found from the model directory both resolved and as written — is judged as a folder
holding one of the guard's files is. An edit's path or a shell word naming it outside a reading
command (`rm -rf arch`, `mv arch x`, `rm -rf ar*`) is denied, the reason saying the folder holds the
model; a reading command may name it (`ls arch`), and a path inside it beside the model is judged by
the model rule (`rm -rf arch/other` is allowed). The project folder itself and every folder above it
are **not judged** as one: a shell word naming the project (`rm -rf .`, the project's full path) is
allowed, so that `git add .` and `pytest .` run.

### A build of the project's manifest { #codex-builds }

<!-- purpose: When Codex may run swcraft validate or generate on the project, and when it is denied -->

`swcraft validate`, or `swcraft generate arxml`, `diagrams` or `needs`, is a reading command when
every `--project` it carries is the guarded `swcraft.yaml` — `--project=`, `./swcraft.yaml`,
`../swcraft.yaml` from a subfolder, or `SWCRAFT_MANIFEST` set to it first — when it carries no other
option than `--warnings-as-errors`, `--component` with a component name and `--arxml-version` with a
release number, and no other word but redirections, and when every output that manifest names keeps
off the guard's own files and the model. It is denied, the reason naming the output, when one does
not. The outputs are `outputs.arxml` and the previous ARXML kept beside it
(`<name>_old.<extension>`), `outputs.needs`, `dist_root` (the component exports' default folder is
below it) and `views_root`, each resolved as SWCraft resolves it and as written, an overlap in either
direction counting. The gate must be runnable by the assistant, and the outputs are the manifest's,
which the assistant never writes.

Any other option — `-o`, `--output`, `--output-dir`, `--all-components`, or an abbreviation the
command line would expand — makes it no build of the manifest's outputs, so a command naming
`swcraft.yaml` stays denied. An unreadable manifest is denied as below.

### An unreadable manifest, and the project { #codex-project }

<!-- purpose: What Codex may do while swcraft.yaml is broken, and which folder the guard takes as the project -->

**An unreadable manifest** denies every edit and shell command, an edit or a read of `swcraft.yaml`
itself included, and the reason asks you to fix `swcraft.yaml` by hand in the editor. The assistant
never repairs it: allowing a repair is a way to write one of the guard's files — a twin differing
only in case, a manifest linked onto a hook file, a linked guard folder.

**The project** is the nearest folder, from `cwd` up to the top of the disk, that holds both
`swcraft.yaml` and `.codex/hooks.json` — the folder whose Codex settings can have run the hook; a
folder holding only a manifest is passed over. Paths still resolve against `cwd`, and reasons name the
model directory relative to the project found.

### The limits under Codex { #codex-does-not-reach }

<!-- purpose: The limits of the model guard under Codex -->

- **A command that reads or writes the model without naming it**: a program or script that opens
  model files (`python tool.py`); a variable or a command substitution (`$DIR/x`, `$(...)`, a
  backtick's output joined to a word, `${x#p}`, `${x/a/b}` — only a `${...}` operator's word and a
  backtick's own words are read); a tool run over the whole project (`rg pattern`, `find .`,
  `git add -A`, `rm -rf ..`); a path put together while the command runs.
- **What a `sed -f` script file writes**, whose text is never read, and a `sed` script syntax that
  neither GNU nor BSD sed reads as the guard does.
- **A path reached through an earlier `cd` in the same command** (`cd notes && cat ../model/x`),
  because words resolve against the call's base folder.
- **`~` and `$HOME`**, never expanded.
- **A command that rewrites the guard's own files without naming them**: `swcraft init` over the
  project, a rename or removal of the project folder or a folder above it (`rm -rf .`,
  `rm -rf ..`) — a folder strictly between it and one of the guard's files, or between it and the
  model directory, is judged as that file or the model is — and a `swcraft` command run on another
  manifest whose outputs point into `.codex/`.
- **A `workdir` Codex does not hand the hook.** Whether a call's `workdir` arrives in `tool_input` is
  not recorded, so a command run in the model directory and naming only paths below it is reached
  only when it does.
- **A project that is no git repository.** Codex loads no project settings there and so runs no
  hook, even from the project folder itself: Codex finds the project's settings from the root its
  `project_root_markers` mark, `.git` by default.
- **A hook declared anywhere but the project's `.codex/hooks.json`** — a `[hooks]` table in
  `.codex/config.toml`, your own `$HOME/.codex/hooks.json` — which runs a guard that finds no
  project and allows everything.
- **PowerShell's own syntax on Windows** (a backtick escape, its quoting), of which only the
  backslash separator is read.
- **A function or alias the shell already holds when the command starts** — from a profile
  (`bash -lc` reads the login one), `BASH_ENV` or an exported function — which no text of the
  command shows, so a reading command's name may run another command.
- **A shell set to match hidden names** (`shopt -s dotglob`, in the command or a profile), whose `*`
  then names `.codex`.
- **A link a wildcard reaches below a literal folder outside the project**, whose matches are never
  read, and a link deeper than directly inside the guard's folders.
- **A wrapper other than the eight above**, which only makes no reading command.
- **A command other than `xargs` that hands a reading command's output on as names** — `parallel`, a
  `while read` loop over a variable, `$X` set to `xargs` — which no word of the command shows.
- **One resolution already begun when the three seconds pass**, which finishes first, bounded by the
  system's limit of links rather than by the three seconds.
- **An edit wrapped in a heredoc**, which Codex accepts and the guard denies — a needless denial, not
  a miss.
- **Another server's tool that writes files**, whose arguments the guard does not know.
- **Input typed into a command that already passed the hook** (`write_stdin`).
- **A session whose project `.codex/` settings or hook are not trusted**, which runs no hook at all.
- **An older `swcraft` without `--host`**, whose usage error (exit code 2) Codex reads as a block of
  every shell command and edit.
- **No `swcraft` on the path**: a hook that cannot start, after which Codex runs the tool. The path
  is the one your login shell sets, since Codex starts the hook through it, not the one of the
  terminal Codex was started from — this is inferred, not measured.

### What it denies more than it needs to under Codex { #codex-denies-more }

<!-- purpose: The harmless Codex commands the guard denies, and why -->

The guard errs towards denial. These are denied:

- a command other than a reading one naming a folder that holds one of the guard's files or the
  model, though it writes nothing — `git add .github`, `git diff .kiro`, `find .github -name x`,
  `cd .github && ls`, `chmod -R 755 .kiro`, `git add arch` for a model directory `arch/model` — the
  cost of the folder rule;
- a word that equals the model directory's name used as text (`echo model`), a wildcard that could
  match it (`ls *` run in the folder that directly holds the model directory), and a `#` comment
  naming it;
- a command that cannot be split into words or is past the limits;
- a `${` left open to the end of the text, a quoted one included (`grep '${' src`), and a
  broken-apart `${` run that holds a quote anywhere, even one naming nothing (`cat ${x:-a "b c"}`);
- one of the guard's files named by a reading command in a text holding a word followed by `()`, a
  quoted one included (`grep '()' swcraft.yaml`), or a renaming command anywhere;
- a word holding a group shaped like an extglob group, read as one whether `extglob` is on or not and
  quoted or not (`rm -rf '!(x)'` run in the folder holding the model directory);
- a command naming `swcraft.yaml` other than in a read, and a command other than a reading one with a
  wildcard that could match `swcraft.yaml` or the model directory's name in the folder holding them
  (`git add *`, `du -sh *`, `find . -name '*.yaml'` at the project root);
- a pattern over a folder of more than 1 024 entries;
- the words after a `sed` that is only an argument (`grep sed notes`), read as scripts;
- a reading command named by a path (`cat` by its absolute path);
- one of the guard's files named in a text holding `xargs` anywhere, even as text
  (`grep xargs swcraft.yaml`);
- a build of the guarded manifest by a `swcraft` named by a path, or with an option off the list
  above;
- a name past the system's limit of links, and a long word opening with one `-` whose endings pass
  the limits;
- on Windows, a word opening with one letter and a colon not followed by a separator
  (`sed 's:a:b:'`, read there as a place on a drive).

## Under Claude Code { #under-claude-code }

<!-- purpose: What the guard decides for Claude Code's reads, searches, writes and shell commands -->

- **The projects.** The nearest folder, from `cwd` upwards, holding `swcraft.yaml` and either
  `.claude/settings.json` or `.claude/settings.local.json` — Codex's rule with Claude Code's two
  project settings files — **and** the folder `CLAUDE_PROJECT_DIR` names when it holds them itself.
  The call is judged against each, and denied when either denies. The call's `cwd` follows a `cd`:
  out of the project (a folder added to the session) the search upwards finds nothing, and into a
  nested project it finds the inner one; the session's own project stays guarded in both. The two
  are one project only when they are the same folder on disk, never because their names match
  whatever the case.
- **A read or search tool** is judged by Copilot's rules, unchanged (`Grep` and `Glob` included), and
  then against the review's folder below.
- **A write tool** — `Edit`, `Write`, `MultiEdit` and `NotebookEdit`: every path under `file_path`
  and `notebook_path`, read as a read tool's paths are and resolved against `cwd`, is judged. The
  call is denied when a path is in the model directory, is one of the guard's own files or a folder
  holding one (resolved or as written), is a folder holding the model directory, is in the review's
  folder or in your SWCraft folder, or cannot be judged, or when the call names no path.
- **A leading `~`** is judged both ways, because whether Claude Code expands it is not checked: a
  read, search or write tool's path or pattern opening with `~` is judged as written and as one in
  your home folder, a search's folder and pattern in every pairing, and a denial from any reading
  wins. One that cannot be read so — another user's home (`~name`), a home the guard cannot
  determine, or a home whose path holds a pattern character — is denied. Shell words are unchanged.
- **A shell tool** — `Bash`, and `Monitor`, which runs its `command` in Bash's shell — is judged by
  Codex's shell rules, unchanged, on `tool_input.command`, resolved against `cwd` (Claude Code sends
  no `workdir`). Where shell words have drives — Windows, where Claude Code's `Bash` is Git Bash —
  each brace expansion of each word part, and each literal path, of the form `/x/...` is judged a
  second time as a path on drive `x`, after bash's brace expansion, so `{/c/x,}` and `/{c,d}/x` are
  read as Git Bash reads them; the command is denied when either reading is.
- **`PowerShell`** is denied in a guarded project whatever it runs: the guard reads bash, not
  PowerShell.
- **The guard's own files** are Codex's, and the three assistants that write share one set:
  `swcraft.yaml`, the `.codex` and `.claude` folders, `.mcp.json`, `.github/hooks` and Kiro's
  `.kiro/hooks`, `.kiro/settings` and `.kiro/agents`, with the folders holding them (`.github`,
  `.kiro`) and the reading-command exception. The cost: an assistant's edit of `.claude/commands/`,
  a skill, `.mcp.json`, `.kiro/settings/` (another server) or `.kiro/agents/` is denied.
- **The review's folder**, `reviews` in your SWCraft folder: a read path, a write path or a literal
  shell word in it, a literal shell word naming a folder above it (your home folder written out in
  full) but not the top of the disk, a search folder overlapping it, and a shell command whose base
  folder lies inside it are denied. This rule is not part of Codex's, whose sandbox confines it.
- **Your SWCraft folder** — `$HOME/.swcraft`, or `SWCraft` in your local application data folder on
  Windows — which holds the `swcraft` the hook runs: a write path into it, and a shell word outside a
  reading command reaching it or a literal one naming a folder above it
  (`rm -rf <your home>/.swcraft/current`, `cp x <your home>/.swcraft/current/bin/swcraft`) but not the
  top of the disk, are denied, with a reason naming no location. A reading command below it
  (`cat <your home>/.swcraft/current/RUNTIME.json`) stays allowed, apart from the review's folder's
  own rule. A SWCraft folder that cannot be determined denies every judged call, as for the review's
  folder. The cost: no assistant updates or reinstalls SWCraft; you do. This rule is not part of
  Codex's, whose sandbox confines writes.
- **The answer** is written as Codex's: `hookSpecificOutput` alone with a non-empty reason, `{}` to
  allow, exit code 0 always. An unexpected failure denies a judged tool, or one that cannot be named,
  in a project.

### The limits under Claude Code { #claude-does-not-reach }

<!-- purpose: The limits of the model guard under Claude Code -->

- **Codex's list, as it applies here**: a command reaching the model without naming it, a script, a
  variable, `~` and `$HOME` in a shell word (a file tool's leading `~` is judged both ways, above), a
  profile's function, a `sed -f` script, a link from outside, and on Windows a Git Bash mount other
  than a drive letter, such as its `usr` or `tmp` mount, judged natively only.
- **Another server's file tools.**
- **A tool Claude Code adds later, and tools of release 2.1.319 that the hook's matcher does not
  name**: `REPL`, which runs JavaScript that can call tools, `Workflow` and `CronCreate`, whose
  subagents' and scheduled prompts' calls are judged only as far as Claude Code hands them to the
  hook, `AppifactRepl` (JavaScript against a hosted artifact), and `RemoteTrigger`. A `Monitor`
  watching a web socket carries no `command` and is denied, a needless denial.
- **A folder Claude Code was not told to trust** (it then runs no project hook), and hooks switched
  off through `/hooks`.
- **A subagent's tool calls** only as far as Claude Code hands them to the hook (release 2.1.319
  handed one, with `agent_id` and `agent_type`).
- **`disableAllHooks`** in a settings file outside the project.
- **A session started outside the project, or in a subfolder of it** — Claude Code then reads none
  of the project's `.claude/settings.json`, so no hook runs — and a call whose `cwd` is outside the
  project when `CLAUDE_PROJECT_DIR` is not set or names no project, where the search upwards finds
  nothing.
- **A hook declared only in your own settings**, which finds no project and allows everything.
- **An older `swcraft` on the hook's path** — VS Code's or the login shell's, not told apart — whose
  usage error (exit code 2) blocks every matched tool; and no `swcraft` at all, after which the tool
  runs.
- **The review's folder reached through `~`, `$HOME` or any variable in a shell word**, a script, a
  path put together while a command runs, the top of the disk, or a command run from a folder above
  it naming no path (`grep -r x` from your home folder): `cat $HOME/.swcraft/reviews/*.pending.json`
  is allowed, so an assistant can approve its own change that way. The confinement the review rests
  on is Claude Code's own, and this denial stops an accidental or literal-path reach only.
- **Your SWCraft folder reached the same ways** (`rm -rf $HOME/.swcraft/current` is allowed), and a
  guard program installed outside it (a Python environment, a system folder), which nothing protects.

## Under Kiro { #under-kiro }

<!-- purpose: What the guard decides for Kiro's tools: the projects, malformed calls, each judged tool -->

- **The projects.** The nearest folder holding `swcraft.yaml` and an entry at `.kiro/hooks` (the
  folder, since you may rename the hook file), searched upwards from the call's `cwd` **and** from
  the folder the hook runs in, which Kiro sets to the project root (measured). The call is judged
  against each, so a `cwd` moved out of the project or into a nested one still judges the original.
  No Kiro variable names the project in the hook's environment, so there is no third starting point.
  A relative path, a search without a folder and a shell command whose `cwd` is null are judged
  twice, against the call's `cwd` **and** against each project folder found, a denial from either
  winning: a `cwd` outside the project or in a subfolder never hides `model/x.yaml`.
- **Malformed input fails closed.** A call that is not a UTF-8 JSON object, a tool name missing or not
  text, arguments that are not an object, and a failure while finding the projects are denied
  whenever a starting point is, or cannot be ruled out to be, in a guarded project. The arguments
  (`tool_input`, `toolArgs`) must be an object as sent — a JSON text is never decoded under Kiro, as
  it is under the other assistants — so a text, list, number or null there denies every call in a
  guarded project, an allowed tool's too. A call naming no `cwd` has the folder the hook runs in as
  its starting point, so with that folder in no project it is allowed.
- **Every other tool is denied unless allowed by name**: `tool_load`, `get_process_output`,
  `list_processes`, `web_search`, `remote_web_search`, `introspect`, `report_progress`,
  `update_session_information`, `switch_to_execution`, `user_input` and `tool_search`, matched
  exactly, and every server tool, `mcp_<server>_<tool>`. Kiro updates itself, and a renamed or new
  file tool must not pass unjudged. An unexpected failure follows the same rule.
- **The guard's own files** (Kiro's three folders among them), the folders holding them
  (`delete_file .kiro`, `rm -rf .kiro`, `mv .github x`) and the folders holding the model directory
  (`delete_file arch`, `rm -rf arch`), an unreadable manifest, the review's folder and your SWCraft
  folder are Claude Code's rules, unchanged.

### Kiro's judged tools { #kiro-judged-tools }

<!-- purpose: How the guard judges each of Kiro's file, search, code, write, shell and fetch tools -->

Each judged tool has a closed list of argument keys, and every key a call carries must be on its
tool's list, whatever its value. A path or pattern key holds a text or null, a list key only where
listed (`invoke_sub_agent`'s `contextFiles`). Kiro's tool names are matched whatever their case.

| Tool | Its keys |
|---|---|
| `read_file` | `path`; `offset`, `limit` |
| `list_directory` | `path`; `explanation`, `depth` |
| `grep_search` | the pattern `includePattern`; `query`, `caseSensitive`, `excludePattern`, `explanation` |
| `file_search` | `explanation`, `query`, `excludePattern`, `includeIgnoredFiles` |
| `code` | `path`, `file_path`; `operation` and the operation's own keys |
| `fs_write`, `fs_append` | `path`; `text` |
| `str_replace` | `path`; `oldStr`, `newStr`, `replace_all` |
| `delete_file` | `targetFile`; `explanation` |
| `execute_bash` | `command`, `description`, `cwd`, `run_in_background`, `timeout` |
| `invoke_sub_agent` | `name`, `prompt`, `explanation`, `preset`, `specTask`; `contextFiles`, each `{path, startLine, endLine}` |
| `web_fetch` | `url`, `mode`, `searchPhrase` |

The `code` tool's own keys are `symbol_name`, `symbols`, `symbol_type`, `limit`, `exact_match`,
`language`, `include_source`, `top_level_only`, `pattern`, `replacement`, `row`, `column`,
`new_name`, `trigger_character`, `tab_size` and `insert_spaces`.

- **Reads and searches** are judged by Copilot's rules, given the kind and the keys. `read_file` and
  `list_directory` one level deep (`depth` absent, null or `1`) are reads, a read naming no path
  denied. A deeper listing, `grep_search`, `file_search` and `code` are searches.
- **An include pattern** holding a blank or a `,` outside a brace group — a list of patterns, such as
  `notes/**,model/**` — cannot be judged and is denied. A search with no folder key is rooted at its
  include pattern's literal leading folders (`notes/**` searches `notes`), else at the call's `cwd`,
  so a search over the whole project (`**/*.md`, every `file_search`) is denied, the reason saying to
  narrow it. Kiro hands the include pattern to ripgrep's `--glob`, which reads it as `.gitignore`
  does: a brace alternative with no slash, or only a trailing one (`Engine.yaml`, `model`,
  `x.yaml/`), matches at any depth, so it is a search over the whole project too; one with a leading
  slash is anchored at the search's folder, so it is judged both as written and with the slash
  removed (`/model/**` is denied); one with a slash in the middle keeps its literal leading folders.
- **`code`** runs only the operations that change no file: `search_symbols`, `lookup_symbols`,
  `get_document_symbols`, `generate_codebase_overview`, `search_codebase_map`, `pattern_search`,
  `goto_definition`, `find_references`, `hover`, `completion` and `diagnostics`. `rename_symbol`,
  `format_code`, `pattern_rewrite` and an operation this release does not know are denied.
- **`invoke_sub_agent`** is allowed, its context files judged as reads (its own calls reach the hook,
  measured).
- **The writes** — `fs_write`, `fs_append`, `str_replace` and `delete_file` (its path under
  `targetFile`) — are Claude Code's write rule with their keys, a folder holding the model directory
  among what is denied. `delete_file`, which only removes, is also denied on the project folder and
  on every folder holding it or the model directory (`"."`, the project's full path, `".."`), on
  every folder holding the review's folder but not the top of the disk (`<your home>/.swcraft`, as
  `rm -rf` of it is), and on your SWCraft folder, every path in it and every folder holding it but
  not the top of the disk (Claude Code's rule on your SWCraft folder).
- **`execute_bash`** is Codex's shell rules with Claude Code's rules on the review's folder and your
  SWCraft folder, its `cwd` judged as Codex's `workdir` (a null one is the call's `cwd`).
- **`execute_pwsh`, `execute_cmd`** and the older engine's `fs_read` are denied whatever they carry.
  The older engine's `fs_write` and `execute_bash` carry keys the current tools do not list, so they
  are denied too.
- **`web_fetch`** is judged, not allowed by name: its keys are listed above (Kiro's tool service
  0.66.91), and its `url` must be a text opening with an `http` or `https` scheme, whatever the case. Any
  other URL — `file:`, another scheme, leading blanks, none — is denied in a guarded project, whether
  Kiro's fetch reads a local file not having been checked. Under an unreadable manifest every
  `web_fetch` is denied, an `http` or `https` one included, the reason asking for the fix by hand, as
  for every other judged call.
- **`control_bash_process` and `control_pwsh_process`**, which start a command in the background
  (Kiro's tool service 0.66.91: the action `start` with a `command`), are denied whatever they carry,
  the reason pointing at `execute_bash` with `run_in_background`. `get_process_output` and
  `list_processes`, which name no file, are allowed by name.

### The paths Kiro reads more ways { #kiro-path-readings }

<!-- purpose: How the guard judges every reading Kiro may give a path: ~, retried reads, joined paths -->

**A leading `~`.** Kiro's tool service expands `~`, and `~` followed by a slash or a backslash, to
your home folder (kiro-cli's `resolveRelativePath`). So every path key, pattern key, context file's
`path`, `code` path and `execute_bash`'s `cwd` opening with `~` is judged as under Claude Code, with
Node's way of joining paths among the readings. The text of an `execute_bash` command is unchanged:
its `~` is bash's (below).

**The readings Kiro's tool service adds**, each one reading more, never one fewer:

- a relative `read_file` that fails is retried with its first part stripped, so `zz/model/x.yaml` is
  judged as `model/x.yaml` too;
- a context file whose read fails is retried with its first part dropped, with **no** check for `..`;
- a first part naming the project folder or the call's `cwd` (as given and resolved) is removed;
- `/X:...` is read as `X:...`;
- a path holding `..` is read as Node's `path.join` reads it.

A reading derived so gets its `~` readings too.

### How the guard answers Kiro { #kiro-answers }

<!-- purpose: What Kiro gets back from the guard on a denial and on an allow -->

A denial is exit code 2 with exactly one line on the error output — the reason, every line break a
space, never empty, an unexpected failure and a pinned-release mismatch folded into it — and nothing
on the standard output. An allow is exit code 0 with nothing on the standard output, its notes as
lines of their own on the error output.

### The limits under Kiro { #kiro-does-not-reach }

<!-- purpose: The limits of the model guard under Kiro -->

- **Claude Code's list, as it applies here**: a command reaching the model without naming it, a
  script, a variable, `~` and `$HOME` in an `execute_bash` command's text (a file tool's leading `~`
  and the `cwd`'s are judged both ways, above), a profile's function, a `sed -f` script, a link from
  outside, an earlier `cd` in the same command.
- **Another server's file tools.**
- **A later Kiro.** A tool Kiro adds after kiro-cli 3.1.0 is denied by the list of allowed tools, but
  a new *argument* of a tool allowed by name is not read, and kiro-cli updates itself, so the lists
  can age without a word. A later Kiro may resolve paths differently: the leading-`~` expansion, the
  retried reads, the context-file fallback and the workspace-folder names above follow exactly
  kiro-cli 3.1.0 and 3.3.0 (Kiro's tool service 0.66.91 and 0.66.102) and Kiro 1.4.178
  (`kiro.kiro-agent` 1.1.939). kiro-cli 3.5.0 (0.66.119) retries fewer paths — none whose shortened
  form is rooted at the home folder (`~` alone, or a `~` followed by a slash or a backslash) or at a
  drive (a letter, a colon and then a slash or a backslash), and a context file under the same rule as
  a read; a shortened form such as `~alice/x`, or `C:x` relative to a drive, is still retried — so every path it tries is among those judged. Under a later release what the guard protects is not established
  until it is checked again.
- **What `code`'s `find_references` and its other reading operations answer** from files beyond the
  path given.
- **A context file Kiro reads into a sub-agent outside the hook** — whether it does is not measured;
  its path and the path Kiro retries are judged.
- **A workspace folder other than the project and the call's `cwd`** — another root of a window with
  several folders — which the hook is not told of, so a path naming another root folder is judged as
  relative to the project.
- **A folder not trusted, or trusted without the window reloaded** (no project hook runs); a hook
  switched off in Kiro's interface; a hook that runs past its time limit, after which the tool runs
  (measured: it fails open); a hook declared only in your own `$HOME/.kiro/hooks/`; both starting
  points outside the project.
- **A shell word naming the project folder or a folder above it** (`rm -rf .`,
  `rm -rf <the project's full path>`), which is **not judged** as a folder holding the model or the
  guard's own files; only `delete_file` is refused there.
- **What `web_fetch` reaches at an `http` or `https` URL.**
- **The review's folder reached through `~`, `$HOME` or any variable in a shell word**, a script or a
  path put together while a command runs: `printf … > $HOME/.swcraft/reviews/<id>.decision.json` is
  allowed, so an assistant can approve its own change that way. The confinement the review rests on
  is Kiro's own permission step; whether it asks before a shell write outside the project was not
  tried.
- **Your SWCraft folder reached the same ways**, and a guard program installed outside it (a Python
  environment, a system folder), which nothing protects.
- **`.kiro/steering` and `.kiro/specs`**, which are none of the guard's files, and reading one of the
  guard's files: the guard protects their integrity only.
- **An older `swcraft` without `--host kiro`**, whose usage error (exit code 2) blocks Kiro's file and
  shell tools; and no `swcraft` at all, after which every tool runs.

### What it denies more than it needs to under Kiro { #kiro-denies-more }

<!-- purpose: The harmless Kiro calls the guard denies, and how to word them so they pass -->

The guard errs towards denial. These are denied:

- a shell word, outside a reading command, naming `.github`, `.kiro` or a folder holding the model
  directory (Codex's list of needless denials);
- a relative path or a pattern meant against a subfolder `cwd` that names `model/x.yaml` from the
  project root, since both starting points are judged;
- an include pattern with a comma outside braces or a blank;
- any `web_fetch` URL other than an `http` or `https` one, and `control_bash_process`;
- a search without a literal include folder, a bare file-name include pattern (`Engine.yaml`, although
  no such file may be in the model — narrow it with a folder, `notes/Engine.yaml`), and every
  `file_search`;
- a `code` call naming no path;
- a `list_directory` deeper than one level at or above the model directory, although kiro-cli 3.1.0
  lists one level whatever the depth.
