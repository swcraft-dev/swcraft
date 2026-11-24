# SWCraft — the toolchain, in one file

You have unpacked the SWCraft workstation archive. It installs **one** SWCraft
toolchain on **this machine**, pinned to the release whose version the archive's
filename carries:

- `swcraft` — the command line: scaffold a project, check a model, generate
  AUTOSAR ARXML, diagrams and Sphinx-Needs JSON, validate the result;
- `swcraft-mcp` — the MCP server, which is how an AI assistant reads and edits
  a model;
- `swcraft-agent` — the assistant host;
- `swcraft-specimens` — the specimen generator, which makes synthetic
  architecture projects; it is part of this release and installed beside
  `swcraft`: `swcraft-specimens specimen passport.json --out <dir>` generates a
  project of the shape a `swcraft passport` file describes, and
  `swcraft-specimens generate small --out <dir>` one from a packaged preset;
  either project's `swcraft.yaml` names this release;
- the SWCraft **VS Code extension**, as a `.vsix` in this folder, and the two
  editor extensions it works with, in `extensions/`.

Everything is installed from the `wheels/` and `runtime/` folders beside this
file. **The installer downloads nothing**, so it works on a machine with no
network access and needs no administrator rights.

Nothing here is a project. A SWCraft *project* — the YAML model, `swcraft.yaml`,
the build tasks — is created afterwards with `swcraft init`, and it installs
nothing of its own.

## What is in this folder

| File | What it is |
| --- | --- |
| `README.md` | this file |
| `SHA256SUMS` | the SHA-256 of every other file here, one per line |
| `install.py` | the installer: one Python file, standard library only |
| `swcraft_install.cmd` | the Windows wrapper — checks the Python in `runtime/`, unpacks it and runs `install.py` on it |
| `swcraft_install.sh` | the macOS and Linux wrapper — the same job |
| `swcraft-vscode-<version>.vsix` | the VS Code extension |
| `wheels/` | every Python package the installation needs, plus `PLATFORM.json`, which records the platform and interpreter they were downloaded for |
| `runtime/python.tar.gz` | Python, as the file the python-build-standalone project publishes, byte for byte — so its SHA-256 can also be compared with the one that release lists. The installer and the installed toolchain run on it |
| `runtime/node.tar.gz` | Node.js, as the file nodejs.org publishes, byte for byte — so its SHA-256 can also be compared with the one nodejs.org lists. It is `node.tar.xz` in the Linux archive and `node.zip` in the Windows one |
| `runtime/RUNTIME.json` | the versions of the files in `runtime/` and `extensions/`, and the platform they were built for |
| `extensions/redhat.vscode-yaml.vsix` | Red Hat's YAML extension: completion and validation in the model files |
| `extensions/redhat.vscode-xml.vsix` | Red Hat's XML extension, built for this platform: structure and validation in the generated ARXML, with no Java needed |
| `THIRD-PARTY-NOTICES.md` | every part of this folder SWCraft did not write, with the licence its publisher declares; for the runtime and the editor extensions the licence text too, and for each third-party Python package the paths of the licence files inside its wheel |

## Before you install

Nothing has to be on the machine first, and nothing is downloaded while you
install. Three things are worth knowing:

- **Python** does not need to be on the machine. This archive carries its own,
  in `runtime/`: the wrapper checks it against `SHA256SUMS`, unpacks a copy into
  a `.bootstrap` folder beside this file to run the installer with, and the
  installation is then built on a second copy of its own. A Python you already
  have is not used and not changed.
- **Node.js** does not need to be on the machine either. This archive carries its
  own, in `runtime/`, and the installer places `node` beside `swcraft` in the
  installation — which is what `swcraft generate diagrams` lays the diagrams out
  with. There is no setting to change.
- **VS Code** is optional, and only the extension step needs it. That step is
  skipped, with a sentence saying so, when VS Code's `code` command is not on
  `PATH`.
- **The VS Code Marketplace** does not need to be reachable. The two editor
  extensions SWCraft works with travel in `extensions/`, and the installer hands
  them to VS Code before SWCraft's own.

Parts of this folder were written by others — Node.js, Python, the two editor
extensions, the diagram layout engine (elkjs) that travels inside the
SWCraft packages in `wheels/`, and the third-party Python packages in `wheels/`.
`THIRD-PARTY-NOTICES.md` lists each with its licence.

## Step 1 — check what you downloaded

The installer verifies every file in this folder against `SHA256SUMS` before it
installs anything, and refuses a file that does not match. You can do the same
by hand from inside this folder.

macOS and Linux:

```sh
shasum -a 256 -c SHA256SUMS
```

Windows PowerShell (`Get-FileHash` has no `-c` mode, so compare in a loop):

```powershell
Get-Content SHA256SUMS | ForEach-Object {
    $expected, $name = $_ -split '\s+', 2
    $actual = (Get-FileHash -Algorithm SHA256 $name).Hash.ToLower()
    if ($actual -eq $expected) { "OK       $name" } else { "MISMATCH $name" }
}
```

The zip you downloaded is covered separately: the file published beside it names
the zip's own SHA-256. Both are integrity checks, not signatures — nothing here
is signed.

