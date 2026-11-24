<!-- draft: not yet reviewed by the product's author -->

# In Kiro { #kiro }

<!-- purpose: Using SWCraft in Kiro, the editor and kiro-cli: files, trust and reload, the guard, limits -->

Kiro is an editor built on the same open-source base as VS Code, with its own chat assistant and a
command-line client, `kiro-cli`. SWCraft works in it much as it does in VS Code: the SWCraft panel,
**Check Editor Tooling**, the diagrams, the test tables and the review tab are the same, and Kiro's
chat asks SWCraft's tools and proposes its changes through them, which open the same SWCraft review
tab. What differs is how Kiro finds SWCraft: Kiro's chat does not see the server SWCraft's extension
registers with the editor, so it reads SWCraft's server, instructions and model guard from three
files in the project — `.kiro/settings/mcp.json`, `.kiro/steering/swcraft.md` and
`.kiro/hooks/swcraft-guard.json` — which **Onboard Project** writes, the server's review set to
`panel`. A change waits for you in the SWCraft review tab *because* that review mode is `panel`;
`elicit`, the mode that asks in the client's own chat, is not the one to use in Kiro ([Asking and
changing](#asking-and-changing) says why). Kiro reads the three files only once you have trusted the
folder and then reloaded the window, once, with **Developer: Reload Window**. This page is about what is particular to Kiro; [In
VS Code](vscode.md) has everything else.

## Installing SWCraft's extension into Kiro { #installing-swcrafts-extension-into-kiro }

<!-- purpose: Installing SWCraft's extension into Kiro from the release's .vsix, and its two companions -->

SWCraft's installer hands the extension to VS Code, not to Kiro, so in Kiro you install it yourself,
from the `.vsix` in the unpacked release archive. Either open Kiro's Extensions view, its `…` menu,
**Install from VSIX…**, or use Kiro's own command line. On macOS it is the `code` command inside
Kiro's application, `Kiro.app/Contents/Resources/app/bin/code` in the Applications folder:

```sh
Kiro.app/Contents/Resources/app/bin/code --install-extension <the .vsix file>
```

Run it from the Applications folder, or write the application's full location before it. Kiro takes
its extensions from Open VSX instead of the VS Code Marketplace, and Open VSX carries the two
extensions SWCraft's comes with, Red Hat YAML and Red Hat XML: installing SWCraft's extension
brings them from there. Uninstalling SWCraft's extension in Kiro removes the two with it.

Everything [Install the extension](vscode.md#install-the-extension) says about the `swcraft` the
extension runs holds in Kiro too: it finds your SWCraft installation without being told, and
`swcraft.executable` is read from your user settings alone.

## Onboard Project in Kiro { #onboard-project-in-kiro }

<!-- purpose: The three files Onboard Project writes for Kiro, and what happens to a file of your own -->

**Setup → Onboard Project** works as [Turning a folder into a project](vscode.md#turning-a-folder-into-a-project)
describes. Its list of chat clients names **Kiro**, and run inside Kiro it starts with Kiro ticked
instead of GitHub Copilot. On the command line, `--client kiro` makes the same choice:

```sh
swcraft init my-arch --integration moon --client kiro
```

`swcraft init` has no editor to look at, so without `--client` it writes Copilot's files only.
For Kiro, onboarding writes three files:

| File | What it does |
|---|---|
| `.kiro/settings/mcp.json` | starts SWCraft's server as `swcraft mcp-serve`, the project's `swcraft.yaml` named relative to the project folder Kiro starts it in, with `SWCRAFT_REVIEW` set to `panel` |
| `.kiro/steering/swcraft.md` | SWCraft's instructions for the assistant — the assistant profile whole, opened by a few lines for Kiro: its own file and shell tools stay off the model and the guard's files, a question about SWCraft itself goes to SWCraft's `guide` before any of Kiro's own tools, and each SWCraft tool is loaded with `tool_load` before its first call |
| `.kiro/hooks/swcraft-guard.json` | the model guard's hook, `swcraft guard --host kiro`, run before every tool call ([The model guard under Kiro](#the-model-guard-under-kiro)) |

The `SWCRAFT_REVIEW` value `panel` in `.kiro/settings/mcp.json` is what makes a change Kiro's chat
proposes wait for you in the SWCraft review tab; keep it, since `elicit` is not the review mode to use
in Kiro ([Asking and changing](#asking-and-changing)).

They name nothing per-machine, so they go into version control with the project. The instructions
file has no header of Kiro's own, which Kiro reads as *always included*: Kiro lists it under
**Included Steering** in its chat.

**Your own file is kept.** Each file is written where the project has none and replaced where it is
SWCraft's own: `.kiro/settings/mcp.json` while it holds nothing but SWCraft's server, the hook file
while every hook in it runs `swcraft guard --host kiro` and none carries Kiro's own `enabled` or
`description` key — so a hook you switched off in Kiro stays off, the file then yours — and the
instructions file while its first
line still says **Onboard Project** wrote it — delete that line and the file is yours. A file that
is yours is left as it is, and the onboarding output names it with this page: add SWCraft's part to
it by hand, from the table above. Nothing is written through a link, nothing outside the project,
and no trust decision is taken for you.

## Trust the folder, then reload the window once { #trust-the-folder-then-reload-the-window-once }

<!-- purpose: Why Kiro shows no SWCraft server, guard or instructions until the window is reloaded -->

Kiro asks whether to trust a folder the first time you open it. Trust the folder — choose **Trust** —
and then reload the window, once: run **Developer: Reload Window** from the command palette. Right after **Trust**, without the
reload, Kiro has not loaded the project's files: no SWCraft server, the guard's hook disabled, and no
SWCraft instructions. Kiro's own log says so, with lines such as `workspace scope skipped: workspace
is not trusted` and `hooks.v2.executionDisabledUntrustedWorkspace`. After the reload all three are
there, and a folder you trusted before has them from the moment you open it.

To check: Kiro's MCP servers view shows `swcraft` connected, its hooks list shows **SWCraft model
guard**, and its chat lists the instructions file, `.kiro/steering/swcraft.md`, under **Included
Steering**.

## Which `swcraft` Kiro runs { #which-swcraft-kiro-runs }

<!-- purpose: Which swcraft Kiro's server and guard hook run, and what an older or missing one does -->

Kiro starts the server and the guard's hook as a bare `swcraft`, looked up on the path Kiro was
started with. Started from the Dock or Finder, Kiro reads that path from your login shell, as VS
Code does; started from a terminal, it takes that terminal's path. The server is started in the
project folder, so open the project folder itself in Kiro: anywhere else, every SWCraft tool answers
that there is no SWCraft project here ([Start in the project
folder](vscode.md#start-in-the-project-folder) has the message).

- **An older `swcraft` first on that path** refuses the project's pinned release, so the chat says
  SWCraft's tools are unavailable — and the guard's hook stops with a usage error, which Kiro reads
  as a denial: Kiro's file and shell tools are **blocked**, every one of them. SWCraft's own tools
  and `tool_load` are probably blocked too; that was not tried.
- **No `swcraft` at all**: the server does not start, and the hook cannot run, after which Kiro
  **runs every tool** — the guard does not block when it is missing.

SWCraft's extension warns when the bare `swcraft` that `.kiro/settings/mcp.json` names is not the
one the extension uses. The check is the one VS Code has: quit Kiro, start it again from the Dock or
Finder, and see that the warning does not come back ([Which `swcraft` the chats
run](vscode.md#which-swcraft-the-chats-run)).

## Allow, once per call { #allow-once-per-call }

<!-- purpose: Why Kiro asks Allow before every SWCraft tool call, and the autoApprove key not used -->

Kiro asks before each call of a SWCraft tool, and **Allow** runs it. That is Kiro's permission to
run a tool, not the review: a change still waits for you in the SWCraft review tab. Kiro's server
file can carry an `autoApprove` list that stops the question for the tools it names; SWCraft's file
does not, because whether it acts in Kiro's chat was not tried — in a scripted `kiro-cli` run it did
not. Add it yourself if you want it: the file is then yours, and **Onboard Project** keeps it as it
is.

## Asking and changing { #asking-and-changing }

<!-- purpose: How a question and a change go in Kiro's chat, the review tab, and how long Kiro waits -->

Kiro loads a server's tools only when its assistant asks for them: before it first calls a SWCraft
tool it calls `tool_load` for it, one more step in the chat and nothing to answer. A question is then
answered through SWCraft's tools. A change is proposed through `change_set`: the SWCraft review tab
opens in Kiro, naming the client as `kiro`, and you answer it there — never in the chat. While the
tab waits, Kiro's chat shows the tool as running, with **Working** and **Cancel**: that is the wait
for your answer, not a failure.

Kiro documents no limit on how long a tool call may take, and SWCraft's file sets none. The longest
wait tried was 20 minutes, in `kiro-cli`, which kept waiting until it was stopped; in the editor
the wait tried was a few minutes. `panel` is the review to use: `elicit`, which asks in the client's
own chat, was answered *cancel* by a scripted `kiro-cli` run, so the proposal ended abandoned with
your files untouched, and Kiro's editor chat was not tried with it.

## `kiro-cli` in a terminal { #kiro-cli-in-a-terminal }

<!-- purpose: Running kiro-cli in an onboarded project: trust, the start race, approvals, updates -->

`kiro-cli` started in the project folder reads the same three files, so its proposals wait for the
review tab of a VS Code or Kiro window open on the project with SWCraft's extension. Interactively it
asks whether to trust the folder before it loads the project's server, hook and instructions. A
scripted run looks like this:

```sh
kiro-cli chat --v3 --no-interactive --trust-workspace --require-mcp-startup --trust-tools=mcp_swcraft_read,mcp_swcraft_change_set "Which components are on CORE0?"
```

- `--require-mcp-startup` waits for SWCraft's server before the first answer, and ends the run with
  exit code 3 when the server cannot start. Without it the first answer came before the server was
  connected, and the assistant said it had no SWCraft tools.
- `--trust-tools` approves the SWCraft tools you name, each as `mcp_swcraft_<tool>`; naming the
  server as a whole (`@swcraft`) did not approve them.
- `--v3` names Kiro's current engine. A scripted run that names none runs the older one, whose file
  tools the guard denies whatever they do.
- `--trust-workspace` trusts the folder for that one run.

Your own `$HOME/.kiro/settings/mcp.json` is read beside the project's, the project's winning: a
`swcraft` server you once added there by hand is better removed. And `kiro-cli` **updates itself**:
it moved from 3.0.0 to 3.1.0 while SWCraft was being tried with it. A release that renames a tool
can change what the guard sees ([The model guard under Kiro](#the-model-guard-under-kiro)).

## Content collection is on by default { #content-collection-is-on-by-default }

<!-- purpose: Kiro sends prompts and content for service improvement by default; where to turn it off -->

Kiro's own data-protection page says that by default, on its free tier and for individual
subscribers, Kiro collects content — what you and its assistant send, your model's text among it —
for service improvement. Check your organisation's rules before you use it on an architecture that
must not leave your machine. To turn it off in the editor: **Settings → User → Application →
Telemetry and Content**, and clear **Data Sharing and Prompt Logging: Content Collection for Service
Improvement**. `kiro-cli` showed the setting to its hooks as `KIRO_CONTENT_COLLECTION_ENABLED`;
whether the editor's switch also reaches `kiro-cli`, and the command line's own setting, were not
looked into.

## Kiro with Codex or Claude Code { #kiro-with-codex-or-claude-code }

<!-- purpose: Choosing Kiro beside Codex or Claude Code: Kiro reads AGENTS.md too, cut short -->

Kiro reads a project's `AGENTS.md` on its own, as instructions always included — and `AGENTS.md` is
what the Codex and Claude Code choices write. With Kiro chosen beside either of them, Kiro reads
SWCraft's instructions twice: whole from its own `.kiro/steering/swcraft.md`, and again from
`AGENTS.md`, which Kiro cuts at 50 000 characters, losing the end of the model's schema, and whose
opening lines speak to Codex and Claude Code. That costs every request about 12 500 tokens more,
and no way was found to have Kiro skip `AGENTS.md`. The choice is allowed; onboarding shows one
warning, and `swcraft init` prints it after `Warning:`:

```text
Kiro also reads AGENTS.md, so it reads SWCraft's instructions twice, the second time cut short; see the guide's "In Kiro" page.
```

Kiro with Copilot shows no warning: VS Code reads nothing under `.kiro/`, and Kiro was not seen
reading Copilot's assistant profile.

## The model guard under Kiro { #the-model-guard-under-kiro }

<!-- purpose: What the model guard decides under Kiro, before each of Kiro's tool calls -->

Every rule of the guard under Kiro,
and everything it does not reach, is on [The model guard's rules](model-guard.md#under-kiro).

The project's `.kiro/hooks/swcraft-guard.json` asks Kiro to run `swcraft guard --host kiro` before
every tool call, in the editor and in `kiro-cli` alike. A denial is one line beginning `SWCraft:`,
which Kiro passes to its assistant while it blocks the tool; in a scripted run the call shows as
failed. Asked to write a model file directly, the guard answers:

```text
SWCraft: in this project the model directory (`model`) is changed through the SWCraft tools, whose changes the panel reviews; this edit writes `model/hardware/CPU.yaml` there. Use the SWCraft MCP tool `change_set` instead.
```

It joins the other guards ([The model guard under Claude
Code](vscode.md#the-model-guard-under-claude-code)) and:

- **denies** a read (`read_file`, and `list_directory` one level deep) of a file in your model
  directory, and a deeper listing at, in or above it;
- **denies** a search whose folder is the model directory, inside it or above it — the project
  folder included. Kiro's searches name a folder only through an include pattern: `grep_search` with
  `notes/**` searches `notes`, but one without such a folder, and every `file_search`, searches the
  whole project and is denied, with a reason saying to narrow it with an include pattern. An include
  pattern that lists several, with a comma outside braces or a blank (`notes/**,model/**`), cannot be
  judged and is denied; `{notes,docs}/**` is judged by each folder it names. A pattern with no folder in
  it — a bare file name such as `Engine.yaml`, or a bare folder name — matches at any depth, the model
  directory included, so it too is a search over the whole project and is denied: narrow it with a
  folder, `notes/Engine.yaml`. Kiro's `code` tool is judged as a search on the path it names; its operations that edit files beyond that
  path — renaming a symbol, formatting, rewriting by pattern — are denied;
- **denies** a write, an append, a replacement or a deletion (`fs_write`, `fs_append`,
  `str_replace`, `delete_file`) of a file in the model directory, naming the file, and one whose file
  it cannot judge or that names none. Where your model directory sits in a folder of its own inside
  the project — `arch` for a model directory `arch/model` — that folder itself is protected too, as
  under Codex: writing or deleting `arch` is denied, while a file beside the model inside it, such as
  `arch/notes.md`, is not. `delete_file` is also denied on the project folder itself and on every
  folder holding it (`.`, `..`, the project's full path);
- **denies** a shell command (`execute_bash`) by Codex's rules, word for word ([The model guard
  under Codex](vscode.md#the-model-guard-under-codex)), from the folder Kiro says it runs in, else the
  project folder; a folder in the model denies it. `execute_pwsh`, Kiro's Windows shell, is denied
  whatever it runs: the guard reads bash;
- **denies** every change to the guard's own files — `swcraft.yaml`, `.kiro/hooks/`,
  `.kiro/settings/`, `.kiro/agents/`, `.mcp.json`, `.codex/`, `.claude/` and `.github/hooks/` — and
  to the folders that hold them, so `rm -rf .kiro` and deleting `.github` are denied too. Reading
  them is allowed. `.kiro/steering/` and `.kiro/specs/` are yours and Kiro's to change. An edit of
  another server in `.kiro/settings/mcp.json`, or of a Kiro agent under `.kiro/agents/`, is denied
  with them: make it by hand;
- **denies** a path into the review's own folder, as under Claude Code;
- **denies** a read, a listing, a search's include pattern, a file handed to a sub-agent, a write and
  the folder an `execute_bash` command runs in, when the path holds your model directory's path
  anywhere, or starts with its last folders, whatever Kiro then does with it — `zz/yy/model/swc/x.yaml`
  and `zz/model` as a command's folder included, as [under
  Copilot](vscode.md#the-model-guard). This holds whatever a later Kiro does with such a path; the
  words of the command itself are judged as the shell reads them instead;
- **denies** the same calls when the path ends like a file or folder in the model directory — while
  `model/swc/Engine.yaml` exists, `elsewhere/swc/Engine.yaml` and the include pattern
  `notes/**/swc/Engine.yaml` — and, while the guard cannot list the model directory whole (more than
  100,000 files and folders, more than two seconds, or a folder it may not read), every path of two
  or more parts, an absolute one and a command's absolute folder included, until you make the model
  directory readable again (its permissions, a broken mount) or keep less in it;
- **denies** a change to your SWCraft folder, `$HOME/.swcraft`, where the `swcraft` the guard runs
  is installed, as under Claude Code: a write, an edit or a deletion there — `delete_file` of the
  folder, of a folder inside it or of a folder holding it included — and a shell command that
  writes, removes or moves anything there. Reading it is allowed, except the review's folder. So Kiro
  cannot update or reinstall SWCraft: you do that;
- **denies** a tool it does not know, and a call of a tool it judges that carries an argument it does
  not know, saying so: Kiro updates itself, and a new file tool or a new way to name a file must not
  pass unjudged. It allows SWCraft's tools and every other server's, `tool_load`, web search, the few
  built-in tools that touch no file, and reading a command's output while it runs in the background
  (`get_process_output`, `list_processes`). A web fetch is allowed for an `http:` or `https:`
  address only: a `file:` address, any other kind, or none is denied. Starting a command in the
  background through Kiro's process tool is denied whatever it runs: `execute_bash` with `run_in_background`, then `get_process_output`, is the way. A
  sub-agent is allowed — its own calls reach the guard the same way — and the files handed to it are
  judged as reads;
- **allows** everything else. Where `swcraft.yaml` is there but cannot be read or is not valid, it
  denies every call it judges, a read of `swcraft.yaml` included: fix the file by hand in the
  editor.

The project is the nearest folder holding `swcraft.yaml` and `.kiro/hooks/`, looked for upwards from
the folder of the call and from the folder Kiro runs the hook in, which is the project folder: a
call that names another folder still judges the project. A relative path, a search that names no
folder and a shell command that names no folder of its own are judged from both — the folder of the
call and the project folder — and a denial from either wins. A call the guard cannot read is denied
in a project.

Kiro reads a path that opens with `~` as one in your home folder, `$HOME`. So the guard judges such
a path — in a file tool, an include pattern, a file handed to a sub-agent, and the folder an
`execute_bash` command runs in — both ways, as a path in your home folder and as one in a folder
named `~`, and denies it when either reading reaches the model, the guard's files, the review's
folder or your SWCraft folder: the review's folder or the project's model written from `~` is
denied, a notes file in your home folder written that way is allowed. A path naming another user's
home, such as `~someone`, is denied: write it in full.

Kiro also tries a path in more than one place, and the guard judges every place it may try. When
`read_file` cannot open a relative path, Kiro tries it again without its first folder, so
`zz/model/hardware/CPU.yaml` is judged as `model/hardware/CPU.yaml` too. A file handed to a
sub-agent is tried again the same way, even when what is left climbs out with `..`, so
`zz/../outside.md` is judged as `../outside.md` too. And in a window with several folders, a path
whose first folder has the name of one of them is read in that folder, so a path that starts with
the project folder's name, or the name of the folder of the call, is judged with that name removed
as well: in a project folder named `p`, writing `p/model/hardware/CPU.yaml` is denied. A path holding
`..` is also judged as Kiro reads it, the `..` taken away before any link is followed, so
`hop/../model/x.yaml` is judged as `model/x.yaml` even where `hop` is a link to another folder.

### What the guard under Kiro does not reach { #what-the-guard-under-kiro-does-not-reach }

<!-- purpose: The limits of the model guard under Kiro, and what it denies more than it needs to -->

The Codex list first, as it applies here: a command that reaches the model without naming it (a
script, a variable, a substitution, a tool run over the whole project), `~` or `$HOME` written in
the text of a shell command (the shell expands them; a file tool's `~` is judged, above), a function a shell profile defines, what a `sed -f` script writes, a link reached from
outside the project, and a folder an earlier `cd` in the same command moves into. Then:

- **A shell command naming the project folder itself or a folder above it** — `rm -rf .`, the
  project's full path, `rm -rf ..`: the guard does not judge it as naming a folder that holds the
  model or the guard's files, because denying every command that names `.` would deny `git add .`.
  Only `delete_file` is refused there. The guard protects its files from being changed, not from
  being read.
- **The review's folder reached any other way.** A shell command writing it with `~`, `$HOME` or
  any variable, a script that opens the files, a path put together while a command runs: a `cat` of
  the pending proposals or a `printf` of an answer into that folder, written that way, is allowed —
  and an answer written there approves the change. The guard stops an accidental reach, not a
  deliberate one; what the review rests on is Kiro's own permission step for its file and shell tools.
  Whether Kiro asks before a shell write outside the project folder in particular was not tried, so
  do not approve Kiro's file and shell tools wholesale (`kiro-cli`'s `--trust-all-tools`) in a
  SWCraft project.
- **Your SWCraft folder reached any other way**, the same ways, and a `swcraft` installed somewhere
  other than your SWCraft folder (a Python environment of your own, a system folder), which the
  guard does not protect.
- **A home folder whose path holds a search pattern's character** — `{`, `}`, `(`, `)`, `[`, `]`,
  `*`, `?`, `@`, `+`, `!` or `|`, such as a user folder named `John (Admin)`: the guard cannot tell
  that path from a pattern, so every path and pattern opening with `~` is denied there. Write the
  path in full.
- **A guard that does not run.** A folder not trusted, or trusted without the reload; the hook
  switched off in Kiro's hooks list; a hook declared only in your own `$HOME/.kiro/hooks/`, which
  finds no project; `kiro-cli` started, or a Kiro window opened, outside the project folder, so that
  both places the guard looks from are outside it: it finds no project and allows everything. A hook that runs past its 10 seconds is stopped by Kiro, **and the tool runs** —
  seen with a hook made to wait. An older `swcraft` blocks Kiro's tools and none lets every tool run
  ([Which `swcraft` Kiro runs](#which-swcraft-kiro-runs)).
- **Another server's file tools**, which pass like SWCraft's own.
- **The other folders of a window with several folders.** Kiro does not tell the guard which they
  are, so a path that starts with the name of one of them is judged as a path in the project
  folder, not in that other folder.
- **A path that does not spell the model directory.** A new file whose path holds neither the model
  directory's path nor its last folders, which a later Kiro turns into a model file in a way of its
  own, a model directory named with a pattern (`mod?l`) or split by `..` (`arch/x/../model`), a path
  that ends like a model file only once its `..` is applied, a single name or a pattern whose last two
  parts hold a wildcard, a file added to the model while the guard judges the call, other Unicode
  forms of the same letters, and a Windows short name (`INTERF~1`): the guard's copy of Kiro's own
  reading is what judges these.
- **A later Kiro.** The guard was checked against exactly `kiro-cli` 3.1.0 and Kiro 1.4.178 (its
  agent extension 1.1.939), and it mirrors how those releases, and `kiro-cli` 3.3.0, whose handling of
  paths was read again and found the same, turn a written path into a file: the home folder for a
  leading tilde, the read tried again without its first folder, the files handed to a sub-agent, and
  the names of a window's folders. `kiro-cli` 3.5.0's handling was read too: it tries fewer paths
  again (none whose shortened form is rooted at the home folder — a tilde alone, or one followed by
  a slash or a backslash — or at a drive, a letter, a colon and then a slash or a backslash,
  and a sub-agent's file under the same rule as a read; a shortened form such as `~alice/x`, or
  `C:x` relative to a drive, is still tried), and every path it tries is one the
  guard judges already. `kiro-cli` updates
  itself without asking, and
  a later Kiro may turn paths into files differently; the guard then needs checking again against
  that release, and until it is, what the guard protects under a later Kiro is not established. A
  tool Kiro adds is denied until the guard knows it, and so is a new argument of a tool it judges;
  but a new argument of a tool it allows without judging — SWCraft's, another server's,
  `tool_load`, the web search tools — is not read.
- **What a web fetch reaches** at an `http:` or `https:` address.
- **Files read on a tool's behalf**: what `code`'s reading operations take from files other than the
  one named, and the files Kiro reads into a sub-agent itself, if it does (not seen either way).
- **The editor's write tools.** In Kiro's editor a read and a shell command on the model were seen
  reaching the guard and denied, with the same tool names as on the command line. A direct edit
  asked for in the editor stopped at its first step, a read, so no write tool of the editor has
  reached the guard yet; the command line's writes were seen and are the ones the guard judges.

It also denies more than it needs to: a search over the whole project, a `code` call that names no
path, and a listing deeper than one level at or above the model — although `kiro-cli` 3.1.0 lists
one level whatever depth it is asked for. A shell command other than the reading ones that names
`.github`, `.kiro` or a folder holding the model is denied even when it writes nothing (`git add
.github`, `git diff .kiro`, `find .github -name x`, `cd .github && ls`, `git add arch`). A relative
path or pattern meant for the folder of the call is denied when it reads as the model from the
project folder: `grep_search` for `*.md` from the `notes` folder counts as a search over the whole
project, and so does an include pattern naming a bare file name, `Engine.yaml`, even when no such file
is in the model. And so are an include pattern listing several with a comma or a blank, a web fetch of
anything but an `http:` or `https:` address, and every background start through Kiro's process
tool. On macOS and Linux a path that opens with a slash and then a drive letter is denied as well:
Kiro drops that slash and reads the rest as a path inside the project folder. A path holding a folder
named like the model directory is denied too (`docs/model/notes.md`, a path through `..` that passes
its name), and, for a model directory of several folders such as `arch/model`, a relative path that
starts with `model`, because a second window folder at that place would make it a model file. So is a
file outside the model whose last folder and name are a model file's, `docs/interfaces/Foo.yaml`
beside `model/interfaces/Foo.yaml`: rename the folder or the file, or let the assistant use the
SWCraft tools.

## What was tried { #what-was-tried }

<!-- purpose: Which Kiro releases, models and steps SWCraft was tried with, and what was not tried -->

On 2026-11-09, on macOS, with Claude Haiku 4.5 as the model. In **Kiro 1.4.121** (its agent
extension 1.1.787), on a project set up for Kiro: trusting the folder and reloading the window once,
the server connecting from the project's file, the instructions listed under **Included Steering**,
a question answered through SWCraft's tools, a change committed through the SWCraft review tab, and a
direct edit and a terminal command on the model refused by the guard. Onboard Project listed Kiro;
its starting with Kiro ticked inside Kiro came after that walk-through and is checked by SWCraft's
own tests only. With **`kiro-cli` 3.0.0 and 3.1.0** (it updated itself in between): every call the
guard judges, captured from Kiro and replayed against the guard; a hook past its time limit, after
which the tool ran; a wait of 20 minutes for the review; `elicit` answered *cancel*; the server's
start race; `AGENTS.md` cut at 50 000 characters and the instructions file read whole.

**The release's own check with `kiro-cli`:** on 2026-11-10, `kiro-cli` 3.1.0 with Claude Haiku 4.5,
on a project set up for Kiro by this release, answered a question from the model through SWCraft's
tools, committed a change approved through the review, and was refused a direct edit of a model file
by the guard, the file unchanged.

What was not tried: Windows and Linux; `autoApprove`; `elicit` in Kiro's editor chat; a **Reject**
through Kiro; a wait longer than 20 minutes; other models and releases; the editor's write tools
reaching the guard; and how Kiro's editor finds the `swcraft` its files name, beyond the path the
walk-through set.