## Step 2 — install

Windows, from this folder in PowerShell or Command Prompt:

```
.\swcraft_install.cmd
```

macOS and Linux, from this folder:

```sh
./swcraft_install.sh
```

You can also double-click `swcraft_install.cmd` on Windows: it waits for a key
press at the end so that the last line stays readable. A run started from
PowerShell with no arguments waits for a key in the same way — press one. Set
`SWCRAFT_NO_PAUSE` to switch that off, which is what an unattended machine
wants.

### What you see while it runs

One line for the wrapper's own work, then one numbered line per step, each
closed by how long the step took; a step's own sentence, such as the
`export PATH=` line, is printed under its line:

```text
preparing the installer ... done
Installing SWCraft <version> into /Users/you/.swcraft
1/12 checking the wheels against this machine ... done, 0 s
2/12 unpacking Python ... done, 3 s
3/12 creating the Python environment ... done, 4 s
4/12 installing swcraft-agent and its dependencies ... done, 38 s
...
12/12 removing the installer's temporary Python ... done, 1 s
SWCraft <version> is installed in /Users/you/.swcraft
```

The number of steps depends on what this folder carries and on the options you
pass. The programs the steps start — Python's environment builder, `pip`, VS
Code's `code` command — do not write to the screen: their output goes into
`install.log` in the installation directory,
`%LOCALAPPDATA%\SWCraft\install.log` on Windows and `~/.swcraft/install.log` on
macOS and Linux (or in the directory `--home` names). The log holds the **last
run only** — every install replaces it — and it stays on disk. A user name and
password written into an `--index-url` are masked in the installer's own lines
of the log; whatever a program prints about what it was given is written as the
program printed it, and a password passed in a URL's query is not masked at all.
On macOS and Linux the installer creates the log readable by you alone; on
Windows it is protected like the rest of the directory it is in, which under
`%LOCALAPPDATA%` is your own.

`--verbose` puts the programs' output back on the screen; the log is still
written, without it. A package index that asks for a user name and password
while installing needs `--verbose` in a terminal: otherwise the installer gives
the program no keyboard, so the step fails at once with the question in the log
instead of waiting on a question nobody can see.

### What the installer does, in order

1. The wrapper checks `runtime/python.tar.gz` against its line in `SHA256SUMS`,
   unpacks it into `.bootstrap` beside this file, and starts `install.py` with
   that Python.
2. Checks every file here against `SHA256SUMS`, reads the version to install
   out of the filenames, and opens `install.log` in the installation directory,
   creating that directory.
3. Checks that the wheels in `wheels/` were downloaded for that Python and this
   platform.
4. Creates the installation under your user profile —
   `%LOCALAPPDATA%\SWCraft` on Windows, `~/.swcraft` on macOS and Linux — as
   `versions/<version>/`: unpacks the Python from `runtime/` into it, builds the
   installation on that Python, and installs the SWCraft packages, the
   specimen generator among them, into it from `wheels/`, offline.
5. Unpacks Node.js from `runtime/` into that same version, and places `node`
   beside `swcraft` in its executable directory.
6. Points the `current` link at the version it just installed.
7. Makes `current`'s executable directory reachable. **On Windows** the
   installer edits your *user* `PATH` itself, once — open a **new** terminal
   afterwards, because an already-open one keeps the old value. On **macOS and
   Linux** it edits no file: it prints one line,
   `export PATH="<home>/current/bin:$PATH"`, for you to add to your shell
   profile yourself.
8. Hands the two editor extensions in `extensions/` and then the SWCraft
   `.vsix` to VS Code, when `code` is on `PATH`.
9. Removes the other versions it finds, so one installation is left, and then
   the `.bootstrap` folder. On Windows the Python running the installer lives in
   that folder and cannot be removed while it runs, so the wrapper removes the
   folder once the installer has finished. On macOS and Linux, if the folder
   cannot be removed, the installer says so in one sentence and you can delete it
   yourself: nothing uses it any more.

The order is deliberate: the new version is installed *beside* the old one and
`current` is switched only once it is there, so a run that is interrupted leaves
a working installation rather than none.

### The VS Code step

Before handing anything over, the installer reads the VS Code version the
SWCraft extension declares — out of the `.vsix` in this folder, never a number
written into the installer — and asks your VS Code what version it is. It asks
that of SWCraft's extension only: an editor extension from `extensions/` that
needs a newer VS Code is refused by VS Code itself, and that is reported as the
fifth outcome below. For the SWCraft extension, four outcomes, each one sentence:

- **installed** — the usual one, and the extension replaces whatever version of
  itself was there before, in either direction;
- **your VS Code is too old** — the run stops before the handover and names your
  version, the version the extension needs, and the `.vsix` to install once you
  have updated the editor;
- **`code` was not found** — the toolchain is installed, the extension is not,
  and the sentence names the `.vsix` to install yourself from the Extensions
  view (*Install from VSIX…*). On macOS a VS Code started from the Dock often
  has no `code` on `PATH`; *Shell Command: Install 'code' command in PATH* from
  the Command Palette adds it;
- **`code` could not be started, or failed** — same again: the toolchain is
  installed, the extension is not, and the sentence names the file to install by
  hand.

And one more for the two editor extensions, which are handed over first:

- **an editor extension was not installed** — one sentence naming it and the
  `.vsix` in `extensions/`, and the run **carries on**: the toolchain and the
  SWCraft extension are installed either way, and what is missing is completion
  or ARXML checking in the editor until you install that extension by name or
  from the file.

The editor extensions in this folder are a **starting point, not a pin**. When
your VS Code can reach the Marketplace and it holds a newer version of one, VS
Code updates to it and keeps the newer one, as it does for any extension.

### Options

`install.py --help` lists them all. The ones worth knowing:

| Option | What it does |
| --- | --- |
| `--python PATH` | build the installation on this Python 3.11 instead of the one the archive carries, and unpack none — run `install.py` directly to use it |
| `--home DIR` | install into this directory (an absolute path) instead of the user-profile default |
| `--no-vsix` | do not touch VS Code at all — what an unattended machine wants |
| `--no-path` | change no `PATH` and print no `export PATH=` line |
| `--uninstall` | remove the installation and the extension again |
| `--verbose` | show every program's own output on the screen instead of only in `install.log` |

The wrappers pass everything through, so `.\swcraft_install.cmd --no-vsix` and
`./swcraft_install.sh --no-vsix` are the way to use them.

`install.py --index-url URL` installs the same SWCraft packages, the specimen
generator among them, from a package index instead of `wheels/`.

## After the install

Open a new terminal on Windows, or add the printed `export PATH=` line to your
shell profile on macOS and Linux, and then:

```sh
swcraft --help          # the command line
swcraft docs            # serve the user guide from this installation and open it
```

`swcraft docs` needs no project and no network: the guide ships inside the
installation. It is the place to go next — it explains the model, the layers and
the commands.

To start a project:

```sh
swcraft init myproject --integration moon
swcraft check-model --project myproject
```

In VS Code, the extension adds the `SWCraft:` commands to the Command Palette,
registers the SWCraft MCP server so an assistant can work on the model, and
works with the two editor extensions the installer put in beside it: Red Hat's
YAML extension (the model files, with completion and validation from SWCraft's
own schemas) and Red Hat's XML extension (the generated ARXML). Its *Diagrams*
item generates the diagrams and shows them in a tab of the editor.
`SWCraft: Check Editor Tooling` reports what the editor side can and cannot see.

A project records which release it is built with, in `swcraft.yaml`:

```yaml
toolchain_version: <version>
```

Every `swcraft` command reads that field first. A `swcraft` that is not the
release named there refuses the commands that write or gate the build, and warns
without failing for the read-only ones — so a project and a workstation cannot
drift apart silently. The field is optional; a project without it pins nothing.

## Updating, and going back

Download the next archive, unpack it, and run the wrapper inside **that**
archive. It installs the new version beside the current one, switches `current`,
updates the extension and then removes the older version. There is nothing to
uninstall first.

Going back to an earlier release is the same procedure with that release's
archive: the installer you run is always the one inside the archive you
unpacked.

## Removing it

From the unpacked archive:

```
.\swcraft_install.cmd --uninstall
```

```sh
./swcraft_install.sh --uninstall
```

That removes the installation directory and the SWCraft VS Code extension. The
two editor extensions stay: they are ordinary extensions of your editor, and
you remove them from the Extensions view if you no longer want them. On **Windows**
it also takes the entry it added off your user `PATH`; on macOS and Linux it
prints the line to delete from your shell profile, and edits no file. It writes
no `install.log`: it removes the directory the log would be kept in.

## If something goes wrong

Every refusal is one sentence, the last line on the console, naming what to do
next. When a step fails, its line ends in `failed`, and the lines above the
refusal are the last lines of `install.log` (up to twenty) and the log's path:
what `pip` or `code` said is there. A run that succeeds prints the log's path
only when a program said no to something that does not stop the installation —
an editor extension `code` would not install. The most common refusals:

- *"… does not match its line in SHA256SUMS …"* — the Python the archive carries
  is damaged or missing: download the archive again and unpack it afresh.
- *"… could not be created or written …, so nothing was installed: pass a --home
  you can write to"* — the installation directory, or `install.log` in it, cannot
  be made: name a directory of your own with `--home`.
- *"SWCraft needs Python 3.11 …"* — only when `--python` names an older Python:
  leave the option out, and the Python the archive carries is used instead.
- *"… so the toolchain is installed and the extension is not …"* — the command
  line is ready to use; install the named `.vsix` from the VS Code Extensions
  view, *Install from VSIX…*, when you get to the editor.

If the installer stops part-way through for any other reason, running it again
is safe: it re-does the steps it needs and leaves the working installation it
found.

A **first** install that fails leaves something behind. One that fails before
anything is unpacked — at the check of the wheels — leaves an installation
directory holding only `install.log`: `--uninstall` refuses it as no installation, so delete that
directory yourself. One that fails later may leave part of an installation
beside the log — an unpacked runtime, a `versions/<version>` directory — and
`--uninstall` removes it.
